"""Сетевые проверки: сервер + «болванки» клиентов на сокетах."""

from __future__ import annotations

import json
import socket
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from minesweeper.server import GameServer

W, H = 200, 200


class FakeClient:
    def __init__(self, port: int, name: str, password: str = "") -> None:
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=5)
        self.sock.settimeout(5)
        self.stream = self.sock.makefile("rb")
        self.send({"t": "hello", "name": name, "password": password})

    def send(self, message: dict) -> None:
        self.sock.sendall((json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8"))

    def read(self) -> dict:
        line = self.stream.readline()
        if not line:
            raise AssertionError("сервер закрыл соединение")
        return json.loads(line)

    def wait(self, kind: str, limit: int = 40) -> dict:
        for _ in range(limit):
            msg = self.read()
            if msg.get("t") == kind:
                return msg
        raise AssertionError(f"не дождались сообщения {kind}")

    def close(self) -> None:
        # сначала файл: пока он открыт, сокет не закрывает дескриптор
        try:
            self.stream.close()
        finally:
            self.sock.close()


def handshake(port: int, name: str, password: str = "") -> dict:
    """Первое сообщение сервера: welcome или denied."""
    sock = socket.create_connection(("127.0.0.1", port), timeout=5)
    stream = sock.makefile("rb")
    sock.sendall((json.dumps({"t": "hello", "name": name, "password": password}) + "\n").encode())
    line = stream.readline()
    sock.close()
    if not line:
        raise AssertionError("сервер молчит")
    return json.loads(line)


def test_guards() -> None:
    """Пароль и лимит игроков."""
    server = GameServer(port=0, width=60, height=40, density=0.2, name="Закрытая",
                        max_players=2, password="1234", seed=5, log=lambda *_: None)
    server.start()

    denied = handshake(server.port, "Без пароля")
    assert denied["t"] == "denied" and denied["reason"] == "password", denied
    denied = handshake(server.port, "Неверный", password="0000")
    assert denied["reason"] == "password", denied

    a = FakeClient(server.port, "Аня", password="1234")
    welcome = a.wait("welcome")
    assert welcome["locked"] is True and welcome["max_players"] == 2
    assert welcome["server"] == "Закрытая"
    b = FakeClient(server.port, "Боря", password="1234")
    b.wait("welcome")

    full = handshake(server.port, "Третий", password="1234")
    assert full["t"] == "denied" and full["reason"] == "full", full

    a.close()
    b.close()
    time.sleep(0.2)
    server.stop()
    print("ok: пароль и лимит игроков работают")


def test_discovery() -> None:
    """Автопоиск отдаёт название, лимит и признак пароля."""
    from minesweeper.protocol import discover

    server = GameServer(port=0, width=80, height=60, density=0.1, name="Найди меня",
                        max_players=3, password="x", seed=7, log=lambda *_: None)
    server.start()
    time.sleep(0.3)
    try:
        found = discover(1.5)
    finally:
        server.stop()
    match = [info for info in found if info.get("name") == "Найди меня"]
    assert match, found
    info = match[0]
    assert info["port"] == server.port, info
    assert info["max_players"] == 3 and info["locked"] is True, info
    assert info["w"] == 80 and info["h"] == 60, info
    print(f"ok: автопоиск видит игру {info['host']}:{info['port']}, пароль и лимит")


def main() -> None:
    test_guards()

    server = GameServer(port=0, width=W, height=H, density=0.2, name="Проверка",
                        max_players=8, seed=1234, log=lambda *_: None)
    server.start()
    time.sleep(0.1)
    board = server.board

    a = FakeClient(server.port, "Аня")
    welcome = a.wait("welcome")
    assert welcome["w"] == W and welcome["h"] == H
    assert welcome["host"] is True, welcome
    assert welcome["mines"] == board.total_mines
    assert welcome["locked"] is False and welcome["max_players"] == 8
    print(f"ok: приветствие, мин {welcome['mines']}")

    a.send({"t": "view", "x": 40, "y": 40, "w": 20, "h": 10})
    view = a.wait("view")
    assert view["x"] == 40 and len(view["rows"]) == 10 and len(view["rows"][0]) == 20
    assert all(c in "?F.12345678 *" for row in view["rows"] for c in row)
    print("ok: выдача окна 20x10")

    a.send({"t": "flag", "x": 40, "y": 40})
    view = a.wait("view")
    assert view["rows"][0][0] == "F", view["rows"][0]
    a.send({"t": "flag", "x": 40, "y": 40})
    view = a.wait("view")
    assert view["rows"][0][0] == "?", view["rows"][0]
    print("ok: флаги ставятся и снимаются")

    a.send({"t": "overview"})
    overview = a.wait("overview")
    assert overview["w"] == 160 and len(overview["rows"]) == 160
    assert all(len(row) == 160 for row in overview["rows"])
    assert set("".join(overview["rows"])) <= set("0123456789")
    print("ok: миникарта поля 160x160 с заполненностью")

    safe = None
    for y in range(40, 50):
        for x in range(40, 60):
            if not board.is_mine(x, y):
                safe = (x, y)
                break
        if safe:
            break
    a.send({"t": "reveal", "x": safe[0], "y": safe[1]})
    view = a.wait("view")
    assert view["found"] >= 1 and view["alive"] is True
    print(f"ok: открытие клетки {safe}, всего открыто {view['found']}")

    b = FakeClient(server.port, "Боря")
    welcome_b = b.wait("welcome")
    assert welcome_b["host"] is False, welcome_b
    join = a.wait("event")
    while join.get("kind") != "join":
        join = a.wait("event")
    assert join["name"] == "Боря"
    print("ok: событие подключения второго игрока")

    mine = None
    for y in range(40, 50):
        for x in range(40, 60):
            if board.is_mine(x, y):
                mine = (x, y)
                break
        if mine:
            break
    b.send({"t": "view", "x": 40, "y": 40, "w": 20, "h": 10})
    b.wait("view")
    b.send({"t": "reveal", "x": mine[0], "y": mine[1]})
    boom = b.wait("event")
    while boom.get("kind") != "boom":
        boom = b.wait("event")
    assert boom["name"] == "Боря"
    view = b.wait("view")
    assert view["alive"] is False and "*" in "".join(view["rows"])
    print(f"ok: подрыв на {mine}, взрыв виден в окне")

    # взрыв заканчивает партию всем, а не только подорвавшемуся
    kinds, view_a = [], None
    while view_a is None:
        msg = a.read()
        if msg.get("t") == "event":
            kinds.append(msg.get("kind"))
        elif msg.get("t") == "view":
            view_a = msg
    assert view_a["alive"] is False, view_a
    assert "boom" in kinds and "gameover" in kinds, kinds
    print("ok: подорвались все игроки")

    b.send({"t": "reveal", "x": safe[0], "y": safe[1]})
    info = b.wait("info")
    assert "хозяин" in info["text"], info
    print("ok: после взрыва клетки не открываются")

    b.send({"t": "restart"})
    info = b.wait("info")
    assert "хозяин" in info["text"], info
    assert server.board is board, "не хозяин не должен перезапускать партию"
    print("ok: новую партию начинает только хозяин")

    a.send({"t": "chat", "text": "привет"})
    chat = b.wait("chat")
    assert chat["text"] == "привет" and chat["name"] == "Аня"
    print("ok: чат")

    old_board = server.board
    a.send({"t": "restart"})
    restart = a.wait("event")
    while restart.get("kind") != "restart":
        restart = a.wait("event")
    view = a.wait("view")
    assert view["over"] is False and view["alive"] is True
    assert server.board is not old_board
    print("ok: новая партия по R")

    # хозяин ушёл — игра переходит к следующему игроку
    a.close()
    moved = False
    try:
        for _ in range(60):
            msg = b.read()
            if msg.get("t") != "players":
                continue
            me = [p for p in msg["players"] if p["id"] == welcome_b["id"]]
            if me and me[0]["host"]:
                moved = True
                break
    except (socket.timeout, OSError):
        pass
    assert moved, "после ухода хозяина игра должна перейти к следующему"
    print("ok: хозяин передаётся следующему игроку")

    b.send({"t": "restart"})
    restart = b.wait("event")
    while restart.get("kind") != "restart":
        restart = b.wait("event")
    print("ok: новый хозяин перезапускает партию")

    b.close()
    time.sleep(0.2)
    server.stop()
    print("\nвсе сетевые проверки пройдены")


if __name__ == "__main__":
    test_discovery()
    main()
