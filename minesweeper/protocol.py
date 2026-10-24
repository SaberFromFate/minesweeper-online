"""Протокол клиента и сервера.

Транспорт — TCP, каждое сообщение есть одна строка JSON (UTF-8) с ``\\n``
в конце. Сервер авторитетен, а по сети ходит только видимое окно поля.
"""

from __future__ import annotations

import json
import socket

#: сторона поля по умолчанию (500 x 500 = 250 тыс. клеток)
FIELD_SIZE = 500

#: быстрые кнопки выбора стороны поля (квадратные пресеты)
FIELD_PRESETS = (50, 100, 200, 300, 400, 500, 800, 1000, 1500)

#: границы произвольного размера (и для сохранений, и для новых партий)
FIELD_MIN = 8
FIELD_MAX = 4000

#: цвета игроков (по кругу)
PALETTE = [
    "#ffd43b",
    "#4dabf7",
    "#ff8787",
    "#69db7c",
    "#da77f2",
    "#ffa94d",
    "#66d9e8",
    "#f783ac",
]

#: больше окна сервер не отдаст — защита от «покажи мне всё поле»
MAX_VIEW = 300

#: сторона миниатюры поля (миникарта)
OVERVIEW_SIZE = 160

#: порт игры по умолчанию
DEFAULT_PORT = 5555

#: UDP-порт автопоиска серверов в локальной сети
DISCOVERY_PORT = 5556
DISCOVERY_MAGIC = b"MINESWEEPER_LAN_V1"


def encode(message: dict) -> bytes:
    return (json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")


def lan_ip() -> str:
    """Свой адрес в локальной сети (для подсказки друзьям)."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        return str(sock.getsockname()[0])
    except OSError:
        return "127.0.0.1"
    finally:
        sock.close()


def discover(timeout: float = 1.6) -> list[dict]:
    """Ищет игры в локальной сети широковещательным UDP-запросом.

    Возвращает список словарей: name, host, port, players, max_players,
    locked, w, h, mines.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.bind(("", 0))
    sock.settimeout(0.3)
    found: dict[str, dict] = {}
    try:
        for _ in range(max(1, int(timeout / 0.3))):
            try:
                sock.sendto(DISCOVERY_MAGIC, ("255.255.255.255", DISCOVERY_PORT))
            except OSError:
                pass
            for _ in range(3):
                try:
                    data, addr = sock.recvfrom(4096)
                except socket.timeout:
                    break
                except OSError:
                    return list(found.values())
                try:
                    info = json.loads(data)
                except ValueError:
                    continue
                info["host"] = addr[0]
                found[f"{info['host']}:{info.get('port')}"] = info
    finally:
        sock.close()
    return list(found.values())
