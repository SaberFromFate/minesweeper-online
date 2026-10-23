"""Сетевой сервер: одно общее поле и несколько игроков.

Сервер авторитетен: клиенты присылают только «открой (x, y)» и
«поставь флаг». Всё состояние поля живёт здесь, клиенту уходит лишь
его видимое окно — иначе миллион клеток просто не передать.

Сервер также рассказывает о себе в локальную сеть (UDP): название,
число игроков, лимит и наличие пароля — из этого клиент строит
список игр.
"""

from __future__ import annotations

import json
import queue
import random
import socket
import threading
import time

from .board import Board
from .protocol import (
    DEFAULT_PORT,
    DISCOVERY_MAGIC,
    DISCOVERY_PORT,
    FIELD_SIZE,
    MAX_VIEW,
    OVERVIEW_SIZE,
    PALETTE,
    encode,
)


class Player:
    def __init__(self, pid: int, name: str, sock: socket.socket, addr, color: str) -> None:
        self.id = pid
        self.name = name
        self.color = color
        self.sock = sock
        self.addr = addr
        self.view: tuple[int, int, int, int] | None = None
        self.cursor: tuple[int, int] | None = None
        self.alive = True
        self.score = 0
        self.out: queue.Queue[bytes | None] = queue.Queue(maxsize=512)
        threading.Thread(target=self._writer, daemon=True).start()

    def send(self, message: dict) -> None:
        try:
            self.out.put_nowait(encode(message))
        except queue.Full:
            pass

    def close(self) -> None:
        try:
            self.out.put_nowait(None)
        except queue.Full:
            pass

    def _writer(self) -> None:
        while True:
            data = self.out.get()
            if data is None:
                break
            try:
                self.sock.sendall(data)
            except OSError:
                break
        try:
            self.sock.close()
        except OSError:
            pass

    def public(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "color": self.color,
            "alive": self.alive,
            "score": self.score,
            "cursor": list(self.cursor) if self.cursor else None,
            "view": list(self.view) if self.view else None,
        }


class GameServer:
    def __init__(
        self,
        host: str = "0.0.0.0",
        port: int = DEFAULT_PORT,
        width: int = FIELD_SIZE,
        height: int = FIELD_SIZE,
        density: float = 0.16,
        seed: int | None = None,
        name: str = "Сапёр LAN",
        max_players: int = 8,
        password: str = "",
        advertise: bool = True,
        log=print,
    ) -> None:
        self.host = host
        self.port = int(port)
        self.width = int(width)
        self.height = int(height)
        self.density = float(density)
        self.seed = seed
        self.server_name = name
        self.max_players = max(1, int(max_players))
        self.password = password or ""
        self.advertise = advertise
        self.log = log

        self.lock = threading.RLock()
        self.players: dict[int, Player] = {}
        self._next_id = 1
        self.host_id: int | None = None
        self.board = Board(self.width, self.height, self.density, self.seed)
        self.started = False
        self.game_over = False
        self.score_start = 0
        self._listener: socket.socket | None = None
        self._stop = threading.Event()
        self._players_sent = 0.0
        self._map_cache: tuple[tuple[int, int], float, list[str]] | None = None

    # ------------------------------------------------------------------
    # запуск / остановка
    # ------------------------------------------------------------------
    def start(self) -> None:
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind((self.host, self.port))
        listener.listen(32)
        listener.settimeout(0.5)
        self.port = listener.getsockname()[1]
        self._listener = listener
        threading.Thread(target=self._accept_loop, daemon=True, name="accept").start()
        if self.advertise:
            threading.Thread(target=self._discovery_loop, daemon=True, name="discovery").start()
        self.log(
            f"игра «{self.server_name}»: поле {self.width}x{self.height}, "
            f"мин {self.board.total_mines:,}, порт {self.port}, "
            f"до {self.max_players} игроков"
            + (", с паролем" if self.password else "")
        )

    def serve_forever(self) -> None:
        while not self._stop.is_set():
            time.sleep(0.5)

    def stop(self) -> None:
        self._stop.set()
        if self._listener is not None:
            try:
                self._listener.close()
            except OSError:
                pass
        with self.lock:
            players = list(self.players.values())
            self.players.clear()
        for player in players:
            player.close()

    # ------------------------------------------------------------------
    # сеть
    # ------------------------------------------------------------------
    def _accept_loop(self) -> None:
        while not self._stop.is_set():
            try:
                sock, addr = self._listener.accept()  # type: ignore[union-attr]
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(
                target=self._client_loop, args=(sock, addr), daemon=True, name=f"client-{addr[0]}"
            ).start()

    def _client_loop(self, sock: socket.socket, addr) -> None:
        try:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass
        player: Player | None = None
        stream = sock.makefile("rb")
        try:
            for raw in stream:
                try:
                    message = json.loads(raw)
                except (ValueError, UnicodeDecodeError):
                    continue
                if not isinstance(message, dict):
                    continue
                if player is None:
                    if message.get("t") == "hello":
                        player = self._register(sock, addr, message.get("name"),
                                                message.get("password"))
                        if player is None:
                            break
                    continue
                self._dispatch(player, message)
        except OSError:
            pass
        finally:
            if player is not None:
                self._unregister(player)
            else:
                try:
                    sock.close()
                except OSError:
                    pass
            try:
                stream.close()
            except OSError:
                pass

    @staticmethod
    def _deny(sock: socket.socket, reason: str, message: str) -> None:
        try:
            sock.sendall(encode({"t": "denied", "reason": reason, "message": message}))
        except OSError:
            pass
        try:
            sock.close()
        except OSError:
            pass

    def _register(self, sock: socket.socket, addr, name, password) -> Player | None:
        with self.lock:
            if len(self.players) >= self.max_players:
                self._deny(sock, "full", "Игра уже заполнена")
                return None
            if self.password and str(password or "") != self.password:
                self._deny(sock, "password", "Неверный пароль")
                return None
            pid = self._next_id
            self._next_id += 1
            clean = str(name or "").strip()[:20] or f"игрок{pid}"
            player = Player(pid, clean, sock, addr, PALETTE[(pid - 1) % len(PALETTE)])
            # загруженная соло-партия продолжается с прежним счётом
            player.score = int(self.score_start)
            self.players[pid] = player
            if self.host_id is None:
                self.host_id = pid
            is_host = pid == self.host_id
            board = self.board
            started, over = self.started, self.game_over
            players_now = len(self.players)

        player.send(
            {
                "t": "welcome",
                "id": pid,
                "name": clean,
                "color": player.color,
                "w": board.w,
                "h": board.h,
                "mines": board.total_mines,
                "density": self.density,
                "server": self.server_name,
                "max_players": self.max_players,
                "locked": bool(self.password),
                "players": players_now,
                "started": started,
                "over": over,
                "host": is_host,
            }
        )
        self._broadcast_event({"kind": "join", "name": clean, "color": player.color})
        self._broadcast_players()
        self.log(f"+ {clean} ({addr[0]})")
        return player

    def _unregister(self, player: Player) -> None:
        with self.lock:
            self.players.pop(player.id, None)
            if self.host_id == player.id:
                self.host_id = min(self.players) if self.players else None
        player.close()
        self._broadcast_event({"kind": "leave", "name": player.name})
        self._broadcast_players()
        self.log(f"- {player.name}")

    # ------------------------------------------------------------------
    # сообщения клиентов
    # ------------------------------------------------------------------
    def _dispatch(self, player: Player, msg: dict) -> None:
        kind = msg.get("t")
        if kind == "view":
            self._on_view(player, msg)
        elif kind == "reveal":
            self._on_reveal(player, msg)
        elif kind == "flag":
            self._on_flag(player, msg)
        elif kind == "cursor":
            self._on_cursor(player, msg)
        elif kind == "chat":
            text = str(msg.get("text", ""))[:200].strip()
            if text:
                self._broadcast({"t": "chat", "name": player.name,
                                 "color": player.color, "text": text})
        elif kind == "restart":
            if player.id != self.host_id:
                player.send({"t": "info",
                             "text": "Новую партию начинает хозяин игры."})
                return
            self.new_game()
        elif kind == "overview":
            player.send(
                {
                    "t": "overview",
                    "w": OVERVIEW_SIZE,
                    "h": OVERVIEW_SIZE,
                    "rows": self.overview_rows(),
                }
            )
        elif kind == "ping":
            player.send({"t": "pong"})

    def _on_view(self, player: Player, msg: dict) -> None:
        try:
            x = int(msg["x"])
            y = int(msg["y"])
            w = max(1, min(MAX_VIEW, int(msg["w"])))
            h = max(1, min(MAX_VIEW, int(msg["h"])))
        except (KeyError, TypeError, ValueError):
            return
        x = max(0, min(self.board.w - w, x))
        y = max(0, min(self.board.h - h, y))
        with self.lock:
            first = player.view is None
            player.view = (x, y, w, h)
            self._send_view(player)
            # миникарта у остальных: где сейчас смотрит игрок
            if first or time.time() - self._players_sent > 0.25:
                self._broadcast_players()

    def _on_reveal(self, player: Player, msg: dict) -> None:
        try:
            x, y = int(msg["x"]), int(msg["y"])
        except (KeyError, TypeError, ValueError):
            return
        with self.lock:
            board = self.board
            if self.game_over:
                player.send({"t": "info",
                             "text": "Партия окончена. Новую запускает хозяин игры (R)."})
                return
            if not player.alive:
                player.send({"t": "info",
                             "text": "Вы подорвались вместе со всеми — новую партию запускает хозяин."})
                return
            if not board.inside(x, y):
                return
            if not self.started:
                board.make_safe(x, y)
                self.started = True
            new_cells, boom = board.reveal(x, y)
            if boom:
                # одна мина заканчивает партию всем: команда либо дошла, либо нет
                board.blast(x, y)
                for other in self.players.values():
                    other.alive = False
                self._broadcast_event(
                    {"kind": "boom", "name": player.name, "color": player.color, "x": x, "y": y}
                )
            else:
                player.score += new_cells
            self._check_finish()
        self._broadcast_views()
        self._broadcast_players()

    def _on_flag(self, player: Player, msg: dict) -> None:
        try:
            x, y = int(msg["x"]), int(msg["y"])
        except (KeyError, TypeError, ValueError):
            return
        with self.lock:
            board = self.board
            if not board.inside(x, y) or self.game_over:
                return
            board.toggle_flag(x, y)
        self._broadcast_views()

    def _on_cursor(self, player: Player, msg: dict) -> None:
        try:
            x, y = int(msg["x"]), int(msg["y"])
        except (KeyError, TypeError, ValueError):
            return
        player.cursor = (x, y)
        now = time.time()
        if now - self._players_sent > 0.12:
            self._players_sent = now
            self._broadcast_players()

    # ------------------------------------------------------------------
    # партия
    # ------------------------------------------------------------------
    def new_game(self, seed: int | None = None) -> None:
        # --seed задаёт только стартовое поле; новые партии — новые зёрна
        if seed is None:
            seed = random.randrange(1 << 31)
        board = Board(self.width, self.height, self.density, seed)
        with self.lock:
            self.board = board
            self.seed = seed
            self.started = False
            self.game_over = False
            self._map_cache = None
            for player in self.players.values():
                player.alive = True
                player.score = 0
        self._broadcast_event({"kind": "restart", "mines": board.total_mines})
        self._broadcast_views()
        self._broadcast_players()
        self.log(f"новая партия, seed={seed}, мин {board.total_mines:,}")

    def _check_finish(self) -> None:
        board = self.board
        if not self.players:
            return
        if all(not p.alive for p in self.players.values()):
            self.game_over = True
            self._broadcast_event({"kind": "gameover"})
        elif board.revealed_count >= board.w * board.h - board.total_mines:
            self.game_over = True
            self._broadcast_event({"kind": "win"})

    # ------------------------------------------------------------------
    # миникарта
    # ------------------------------------------------------------------
    def overview_rows(self) -> list[str]:
        """Миниатюра поля для миникарты (кэш на полсекунды).

        Считается без блокировки: чтение готовых битовых строк безопасно,
        а миникарте не страшна пара строк, открытых во время съёмки.
        """
        board = self.board
        key = (id(board), board.revealed_count)
        cache = self._map_cache
        now = time.time()
        if cache is not None and cache[0] == key and now - cache[1] < 0.4:
            return cache[2]
        rows = board.overview(OVERVIEW_SIZE)
        self._map_cache = (key, now, rows)
        return rows

    # ------------------------------------------------------------------
    # рассылка
    # ------------------------------------------------------------------
    def _send_view(self, player: Player) -> None:
        if player.view is None:
            return
        x, y, w, h = player.view
        board = self.board
        player.send(
            {
                "t": "view",
                "x": x,
                "y": y,
                "rows": board.view_rows(x, y, w, h),
                "found": board.revealed_count,
                "safe": board.w * board.h - board.total_mines,
                "over": self.game_over,
                "alive": player.alive,
                "score": player.score,
            }
        )

    def _broadcast_views(self) -> None:
        with self.lock:
            for player in self.players.values():
                self._send_view(player)

    def _broadcast_players(self) -> None:
        with self.lock:
            listing = []
            for p in self.players.values():
                entry = p.public()
                entry["host"] = p.id == self.host_id
                listing.append(entry)
        self._players_sent = time.time()
        self._broadcast({"t": "players", "players": listing})

    def _broadcast_event(self, event: dict) -> None:
        event["t"] = "event"
        self._broadcast(event)

    def _broadcast(self, message: dict) -> None:
        with self.lock:
            players = list(self.players.values())
        for player in players:
            player.send(message)

    # ------------------------------------------------------------------
    # автопоиск в локальной сети
    # ------------------------------------------------------------------
    def _discovery_loop(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(("", DISCOVERY_PORT))
            sock.settimeout(0.5)
        except OSError:
            sock.close()
            return
        while not self._stop.is_set():
            try:
                data, addr = sock.recvfrom(1024)
            except socket.timeout:
                continue
            except OSError:
                break
            if data.strip() != DISCOVERY_MAGIC:
                continue
            answer = {
                "name": self.server_name,
                "port": self.port,
                "players": len(self.players),
                "max_players": self.max_players,
                "locked": bool(self.password),
                "w": self.width,
                "h": self.height,
                "mines": self.board.total_mines,
                "started": self.started,
                "over": self.game_over,
            }
            try:
                sock.sendto(encode(answer).strip(), addr)
            except OSError:
                pass
        sock.close()
