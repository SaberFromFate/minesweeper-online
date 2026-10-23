"""Служебный скрипт: снимает скриншоты всех экранов для проверки вида.

Нужен только для разработки, в игру не входит.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import tkinter as tk
from PIL import ImageGrab

from minesweeper.client import App, GameScreen
from minesweeper.server import GameServer

OUT = Path(__file__).resolve().parents[1]


def main() -> None:
    server = GameServer(
        port=0, name="Вечерний сапёр", max_players=4, seed=4, log=lambda *_: None
    )
    server.start()

    root = tk.Tk()
    root.attributes("-topmost", True)
    app = App(root, "Аня")
    app.show_menu()

    def grab(name: str) -> None:
        root.update_idletasks()
        root.update()
        x, y = root.winfo_rootx(), root.winfo_rooty()
        w, h = root.winfo_width(), root.winfo_height()
        ImageGrab.grab(bbox=(x, y, x + w, y + h)).save(OUT / name)
        print(f"saved {name} {w}x{h}", flush=True)

    def shoot_menu() -> None:
        grab("shot_menu.png")
        app.open_settings()
        root.after(600, shoot_settings)

    def shoot_settings() -> None:
        grab("shot_settings.png")
        for widget in root.winfo_children():
            if widget.winfo_class() == "Toplevel":
                widget.destroy()
        app.show_servers()
        root.after(2400, shoot_servers)

    def shoot_servers() -> None:
        screen = app.screen
        found = list(screen.games.values())
        found.append(
            {
                "host": "192.168.1.42",
                "port": 5555,
                "name": "Тихий вечер",
                "players": 2,
                "max_players": 6,
                "locked": True,
                "w": 500,
                "h": 500,
            }
        )
        screen._apply(found)
        children = screen.tree.get_children()
        if children:
            screen.tree.selection_set(children[0])
        grab("shot_servers.png")

        app.ask_password(found[-1])
        root.after(500, shoot_password)

    def shoot_password() -> None:
        grab("shot_password.png")
        app.show_create()
        root.after(500, shoot_create)

    def shoot_create() -> None:
        grab("shot_create.png")
        app.start_solo()
        root.after(1200, shoot_game)

    def shoot_game() -> None:
        screen = app.screen
        if not isinstance(screen, GameScreen) or not screen.cells:
            root.after(400, shoot_game)
            return
        # первый клик делает область безопасной, потом открываем пятно вокруг
        cx, cy = screen.cam_x + screen.cols // 2, screen.cam_y + screen.rows // 2
        screen.net.send({"t": "reveal", "x": cx, "y": cy})
        root.after(400, lambda: open_area(cx, cy))

    def open_area(cx: int, cy: int) -> None:
        screen = app.screen
        board = app.server.board
        for dy in range(-6, 7):
            for dx in range(-11, 12):
                x, y = cx + dx, cy + dy
                if board.inside(x, y) and not board.is_mine(x, y):
                    screen.net.send({"t": "reveal", "x": x, "y": y})
        root.after(700, finish)

    def finish() -> None:
        grab("shot_game.png")
        root.destroy()

    root.after(700, shoot_menu)
    try:
        root.mainloop()
    finally:
        server.stop()


if __name__ == "__main__":
    main()
