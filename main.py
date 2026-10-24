"""Сапёр 500x500 с мультиплеером по локальной сети.

Без аргументов открывается меню: соло, сетевые игры, сохранения, выход.
Сеть ищется автоматически широковещательным запросом.
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path

from minesweeper.protocol import DEFAULT_PORT, FIELD_MAX, FIELD_MIN, FIELD_SIZE, discover, lan_ip
from minesweeper.server import GameServer


def _ensure_streams() -> None:
    """Под pythonw (без консоли) stdout/stderr — None: пишем их в лог-файл."""
    if sys.stdout is not None and sys.stderr is not None:
        return
    try:
        log = open(Path(tempfile.gettempdir()) / "minesweeper-lan.log", "a",
                   encoding="utf-8", buffering=1)
    except OSError:
        return
    if sys.stdout is None:
        sys.stdout = log
    if sys.stderr is None:
        sys.stderr = log


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="minesweeper",
        description="Сапёр на поле 500x500 с сетевым режимом по LAN",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "примеры:\n"
            "  python main.py                        меню: соло / сетевая игра\n"
            "  python main.py --host 192.168.1.10    сразу подключиться к другу\n"
            "  python main.py --discover             найти игру в локальной сети\n"
            "  python main.py --server               выделенный сервер без окна\n"
            "  python main.py --server --game-name Вечер --password 1234 "
            "--max-players 6\n"
        ),
    )
    parser.add_argument("--host", help="адрес сервера: без меню сразу подключиться")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT,
                        help=f"порт (по умолчанию {DEFAULT_PORT})")
    parser.add_argument("--name", help="имя игрока (по умолчанию имя компьютера)")
    parser.add_argument("--density", type=float, default=0.16,
                        help="плотность мин, 0..1 (по умолчанию 0.16)")
    parser.add_argument("--seed", type=int, default=None, help="фиксированное зерно поля")
    parser.add_argument("--server", action="store_true", help="только сервер, без окна")
    parser.add_argument("--discover", action="store_true",
                        help="найти игру широковещательным запросом и сразу войти")
    parser.add_argument("--game-name", default="Сапёр LAN",
                        help="название игры для режима --server")
    parser.add_argument("--max-players", type=int, default=8,
                        help="максимум игроков для режима --server (по умолчанию 8)")
    parser.add_argument("--password", default="",
                        help="пароль игры для режима --server (необязательно)")
    parser.add_argument("--width", type=int, default=FIELD_SIZE,
                        help=f"ширина поля (по умолчанию {FIELD_SIZE})")
    parser.add_argument("--height", type=int, default=FIELD_SIZE,
                        help=f"высота поля (по умолчанию {FIELD_SIZE})")
    return parser


def main(argv: list[str] | None = None) -> int:
    _ensure_streams()
    args = build_parser().parse_args(argv)

    if not 0.0 <= args.density < 0.95:
        print("Плотность мин должна быть в диапазоне 0..0.95", file=sys.stderr)
        return 2

    if not FIELD_MIN <= (args.width or 0) <= FIELD_MAX:
        print(f"Ширина поля должна быть от {FIELD_MIN} до {FIELD_MAX}", file=sys.stderr)
        return 2
    if not FIELD_MIN <= (args.height or 0) <= FIELD_MAX:
        print(f"Высота поля должна быть от {FIELD_MIN} до {FIELD_MAX}", file=sys.stderr)
        return 2

    # имя может прийти из настроек (файл рядом с пользователем), поэтому
    # пустое значение передаём как есть
    name = args.name
    port = args.port

    if args.server:
        server = GameServer(
            port=port,
            name=args.game_name,
            max_players=args.max_players,
            password=args.password,
            density=args.density,
            seed=args.seed,
            width=args.width,
            height=args.height,
        )
        server.start()
        print(f"Игра «{args.game_name}» доступна по адресу {lan_ip()}:{server.port}", flush=True)
        if args.password:
            print("Вход по паролю.", flush=True)
        print("Подключение: python main.py --host <этот IP>   (или --discover)", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print("\nостановлено")
        finally:
            server.stop()
        return 0

    host = args.host
    if host is None and args.discover:
        print("Ищу игры в локальной сети…")
        found = discover()
        if found:
            for info in found:
                lock = " 🔒" if info.get("locked") else ""
                print(
                    f"  {info['host']}:{info.get('port')}  «{info.get('name')}»{lock}  "
                    f"{info.get('players')}/{info.get('max_players')}"
                )
            host = found[0]["host"]
            port = int(found[0].get("port", port))
        else:
            print("Никого не нашёл — открываю меню.")

    try:
        from minesweeper.client import run_client
    except ImportError as exc:  # pragma: no cover
        print(f"Нужен tkinter для окна: {exc}", file=sys.stderr)
        print("Можно запустить только сервер: python main.py --server", file=sys.stderr)
        return 2

    run_client(host, port, name, density=args.density, seed=args.seed)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
