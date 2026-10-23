"""Сохранения партии в «Документах» пользователя.

Файл — JSON, а битовые карты поля (мины, открытые клетки, флаги, взрывы)
лежат в нём строками base64 от zlib. Поле 500 × 500 занимает ~45 КБ
на карту, но сжимается хорошо, поэтому сохранение весит ~30 КБ.
"""

from __future__ import annotations

import base64
import json
import os
import sys
import time
import zlib
from pathlib import Path

#: версия формата файла
FORMAT = 1

#: расширение сохранений
SUFFIX = ".saper"

#: папка внутри «Документов»
FOLDER = "Minesweeper"

#: разумные пределы поля, чтобы битый файл не съел память
MIN_SIDE, MAX_SIDE = 8, 4000

_BAD_CHARS = '<>:"/\\|?*\n\r\t'


# ----------------------------------------------------------------------
# где лежит
# ----------------------------------------------------------------------
def _registry_documents() -> Path | None:
    """«Документы» из реестра: так учитывается перенос папки в OneDrive."""
    if sys.platform != "win32":
        return None
    try:
        import winreg

        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders",
        )
        try:
            value, _kind = winreg.QueryValueEx(key, "Personal")
        finally:
            key.Close()
        value = os.path.expandvars(str(value))
        return Path(value) if value else None
    except (OSError, ImportError, ValueError):
        return None


def documents_dir() -> Path:
    path = _registry_documents()
    if path is None:
        home = os.environ.get("USERPROFILE") or str(Path.home())
        path = Path(home) / "Documents"
    return path


def saves_dir() -> Path:
    """Папка сохранений (создаётся при первой записи)."""
    return documents_dir() / FOLDER


# ----------------------------------------------------------------------
# запись
# ----------------------------------------------------------------------
def clean_title(title: str) -> str:
    name = " ".join(str(title).split())
    for char in _BAD_CHARS:
        name = name.replace(char, "-")
    return name.strip(" .")[:80] or "Партия"


def _bitmap(rows) -> str:
    return base64.b64encode(zlib.compress(rows, 6)).decode("ascii")


def _pack(rows, width: int) -> bytes:
    nbytes = (width + 7) // 8
    return b"".join(int(row).to_bytes(nbytes, "little") for row in rows)


def save_path(directory: Path, title: str) -> Path:
    """Путь, по которому ляжет партия с таким названием."""
    return Path(directory) / f"{clean_title(title)}{SUFFIX}"


def write_save(directory: Path, title: str, board, *,
               score: int = 0, cam: tuple[int, int] = (0, 0),
               cs: int = 26, started: bool = True,
               mode: str = "solo", server: str = "",
               max_players: int = 1) -> Path:
    """Сохраняет партию и возвращает путь к файлу.

    ``mode`` — "solo" или "net": по нему загрузка решает, поднимать ли
    игру снова видимой в локальной сети и на сколько мест.
    """
    title = clean_title(title)
    width, height = int(board.w), int(board.h)
    mines = _pack(board.mines, width)
    revealed = _pack(board.revealed, width)
    flags = _pack([board.flags.get(y, 0) for y in range(height)], width)
    blasts = _pack([board.blasts.get(y, 0) for y in range(height)], width)

    payload = {
        "format": FORMAT,
        "game": "saper",
        "name": title,
        "saved": time.strftime("%Y-%m-%d %H:%M:%S"),
        "w": width,
        "h": height,
        "density": float(board.density),
        "seed": int(board.seed),
        "started": bool(started),
        "mines": int(board.total_mines),
        "found": int(board.revealed_count),
        "score": int(score),
        "cam": [int(cam[0]), int(cam[1])],
        "cs": int(cs),
        "mode": "net" if mode == "net" else "solo",
        "server": str(server)[:60],
        "max_players": max(1, int(max_players)),
        "bitmaps": {
            "mines": _bitmap(mines),
            "revealed": _bitmap(revealed),
            "flags": _bitmap(flags),
            "blasts": _bitmap(blasts),
        },
    }

    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    path = save_path(directory, title)
    temp = path.with_suffix(SUFFIX + ".tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    temp.replace(path)
    return path


# ----------------------------------------------------------------------
# чтение
# ----------------------------------------------------------------------
def _unpack(text: str, width: int, height: int, what: str) -> list[int]:
    nbytes = (width + 7) // 8
    try:
        raw = zlib.decompress(base64.b64decode(str(text), validate=True))
    except (ValueError, zlib.error) as exc:
        raise ValueError(f"повреждена карта «{what}»") from exc
    if len(raw) != nbytes * height:
        raise ValueError(f"карта «{what}» не совпадает с размером поля")
    return [
        int.from_bytes(raw[y * nbytes:(y + 1) * nbytes], "little")
        for y in range(height)
    ]


def read_save(path: Path) -> dict:
    """Читает файл сохранения, проверяя формат и размеры."""
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("format") != FORMAT:
        raise ValueError("неизвестный формат сохранения")
    try:
        width, height = int(payload["w"]), int(payload["h"])
        density = float(payload["density"])
        seed = int(payload["seed"])
        bitmaps = payload["bitmaps"]
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("в файле нет данных партии") from exc
    if not (MIN_SIDE <= width <= MAX_SIDE and MIN_SIDE <= height <= MAX_SIDE):
        raise ValueError("неподходящий размер поля")
    if not 0.0 <= density < 0.95:
        raise ValueError("неподходящая плотность мин")

    payload["w"], payload["h"] = width, height
    payload["density"], payload["seed"] = density, seed
    try:
        players = int(payload.get("max_players", 1) or 1)
    except (TypeError, ValueError):
        players = 1
    payload["max_players"] = max(1, players)
    mode = str(payload.get("mode") or "")
    payload["mode"] = "net" if (mode == "net" or payload["max_players"] > 1) else "solo"
    payload["server"] = str(payload.get("server") or "")
    payload["rows"] = {
        "mines": _unpack(bitmaps.get("mines", ""), width, height, "мины"),
        "revealed": _unpack(bitmaps.get("revealed", ""), width, height, "открыто"),
        "flags": _unpack(bitmaps.get("flags", ""), width, height, "флаги"),
        "blasts": _unpack(bitmaps.get("blasts", ""), width, height, "взрывы"),
    }
    return payload


def apply_save(board, payload: dict) -> None:
    """Переносит прочитанную партию в поле сервера."""
    rows = payload["rows"]
    if (int(payload["w"]), int(payload["h"])) != (board.w, board.h):
        raise ValueError("размер поля в сохранении другой")
    board.mines = list(rows["mines"])
    board.revealed = list(rows["revealed"])
    board.flags = {y: row for y, row in enumerate(rows["flags"]) if row}
    board.blasts = {y: row for y, row in enumerate(rows["blasts"]) if row}
    board.total_mines = sum(row.bit_count() for row in board.mines)
    blasts = board.blasts
    board.revealed_count = sum(
        (row & ~blasts.get(y, 0)).bit_count() for y, row in enumerate(board.revealed)
    )


# ----------------------------------------------------------------------
# список
# ----------------------------------------------------------------------
def list_saves(directory: Path) -> list[dict]:
    """Сохранения в папке, свежие сверху."""
    directory = Path(directory)
    if not directory.is_dir():
        return []
    items = []
    for path in directory.glob("*" + SUFFIX):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(payload, dict) or payload.get("format") != FORMAT:
            continue
        try:
            players = max(1, int(payload.get("max_players", 1) or 1))
        except (TypeError, ValueError):
            players = 1
        items.append({
            "path": path,
            "name": str(payload.get("name") or path.stem),
            "saved": str(payload.get("saved") or ""),
            "w": payload.get("w"), "h": payload.get("h"),
            "mines": payload.get("mines", 0),
            "found": payload.get("found", 0),
            "score": payload.get("score", 0),
            "mode": "solo" if players <= 1 else "net",
            "max_players": players,
            "mtime": path.stat().st_mtime,
        })
    items.sort(key=lambda item: item["mtime"], reverse=True)
    return items


def delete_save(path: Path) -> None:
    try:
        Path(path).unlink()
    except OSError:
        pass


def human_time(stamp: str) -> str:
    """«2026-10-23 18:12:00» → «23.10.2026 18:12»."""
    try:
        moment = time.strptime(stamp, "%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        return stamp
    return time.strftime("%d.%m.%Y %H:%M", moment)
