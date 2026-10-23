"""Клиент: меню, список сетевых игр и сама игра — всё в одном окне.

Экраны переключаются внутри главного окна:

    меню ── соло ──────────────┐
      │                        │
      └── сетевые игры ────────┼── игра ── (← меню)
             │  ↑ обновить    │
             └── создать ─────┘

Всё поле в окно не влезает (и не надо), поэтому клиент показывает
небольшое окно и просит у сервера только его.
"""

from __future__ import annotations

import json
import os
import queue
import socket
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, simpledialog, ttk

from . import saves
from .protocol import DEFAULT_PORT, FIELD_SIZE, discover, lan_ip
from .sounds import SoundPlayer

CELL_MIN = 10
CELL_MAX = 48
CELL_DEFAULT = 26

#: автопрокрутка у края окна, как в RTS: зона в пикселях и шаг таймера
#: (зона в три раза больше прежней — курсор ловит её заметно раньше)
EDGE_ZONE = 84
EDGE_FAST = 10
EDGE_TICK = 33

BG = "#0d1117"
FG = "#c9d6e3"
PANEL = "#161b22"
CARD = "#151c25"
MUTED = "#7d8b99"
ACCENT = "#4dabf7"
DANGER = "#ff8787"

#: заполненность блока поля на миникарте: 0 (пусто) … 9 (открыто всё)
MAP_RAMP = (
    "#0f161d", "#17222c", "#1d2d3a", "#244055", "#2b5372",
    "#31678c", "#3b7fae", "#4e96c9", "#6fb2e0", "#a3d4f5",
)
MAP_LEVELS = {str(i): color for i, color in enumerate(MAP_RAMP)}
MAP_DARK_A = "#141c25"
MAP_DARK_B = "#101720"
MAP_VIEW = "#ffd43b"

COL_OUT = "#0d1117"
COL_GRID = "#0b1016"
COL_HIDDEN_A = "#3a4b5c"
COL_HIDDEN_B = "#334252"
COL_FLAG = "#e8590c"
COL_OPEN_A = "#aab8c5"
COL_OPEN_B = "#a2b1bf"
COL_BLAST = "#c92a2a"

NUM_COLORS = {
    1: "#1971c2",
    2: "#2b8a3e",
    3: "#c92a2a",
    4: "#5f3dc4",
    5: "#a61e4d",
    6: "#0b7285",
    7: "#212529",
    8: "#495057",
}


# ----------------------------------------------------------------------
# настройки (ник, последний адрес)
# ----------------------------------------------------------------------
def config_path() -> Path:
    base = os.environ.get("APPDATA") or os.environ.get("XDG_CONFIG_HOME") or str(Path.home())
    return Path(base) / "minesweeper-lan.json"


#: что хранится в настройках кроме ника и адреса
DEFAULT_SETTINGS = {"sound": True, "volume": 70, "minimap": True}


def load_settings() -> dict:
    try:
        with open(config_path(), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_settings(data: dict) -> None:
    try:
        path = config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
    except OSError:
        pass


def clean_name(value: str, fallback: str) -> str:
    name = " ".join(str(value).split())[:20].strip()
    return name or fallback


# ----------------------------------------------------------------------
# сеть
# ----------------------------------------------------------------------
class JoinDenied(Exception):
    """Сервер отказал во входе (заполнено / пароль)."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message


class NetClient(threading.Thread):
    """Подключается, делает рукопожатие и читает строки JSON из сокета."""

    def __init__(self, host: str, port: int, name: str, password: str = "") -> None:
        super().__init__(daemon=True, name="net")
        self.host = host
        self.port = port
        self.name = name
        self.queue: queue.Queue[dict] = queue.Queue()
        self.sock = socket.create_connection((host, port), timeout=8)
        self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self._stream = self.sock.makefile("rb")
        self._lock = threading.Lock()
        self.send({"t": "hello", "name": name, "password": password})
        # рукопожатие синхронное: сразу понятно, пустят или нет
        line = self._stream.readline()
        if not line:
            self.sock.close()
            raise JoinDenied("closed", "Сервер закрыл соединение")
        try:
            answer = json.loads(line)
        except ValueError:
            self.sock.close()
            raise JoinDenied("protocol", "Сервер ответил непонятно") from None
        if answer.get("t") == "denied":
            self.sock.close()
            raise JoinDenied(answer.get("reason", "denied"), answer.get("message", "Вход запрещён"))
        if answer.get("t") != "welcome":
            self.sock.close()
            raise JoinDenied("protocol", "Сервер ответил непонятно")
        self.welcome = answer
        self.sock.settimeout(None)

    def send(self, message: dict) -> None:
        data = (json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8")
        try:
            with self._lock:
                self.sock.sendall(data)
        except OSError:
            pass

    def run(self) -> None:
        try:
            for raw in self._stream:
                try:
                    message = json.loads(raw)
                except (ValueError, UnicodeDecodeError):
                    continue
                if isinstance(message, dict):
                    self.queue.put(message)
        except OSError:
            pass
        finally:
            self.queue.put({"t": "_closed"})

    def close(self) -> None:
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass


# ----------------------------------------------------------------------
# мелкие конструкторы виджетов
# ----------------------------------------------------------------------
def _button(parent, text, command, primary=False, width=24) -> tk.Button:
    return tk.Button(
        parent,
        text=text,
        command=command,
        width=width,
        bg=ACCENT if primary else "#233040",
        fg="#06121d" if primary else FG,
        activebackground="#74c0fc" if primary else "#2d3d4f",
        activeforeground="#06121d" if primary else FG,
        bd=0,
        relief="flat",
        highlightthickness=0,
        padx=14,
        pady=10,
        cursor="hand2",
        font=("Segoe UI", 11, "bold" if primary else "normal"),
    )


def _entry(parent, variable, show=None, width=26) -> tk.Entry:
    return tk.Entry(
        parent,
        textvariable=variable,
        show=show,
        width=width,
        bg="#0b0f14",
        fg=FG,
        insertbackground=FG,
        disabledbackground="#0b0f14",
        bd=0,
        relief="flat",
        highlightthickness=1,
        highlightbackground="#243040",
        highlightcolor=ACCENT,
        font=("Segoe UI", 11),
    )


def _label(parent, text, size=11, bold=False, fg=FG, bg=BG) -> tk.Label:
    return tk.Label(
        parent, text=text, bg=bg, fg=fg,
        font=("Segoe UI", size, "bold" if bold else "normal"), anchor="w",
    )


def _check(parent, text, variable, command) -> tk.Checkbutton:
    return tk.Checkbutton(
        parent, text=text, variable=variable, command=command,
        bg=BG, fg=FG, activebackground=BG, activeforeground=FG,
        selectcolor="#0b0f14", bd=0, highlightthickness=0,
        font=("Segoe UI", 11), anchor="w", cursor="hand2", padx=0,
    )


# ----------------------------------------------------------------------
# экран: главное меню
# ----------------------------------------------------------------------
class MenuScreen(tk.Frame):
    def __init__(self, master, app: "App", message: str = "") -> None:
        super().__init__(master, bg=BG)
        self.app = app
        box = tk.Frame(self, bg=BG)
        box.place(relx=0.5, rely=0.5, anchor="center")

        _label(box, "САПЁР", 44, True).pack()
        _label(box, "500 × 500 — 250 тысяч клеток", 12, fg=MUTED).pack(pady=(0, 26))

        _button(box, "Играть соло", self._solo, primary=True).pack(fill="x", pady=5)
        _button(box, "Играть по сети", app.show_servers).pack(fill="x", pady=5)
        _button(box, "Продолжить", app.show_saves).pack(fill="x", pady=5)
        _button(box, "Настройки", app.open_settings).pack(fill="x", pady=5)
        _button(box, "Выйти", app.quit).pack(fill="x", pady=5)

        if message:
            _label(box, message, 10, fg=DANGER).pack(pady=(16, 0))

        nick = tk.Frame(box, bg=BG)
        nick.pack(fill="x", pady=(22, 0))
        _label(nick, "Никнейм", 10, fg=MUTED, bg=BG).pack(side="left")
        self.name_var = tk.StringVar(value=app.name)
        entry = _entry(nick, self.name_var, width=20)
        entry.pack(side="left", fill="x", expand=True, padx=(8, 0), ipady=3)
        entry.bind("<KeyRelease>", self._nick_changed)
        entry.bind("<Return>", lambda event: self._nick_save())
        entry.bind("<FocusOut>", lambda event: self._nick_save())

    def _solo(self) -> None:
        self._nick_save()
        self.app.start_solo()

    def _nick_changed(self, event=None) -> None:
        value = self.name_var.get()
        if len(value) > 20:
            value = value[:20]
            self.name_var.set(value)
        self.app.name = clean_name(value, self.app.name)

    def _nick_save(self) -> None:
        self._nick_changed()
        self.app.save_name()


# ----------------------------------------------------------------------
# окно: настройки
# ----------------------------------------------------------------------
class SettingsDialog(tk.Toplevel):
    """Звук, громкость и миникарта. Всё сохраняется сразу."""

    def __init__(self, master, app: "App") -> None:
        super().__init__(master, bg=BG)
        self.app = app
        self.title("Настройки")
        self.transient(master)
        self.resizable(False, False)
        self.configure(padx=26, pady=20)

        _label(self, "Настройки", 20, True).pack(anchor="w")
        _label(self, "Изменения сохраняются сразу", 9, fg=MUTED).pack(
            anchor="w", pady=(0, 16)
        )

        self.sound_var = tk.BooleanVar(value=bool(app.settings.get("sound", True)))
        _check(self, "Звуки", self.sound_var, self._apply).pack(anchor="w")

        row = tk.Frame(self, bg=BG)
        row.pack(fill="x", pady=(10, 0))
        _label(row, "Громкость", 10, fg=MUTED, bg=BG).pack(side="left")
        self.volume_var = tk.IntVar(value=int(app.settings.get("volume", 70)))
        self.scale = tk.Scale(
            row, from_=0, to=100, orient="horizontal", variable=self.volume_var,
            command=self._volume, showvalue=False, length=180, width=16,
            bg=ACCENT, fg=FG, troughcolor="#243040", activebackground="#74c0fc",
            highlightthickness=0, bd=0, sliderrelief="flat", sliderlength=18,
            font=("Segoe UI", 9), takefocus=True,
        )
        self.scale.pack(side="left", fill="x", expand=True, padx=(10, 0))
        self.scale.bind("<ButtonRelease-1>", self._volume_done)
        self.volume_view = _label(row, f"{self.volume_var.get()}%", 10, fg=FG, bg=BG)
        self.volume_view.pack(side="left", padx=(10, 0))

        self.map_var = tk.BooleanVar(value=bool(app.settings.get("minimap", True)))
        _check(self, "Показывать миникарту", self.map_var, self._apply).pack(
            anchor="w", pady=(10, 0)
        )

        _label(
            self, f"Сохранения: {app.saves_dir}", 9, fg="#5c6a78",
        ).pack(anchor="w", pady=(18, 0))

        foot = tk.Frame(self, bg=BG)
        foot.pack(fill="x", pady=(16, 0))
        _button(foot, "Проверить звук", self._sample, width=15).pack(side="left")
        _button(foot, "Готово", self.destroy, primary=True, width=10).pack(side="right")

        self.bind("<Escape>", lambda event: self.destroy())
        self._center()
        try:
            self.grab_set()
        except tk.TclError:
            pass
        self.focus_set()

    # -- поведение ------------------------------------------------------
    def _center(self) -> None:
        self.update_idletasks()
        width, height = self.winfo_width(), self.winfo_height()
        parent = self.master
        x = parent.winfo_rootx() + (parent.winfo_width() - width) // 2
        y = parent.winfo_rooty() + (parent.winfo_height() - height) // 2
        self.geometry(f"+{max(0, x)}+{max(0, y)}")

    def _sample(self) -> None:
        self.app.sounds.play("flag", gap=0.0)

    def _volume(self, value) -> None:
        # во время перетаскивания только применяем, файл настроек не трогаем
        self.app.settings["volume"] = int(float(value))
        self.volume_view.configure(text=f"{int(float(value))}%")
        self.app.sounds.configure(
            bool(self.sound_var.get()), int(self.app.settings["volume"])
        )

    def _volume_done(self, event=None) -> None:
        self.app.settings["volume"] = int(self.volume_var.get())
        self.app.persist_settings()
        self._sample()

    def _apply(self) -> None:
        self.app.settings["sound"] = bool(self.sound_var.get())
        self.app.settings["minimap"] = bool(self.map_var.get())
        self.app.persist_settings()
        screen = self.app.screen
        if isinstance(screen, GameScreen):
            screen.set_map_visible(bool(self.map_var.get()))


# ----------------------------------------------------------------------
# экран: сохранённые партии
# ----------------------------------------------------------------------
class SavesScreen(tk.Frame):
    def __init__(self, master, app: "App") -> None:
        super().__init__(master, bg=BG)
        self.app = app
        self.items: dict[str, dict] = {}

        head = tk.Frame(self, bg=BG)
        head.pack(fill="x", padx=28, pady=(22, 6))
        _label(head, "Продолжить игру", 22, True).pack(anchor="w")
        self.status = _label(head, "", 10, fg=MUTED)
        self.status.pack(anchor="w", pady=(2, 0))

        body = tk.Frame(self, bg=BG)
        body.pack(fill="both", expand=True, padx=28, pady=(10, 0))
        ServersScreen._style()
        self.tree = ttk.Treeview(
            body,
            columns=("name", "date", "field", "found", "mines", "players"),
            show="headings",
            selectmode="browse",
            style="Games.Treeview",
        )
        for column, title, width, anchor in (
            ("name", "Партия", 290, "w"),
            ("date", "Когда", 145, "center"),
            ("field", "Поле", 110, "center"),
            ("found", "Открыто", 110, "center"),
            ("mines", "Мин", 80, "center"),
            ("players", "Игроки", 90, "center"),
        ):
            self.tree.heading(column, text=title, anchor=anchor)
            self.tree.column(column, width=width, anchor=anchor,
                             stretch=(column == "name"))
        self.tree.pack(fill="both", expand=True)
        self.tree.bind("<Double-1>", self._load_selected)
        self.tree.bind("<Return>", self._load_selected)

        foot = tk.Frame(self, bg=BG)
        foot.pack(fill="x", padx=28, pady=16)
        _button(foot, "Назад", app.show_menu, width=12).pack(side="left")
        _button(foot, "Удалить", self._delete, width=12).pack(side="left", padx=(10, 0))
        _button(foot, "Загрузить", self._load_selected, primary=True, width=14).pack(
            side="right"
        )

        hint = _label(self, "двойной клик по партии — продолжить", 9, fg="#5c6a78")
        hint.pack(anchor="w", padx=30, pady=(0, 14))

        self.refresh()
        self.tree.focus_set()

    # -- список ---------------------------------------------------------
    def refresh(self) -> None:
        found = saves.list_saves(self.app.saves_dir)
        self.tree.delete(*self.tree.get_children())
        self.items.clear()
        for meta in found:
            iid = self.tree.insert(
                "", "end",
                values=(
                    meta["name"],
                    saves.human_time(meta["saved"]),
                    f"{meta['w']} × {meta['h']}",
                    f"{meta['found']:,}".replace(",", " "),
                    f"{meta['mines']:,}".replace(",", " "),
                    "соло" if meta["max_players"] <= 1
                    else f"до {meta['max_players']}",
                ),
            )
            self.items[iid] = meta
        if found:
            first = self.tree.get_children()[0]
            self.tree.selection_set(first)
            self.tree.focus(first)
            self.status.configure(
                text=f"Сохранений: {len(found)} · папка {self.app.saves_dir}", fg=MUTED
            )
        else:
            self.status.configure(
                text="Сохранений пока нет: партию можно сохранить из игры кнопкой «Сохранить»",
                fg=MUTED,
            )

    def _selected(self) -> dict | None:
        selection = self.tree.selection()
        if not selection:
            return None
        return self.items.get(selection[0])

    # -- действия -------------------------------------------------------
    def _load_selected(self, event=None) -> None:
        meta = self._selected()
        if meta is None:
            return
        error = self.app.load_save(meta)
        if error:
            self.status.configure(text=error, fg=DANGER)

    def _delete(self) -> None:
        meta = self._selected()
        if meta is None:
            return
        if not messagebox.askyesno(
            "Удалить сохранение",
            f"Удалить «{meta['name']}»? Это действие нельзя отменить.",
            parent=self.app.root,
        ):
            return
        saves.delete_save(meta["path"])
        self.refresh()


# ----------------------------------------------------------------------
# экран: список сетевых игр
# ----------------------------------------------------------------------
class ServersScreen(tk.Frame):
    def __init__(self, master, app: "App", error: str = "") -> None:
        super().__init__(master, bg=BG)
        self.app = app
        self.games: dict[str, dict] = {}
        self._results: queue.Queue = queue.Queue()
        self._scan_token = 0
        self._timer: str | None = None

        head = tk.Frame(self, bg=BG)
        head.pack(fill="x", padx=28, pady=(22, 6))
        _label(head, "Сетевые игры", 22, True).pack(anchor="w")
        self.status = _label(
            head,
            error or "Сканирую локальную сеть…",
            10,
            fg=DANGER if error else MUTED,
        )
        self.status.pack(anchor="w", pady=(2, 0))

        body = tk.Frame(self, bg=BG)
        body.pack(fill="both", expand=True, padx=28, pady=(10, 0))

        self._style()
        self.tree = ttk.Treeview(
            body,
            columns=("name", "players", "field", "lock"),
            show="headings",
            selectmode="browse",
            style="Games.Treeview",
        )
        for column, title, width, anchor in (
            ("name", "Игра", 380, "w"),
            ("players", "Игроки", 110, "center"),
            ("field", "Поле", 170, "center"),
            ("lock", "", 50, "center"),
        ):
            self.tree.heading(column, text=title, anchor=anchor)
            self.tree.column(column, width=width, anchor=anchor,
                             stretch=(column == "name"))
        self.tree.pack(fill="both", expand=True)
        self.tree.bind("<Double-1>", self._join_selected)
        self.tree.bind("<Return>", self._join_selected)

        foot = tk.Frame(self, bg=BG)
        foot.pack(fill="x", padx=28, pady=16)
        _button(foot, "Назад", app.show_menu, width=12).pack(side="left")
        _button(foot, "Прямое подключение", app.show_direct, width=19).pack(
            side="left", padx=(10, 0)
        )
        _button(foot, "Создать игру", app.show_create, primary=True, width=16).pack(side="right")
        _button(foot, "Обновить", self.refresh, width=14).pack(side="right", padx=(0, 10))

        hint = _label(self, "двойной клик по игре — подключиться, 🔒 — нужен пароль", 9,
                      fg="#5c6a78")
        hint.pack(anchor="w", padx=30, pady=(0, 14))

        self.refresh()
        self.tree.focus_set()

    @staticmethod
    def _style() -> None:
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure(
            "Games.Treeview",
            background="#121820",
            fieldbackground="#121820",
            foreground=FG,
            borderwidth=0,
            rowheight=30,
            font=("Segoe UI", 10),
        )
        style.configure(
            "Games.Treeview.Heading",
            background="#1b2530",
            foreground="#8fa3b8",
            borderwidth=0,
            padding=(6, 8),
            font=("Segoe UI", 9, "bold"),
        )
        style.map(
            "Games.Treeview",
            background=[("selected", "#1f3a5f")],
            foreground=[("selected", "#ffffff")],
        )
        style.map("Games.Treeview.Heading", background=[("active", "#22303e")])
        # убираем рамку вокруг списка: остаётся ровный тёмный прямоугольник
        try:
            style.layout("Games.Treeview", [("Games.Treeview.treearea", {"sticky": "nswe"})])
        except tk.TclError:
            pass

    # -- сканирование ---------------------------------------------------
    def refresh(self) -> None:
        self._scan_token += 1
        token = self._scan_token
        self.status.configure(text="Сканирую локальную сеть…", fg=MUTED)

        def work() -> None:
            found = discover(1.5)
            self._results.put((token, found))

        threading.Thread(target=work, daemon=True, name="scan").start()
        self._poll_results()

    def _poll_results(self) -> None:
        self._timer = None
        if not self.winfo_exists():
            return
        try:
            token, found = self._results.get_nowait()
        except queue.Empty:
            self._timer = self.app.root.after(80, self._poll_results)
            return
        if token != self._scan_token:
            return
        self._apply(found)

    def destroy(self) -> None:
        if self._timer is not None:
            try:
                self.app.root.after_cancel(self._timer)
            except tk.TclError:
                pass
            self._timer = None
        super().destroy()

    def _apply(self, found: list[dict]) -> None:
        for item in self.tree.get_children():
            self.tree.delete(item)
        self.games.clear()
        found.sort(key=lambda info: (-int(info.get("players", 0)), info.get("name", "")))
        for info in found:
            iid = f"{info['host']}:{info.get('port')}"
            self.games[iid] = info
            field = f"{info.get('w', '?')} × {info.get('h', '?')}"
            players = f"{info.get('players', 0)} / {info.get('max_players', '?')}"
            self.tree.insert(
                "", "end", iid=iid,
                values=(info.get("name", "игра"), players, field,
                        "🔒" if info.get("locked") else ""),
            )
        if found:
            self.status.configure(text=f"Найдено игр: {len(found)}", fg=MUTED)
        else:
            self.status.configure(
                text="Игр не найдено. Создайте свою или нажмите «Обновить».", fg=MUTED
            )

    # -- подключение ----------------------------------------------------
    def _join_selected(self, event=None) -> None:
        selection = self.tree.selection()
        if not selection:
            return
        info = self.games.get(selection[0])
        if info is None:
            return
        if info.get("locked"):
            self.app.ask_password(info)
            return
        error = self.app.join(info, "")
        if error:
            self.status.configure(text=error, fg=DANGER)


# ----------------------------------------------------------------------
# экран: создание игры
# ----------------------------------------------------------------------
class CreateScreen(tk.Frame):
    def __init__(self, master, app: "App") -> None:
        super().__init__(master, bg=BG)
        self.app = app

        card = tk.Frame(self, bg=CARD, padx=34, pady=28,
                        highlightthickness=1, highlightbackground="#243040")
        card.place(relx=0.5, rely=0.5, anchor="center")

        _label(card, "Создать игру", 22, True, bg=CARD).grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, 4)
        )
        _label(card, "Игра появится в списке у всех в локальной сети", 10,
               fg=MUTED, bg=CARD).grid(row=1, column=0, columnspan=2, sticky="w", pady=(0, 22))

        self.name_var = tk.StringVar(value=f"Игра {app.name}")
        self.max_var = tk.StringVar(value="8")
        self.pass_var = tk.StringVar()

        rows = (
            ("Название игры", _entry(card, self.name_var)),
            ("Макс. игроков", _entry(card, self.max_var, width=8)),
            ("Пароль (необязательно)", _entry(card, self.pass_var, show="•")),
        )
        for offset, (title, widget) in enumerate(rows):
            row = 2 + offset * 2
            _label(card, title, 10, fg=MUTED, bg=CARD).grid(
                row=row, column=0, columnspan=2, sticky="w", pady=(0, 4)
            )
            widget.grid(row=row + 1, column=0, columnspan=2, sticky="we",
                        ipady=5, pady=(0, 14))
            widget.bind("<Return>", lambda event: self._create())
        first_entry = rows[0][1]

        self.error = _label(card, "", 10, fg=DANGER, bg=CARD)
        self.error.grid(row=8, column=0, columnspan=2, sticky="w")

        buttons = tk.Frame(card, bg=CARD)
        buttons.grid(row=9, column=0, columnspan=2, sticky="we", pady=(18, 0))
        _button(buttons, "Назад", app.show_servers, width=12).pack(side="left")
        _button(buttons, "Создать", self._create, primary=True, width=14).pack(side="right")

        first_entry.bind("<Map>", lambda event: first_entry.focus_set(), add="+")

    def _create(self) -> None:
        name = self.name_var.get().strip() or f"Игра {self.app.name}"
        try:
            max_players = int(self.max_var.get().strip())
        except ValueError:
            self.error.configure(text="Максимум игроков — целое число 1…32")
            return
        if not 1 <= max_players <= 32:
            self.error.configure(text="Максимум игроков — от 1 до 32")
            return
        self.error.configure(text="Создаю поле…")
        self.update_idletasks()
        error = self.app.host_game(name, max_players, self.pass_var.get().strip())
        if error:
            self.error.configure(text=error)


# ----------------------------------------------------------------------
# экран: ввод пароля
# ----------------------------------------------------------------------
class PasswordScreen(tk.Frame):
    def __init__(self, master, app: "App", info: dict) -> None:
        super().__init__(master, bg=BG)
        self.app = app
        self.info = info

        card = tk.Frame(self, bg=CARD, padx=34, pady=28,
                        highlightthickness=1, highlightbackground="#243040")
        card.place(relx=0.5, rely=0.5, anchor="center")

        _label(card, "🔒  " + str(info.get("name", "игра")), 20, True, bg=CARD).pack(anchor="w")
        details = (
            f"{info.get('players', 0)} / {info.get('max_players', '?')} игроков · "
            f"поле {info.get('w', '?')} × {info.get('h', '?')}"
        )
        _label(card, details, 10, fg=MUTED, bg=CARD).pack(anchor="w", pady=(2, 18))

        self.pass_var = tk.StringVar()
        entry = _entry(card, self.pass_var, show="•", width=30)
        entry.pack(fill="x", ipady=5)
        entry.bind("<Return>", lambda e: self._submit())
        entry.bind("<Map>", lambda event: entry.focus_set(), add="+")
        self.error = _label(card, "", 10, fg=DANGER, bg=CARD)
        self.error.pack(anchor="w", pady=(8, 0))

        buttons = tk.Frame(card, bg=CARD)
        buttons.pack(fill="x", pady=(18, 0))
        _button(buttons, "Назад", app.show_servers, width=12).pack(side="left")
        _button(buttons, "Войти", self._submit, primary=True, width=12).pack(
            side="right", padx=(10, 0)
        )

    def _submit(self) -> None:
        self.error.configure(text="Подключаюсь…")
        self.update_idletasks()
        error = self.app.join(self.info, self.pass_var.get())
        if error:
            self.error.configure(text=error)


# ----------------------------------------------------------------------
# миникарта
# ----------------------------------------------------------------------
class Minimap(tk.Frame):
    """Миниатюра всего поля: что открыто, где ваш экран и чужие курсоры.

    Клик по миниатюре переносит камеру в это место.
    """

    def __init__(self, master, on_jump) -> None:
        super().__init__(master, bg="#05090d", highlightthickness=1,
                         highlightbackground="#243040", bd=0)
        self.on_jump = on_jump
        self.size = 160
        self.canvas = tk.Canvas(self, width=self.size, height=self.size, bg="#0a0f15",
                                highlightthickness=0, bd=0, cursor="crosshair")
        self.canvas.pack()
        self.photo = tk.PhotoImage(width=self.size, height=self.size)
        self.image = self.canvas.create_image(0, 0, image=self.photo, anchor="nw")
        self.view_box = self.canvas.create_rectangle(0, 0, 2, 2, outline=MAP_VIEW, width=1)
        self.dots: dict[int, int] = {}
        self.board_w = FIELD_SIZE
        self.board_h = FIELD_SIZE
        self._painted: list[str] = [""] * self.size
        self.canvas.bind("<Button-1>", self._clicked)

    def set_overview(self, rows: list[str]) -> None:
        if not rows:
            return
        size = len(rows)
        if size != self.size:
            self.size = size
            self.photo = tk.PhotoImage(width=size, height=size)
            self.canvas.itemconfigure(self.image, image=self.photo)
            self.canvas.configure(width=size, height=size)
            self._painted = [""] * size
        for y, row in enumerate(rows):
            if row == self._painted[y]:
                continue
            self._painted[y] = row
            shade_a, shade_b = (MAP_DARK_A, MAP_DARK_B) if (y >> 3) & 1 else (MAP_DARK_B, MAP_DARK_A)
            pixels = " ".join(
                MAP_LEVELS[ch] if ch != "0" else (shade_a if (x >> 3) & 1 else shade_b)
                for x, ch in enumerate(row)
            )
            self.photo.put("{" + pixels + "}", to=(0, y))

    def update_view(self, cam_x: int, cam_y: int, cols: int, rows: int,
                    board_w: int, board_h: int, players: list[dict], my_id: int) -> None:
        self.board_w = max(1, board_w)
        self.board_h = max(1, board_h)
        sx = self.size / self.board_w
        sy = self.size / self.board_h
        # сам экран на карте занимает доли пикселя — рисуем заметный маркер
        w = max(5.0, cols * sx)
        h = max(5.0, rows * sy)
        x0 = min(max(0.0, (cam_x + cols / 2) * sx - w / 2), self.size - w)
        y0 = min(max(0.0, (cam_y + rows / 2) * sy - h / 2), self.size - h)
        self.canvas.coords(self.view_box, x0, y0, x0 + w, y0 + h)
        alive = set()
        for player in players:
            pid = player.get("id")
            if pid is None:
                continue
            # где игрок: последний курсор, а если он не двигал мышью — центр его окна
            pos = player.get("cursor")
            if not pos:
                view = player.get("view")
                if view:
                    pos = (view[0] + view[2] // 2, view[1] + view[3] // 2)
                elif pid == my_id:
                    pos = (cam_x + cols // 2, cam_y + rows // 2)
            if not pos:
                continue
            px = int(pos[0] * sx)
            py = int(pos[1] * sy)
            color = player.get("color", ACCENT)
            item = self.dots.get(pid)
            if item is None:
                item = self.canvas.create_rectangle(
                    0, 0, 5, 5, fill=color,
                    outline=FG if pid == my_id else "#05090d", width=1,
                )
                self.dots[pid] = item
            self.canvas.coords(item, px - 2, py - 2, px + 2, py + 2)
            self.canvas.itemconfigure(item, fill=color)
            alive.add(pid)
        for pid in list(self.dots):
            if pid not in alive:
                self.canvas.delete(self.dots.pop(pid))
        self.canvas.tag_raise(self.view_box)
        for item in self.dots.values():
            self.canvas.tag_raise(item)

    def _clicked(self, event) -> None:
        x = int(event.x / self.size * self.board_w)
        y = int(event.y / self.size * self.board_h)
        self.on_jump(x, y)


# ----------------------------------------------------------------------
# экран: прямое подключение по адресу
# ----------------------------------------------------------------------
class DirectScreen(tk.Frame):
    def __init__(self, master, app: "App") -> None:
        super().__init__(master, bg=BG)
        self.app = app

        card = tk.Frame(self, bg=CARD, padx=34, pady=28,
                        highlightthickness=1, highlightbackground="#243040")
        card.place(relx=0.5, rely=0.5, anchor="center")

        _label(card, "Прямое подключение", 20, True, bg=CARD).pack(anchor="w")
        _label(card, "Если игра не нашлась автопоиском — введите адрес вручную", 10,
               fg=MUTED, bg=CARD).pack(anchor="w", pady=(2, 18))

        self.host_var = tk.StringVar(value=app.last_host or f"127.0.0.1:{app.port}")
        _label(card, "Адрес (IP или IP:порт)", 10, fg=MUTED, bg=CARD).pack(anchor="w")
        host = _entry(card, self.host_var, width=30)
        host.pack(fill="x", ipady=5, pady=(4, 14))

        self.pass_var = tk.StringVar()
        _label(card, "Пароль (если игра закрыта паролем)", 10, fg=MUTED, bg=CARD).pack(anchor="w")
        password = _entry(card, self.pass_var, show="•", width=30)
        password.pack(fill="x", ipady=5, pady=(4, 12))

        self.error = _label(card, "", 10, fg=DANGER, bg=CARD)
        self.error.pack(anchor="w")

        buttons = tk.Frame(card, bg=CARD)
        buttons.pack(fill="x", pady=(18, 0))
        _button(buttons, "Назад", app.show_servers, width=12).pack(side="left")
        _button(buttons, "Подключиться", self._connect, primary=True, width=16).pack(
            side="right", padx=(10, 0)
        )

        host.bind("<Return>", lambda event: self._connect())
        password.bind("<Return>", lambda event: self._connect())
        host.bind("<Map>", lambda event: host.focus_set(), add="+")

    def _connect(self) -> None:
        raw = self.host_var.get().strip()
        if not raw:
            self.error.configure(text="Введите адрес, например 192.168.1.10:5555")
            return
        if raw.count(":") > 1:
            self.error.configure(text="Нужен адрес IPv4, например 192.168.1.10")
            return
        host, port = raw, self.app.port
        if ":" in raw:
            head, tail = raw.split(":")
            host, tail = head.strip(), tail.strip()
            if tail.isdigit():
                port = int(tail)
        if not host:
            self.error.configure(text="Введите адрес, например 192.168.1.10:5555")
            return

        self.error.configure(text="Подключаюсь…")
        self.update_idletasks()
        error = self.app.join(
            {"host": host, "port": port, "name": f"{host}:{port}"},
            self.pass_var.get(),
        )
        if error:
            self.error.configure(text=error)
            return
        self.app.remember_host(raw)


# ----------------------------------------------------------------------
# экран: игра
# ----------------------------------------------------------------------
class GameScreen(tk.Frame):
    KEYS: list[tuple[str, str]] = [
        ("<Left>", "pan -1 0"), ("<Right>", "pan 1 0"),
        ("<Up>", "pan 0 -1"), ("<Down>", "pan 0 1"),
        ("<a>", "pan -1 0"), ("<d>", "pan 1 0"),
        ("<w>", "pan 0 -1"), ("<s>", "pan 0 1"),
        ("<plus>", "zoom 1"), ("<equal>", "zoom 1"), ("<KP_Add>", "zoom 1"),
        ("<minus>", "zoom -1"), ("<KP_Subtract>", "zoom -1"),
        ("<Prior>", "page -1"), ("<Next>", "page 1"),
        ("<Home>", "home"), ("<r>", "restart"), ("<g>", "jump"), ("<m>", "map"),
        ("<Return>", "chat"), ("<Escape>", "blur"),
    ]

    def __init__(self, master, app: "App", net: NetClient, note: str,
                 restore: dict | None = None) -> None:
        super().__init__(master, bg=BG)
        self.app = app
        self.root = app.root
        self.net = net
        self.note = note
        self.restore = restore
        self.sounds = app.sounds
        self.running = True
        self._closed = False

        self.board_w = FIELD_SIZE
        self.board_h = FIELD_SIZE
        self.total_mines = 0
        self.my_id = 0
        self.my_name = net.name
        self.my_color = ACCENT
        self.is_host = True
        self.players: list[dict] = []
        self.cells: dict[tuple[int, int], str] = {}
        self.ready = False

        self.cs = CELL_DEFAULT
        self.cam_x = self.board_w // 2
        self.cam_y = self.board_h // 2
        self.cols = 38
        self.rows = 22
        self.built_key: tuple | None = None
        self.slots: list[list[tuple[int, int] | None]] = []
        self.shown: list[list[str]] = []
        self._font = ("Consolas", 14, "bold")
        self.cursor_items: dict[int, tuple[int, int]] = {}
        self.overlay: list[int] = []
        self.dead = False
        self.game_over = False
        self.found = 0
        self.safe_total = 0
        self._map_found = -1
        self._dirty = False

        self._last_view_sent = 0.0
        self._last_cursor = 0.0
        self.hover = (0, 0)
        self._drag: tuple[int, int, int, int] | None = None
        self._edge = (0, 0)
        self._edge_speed = 1
        self._edge_timer: str | None = None

        self._build_ui()
        self._bind_keys()
        self._on_welcome(net.welcome)

        self._timer_poll = self.root.after(16, self._poll)
        self._timer_watch = self.root.after(250, self._watch_size)
        self._timer_map = self.root.after(600, self._ask_overview)
        self._timer_disconnect: str | None = None

    # -- построение -----------------------------------------------------
    def _build_ui(self) -> None:
        top = tk.Frame(self, bg=PANEL, padx=10, pady=6)
        top.pack(side="top", fill="x")
        _button(top, "← Меню", self._leave, width=9).pack(side="left", padx=(0, 10))
        _button(top, "Сохранить", self._save_game, width=11).pack(side="left", padx=(0, 6))
        _button(top, "⚙", self.app.open_settings, width=3).pack(side="left", padx=(0, 10))
        # счёт пакуется раньше строки состояния: иначе длинный список игроков
        # обрезается слева и «вы»/хозяин пропадают из виду
        self.score = tk.Label(top, text="", bg=PANEL, fg=ACCENT, anchor="e",
                              font=("Segoe UI", 10, "bold"))
        self.score.pack(side="right", padx=(12, 6))
        self.info = tk.Label(top, text="подключение…", bg=PANEL, fg=FG, anchor="w",
                             font=("Segoe UI", 10))
        self.info.pack(side="left", fill="x", expand=True)

        bottom = tk.Frame(self, bg=PANEL, padx=8, pady=6)
        bottom.pack(side="bottom", fill="x")

        self.hint = tk.Label(
            bottom,
            text="ЛКМ — открыть · ПКМ — флаг · колесо — масштаб · Shift+колесо/WASD — камера · "
                 "M — карта · G — переход · R — новая партия · Enter — чат",
            bg=PANEL, fg="#7d8b99", anchor="w", font=("Segoe UI", 9),
        )
        self.hint.pack(side="bottom", fill="x", pady=(4, 0))

        chat_row = tk.Frame(bottom, bg=PANEL)
        chat_row.pack(side="bottom", fill="x")
        self.log = tk.Text(chat_row, height=3, bg="#0b0f14", fg=FG, bd=0,
                           highlightthickness=0, font=("Consolas", 9), wrap="word",
                           state="disabled")
        self.log.pack(side="top", fill="both", expand=True)
        self.log.tag_configure("sys", foreground=MUTED)
        self.log.tag_configure("err", foreground=DANGER)
        self.chat_var = tk.StringVar()
        self.chat = tk.Entry(chat_row, textvariable=self.chat_var, bg="#0b0f14", fg=FG,
                             insertbackground=FG, bd=0, highlightthickness=0,
                             font=("Segoe UI", 10))
        self.chat.pack(side="top", fill="x", pady=(4, 0), ipady=4)
        self.chat.bind("<Return>", self._send_chat)
        self.chat.bind("<Escape>", lambda e: self.canvas.focus_set())

        self.canvas = tk.Canvas(self, bg=COL_OUT, highlightthickness=0, bd=0,
                                width=1000, height=600)
        self.canvas.pack(side="top", fill="both", expand=True)

        self.minimap = Minimap(self.canvas, self._jump)
        if self.app.settings.get("minimap", True):
            self.minimap.place(relx=1.0, rely=0.0, x=-12, y=12, anchor="ne")

        self.canvas.bind("<Button-1>", self._on_reveal)
        self.canvas.bind("<Button-3>", self._on_flag)
        self.canvas.bind("<Motion>", self._on_motion)
        self.canvas.bind("<Configure>", lambda e: self._request_view(force=True))
        self.canvas.bind("<MouseWheel>", self._on_wheel)
        self.canvas.bind("<Shift-MouseWheel>", self._on_wheel_h)
        self.canvas.bind("<Button-2>", self._on_drag_start)
        self.canvas.bind("<B2-Motion>", self._on_drag)
        self.canvas.bind("<ButtonRelease-2>", self._on_drag_end)
        self.canvas.bind("<Leave>", self._edge_stop)

        self._log("Добро пожаловать! " + self.note, "sys")

    def _bind_keys(self) -> None:
        for sequence, action in self.KEYS:
            self.root.bind(sequence, lambda e, action=action: self._action(action))

    def _unbind_keys(self) -> None:
        for sequence, _action in self.KEYS:
            self.root.unbind(sequence)

    def _action(self, action: str) -> None:
        if not self.running or self._typing():
            return
        if action.startswith("pan"):
            _, dx, dy = action.split()
            self._pan(int(dx) * 4, int(dy) * 4)
        elif action.startswith("zoom"):
            self._zoom(int(action.split()[1]))
        elif action.startswith("page"):
            self._pan(0, int(action.split()[1]) * self.rows * 2)
        elif action == "home":
            self._jump(self.board_w // 2, self.board_h // 2)
        elif action == "restart":
            if self.is_host:
                self.net.send({"t": "restart"})
            else:
                self._log("Новую партию запускает хозяин игры.", "err")
        elif action == "jump":
            self._ask_jump()
        elif action == "map":
            self._toggle_map()
        elif action == "chat":
            self.chat.focus_set()
        elif action == "blur":
            self.canvas.focus_set()

    # -- сеть → интерфейс -----------------------------------------------
    def _poll(self) -> None:
        if not self.running:
            return
        for _ in range(300):
            try:
                message = self.net.queue.get_nowait()
            except queue.Empty:
                break
            self._handle(message)
        # несколько ответов подряд (например, после серии кликов) дают один кадр
        if self._dirty and self.running:
            self._draw()
        if self.running:
            self._timer_poll = self.root.after(16, self._poll)

    def _ask_overview(self) -> None:
        # карта меняется только когда открывают клетки: лишний раз не просим
        if not self.running:
            return
        if self.ready and not self.game_over and self.found != self._map_found:
            self._map_found = self.found
            self.net.send({"t": "overview"})
        self._timer_map = self.root.after(800, self._ask_overview)

    def _watch_size(self) -> None:
        if not self.running:
            return
        if self.ready:
            width = self.canvas.winfo_width()
            height = self.canvas.winfo_height()
            if width > 1 and height > 1:
                cols = max(4, width // self.cs)
                rows = max(4, height // self.cs)
                if (cols, rows) != (self.cols, self.rows):
                    self._request_view(force=True)
        self._timer_watch = self.root.after(250, self._watch_size)

    def _handle(self, msg: dict) -> None:
        kind = msg.get("t")
        if kind == "view":
            self._on_view(msg)
        elif kind == "overview":
            rows = msg.get("rows") or []
            if rows:
                self.minimap.set_overview(rows)
                self._update_minimap()
        elif kind == "players":
            self.players = msg.get("players", [])
            for player in self.players:
                if player.get("id") == self.my_id:
                    self.my_color = player.get("color", self.my_color)
                    self.is_host = bool(player.get("host"))
            self._update_players_line()
            self._sync_cursors()
            self._update_minimap()
        elif kind == "event":
            self._on_event(msg)
        elif kind == "chat":
            self._log(f"{msg.get('name')}: {msg.get('text')}")
        elif kind == "info":
            if msg.get("text"):
                self._log(msg["text"], "sys")
        elif kind == "pong":
            pass
        elif kind == "_closed":
            if self._closed:
                return
            self._closed = True
            self.running = False
            self._log("Соединение с сервером потеряно.", "err")
            self._timer_disconnect = self.root.after(1200, self.app.on_disconnected)

    def _on_welcome(self, msg: dict) -> None:
        self.board_w = int(msg.get("w", FIELD_SIZE))
        self.board_h = int(msg.get("h", FIELD_SIZE))
        self.total_mines = int(msg.get("mines", 0))
        self.safe_total = self.board_w * self.board_h - self.total_mines
        self.my_id = int(msg.get("id", 0))
        self.is_host = bool(msg.get("host", True))
        self.my_name = msg.get("name", self.my_name)
        self.my_color = msg.get("color", ACCENT)
        self.ready = True
        self.app.root.title(f"Сапёр · {msg.get('server', 'игра')}")
        self._log(
            f"Игра «{msg.get('server')}»: поле {self.board_w}×{self.board_h}, "
            f"мин {self.total_mines:,}, игроков до {msg.get('max_players', '?')}. "
            "Старт в центре поля.",
            "sys",
        )
        if self.restore:
            # продолжение сохранённой партии: масштаб и место на поле
            plan, self.restore = self.restore, None
            self.cs = max(CELL_MIN, min(CELL_MAX, int(plan.get("cs") or self.cs)))
            cam = plan.get("cam") or (self.board_w // 2, self.board_h // 2)
            self.cam_x = int(cam[0]) - self.cols // 2
            self.cam_y = int(cam[1]) - self.rows // 2
        else:
            self.cam_x = self.board_w // 2 - self.cols // 2
            self.cam_y = self.board_h // 2 - self.rows // 2
        self.canvas.focus_set()
        self._request_view(force=True)

    def _on_view(self, msg: dict) -> None:
        x, y = int(msg["x"]), int(msg["y"])
        rows = msg["rows"]
        # не затираем кэш целиком: при панораме старые клетки ещё пригодятся,
        # пока не пришёл ответ на новый запрос
        cells = self.cells
        for j, row in enumerate(rows):
            for i, ch in enumerate(row):
                cells[(x + i, y + j)] = ch
        if len(cells) > 120_000:
            left, right = self.cam_x - self.cols, self.cam_x + self.cols * 2
            top, bottom = self.cam_y - self.rows, self.cam_y + self.rows * 2
            self.cells = {
                key: ch for key, ch in cells.items()
                if left <= key[0] <= right and top <= key[1] <= bottom
            }

        found = int(msg.get("found", 0))
        if found > self.found:
            self.sounds.play("open", gap=0.04)
        self.found = found
        self.safe_total = int(msg.get("safe", self.safe_total))
        self.total_mines = self.board_w * self.board_h - self.safe_total
        self.game_over = bool(msg.get("over"))
        self.dead = not bool(msg.get("alive", True))
        self._dirty = True
        self._update_info()

    def _on_event(self, msg: dict) -> None:
        kind = msg.get("kind")
        if kind == "join":
            self.sounds.play("chat")
            self._log(f"{msg.get('name')} подключился", "sys")
        elif kind == "leave":
            self._log(f"{msg.get('name')} вышел", "sys")
        elif kind == "boom":
            self.sounds.play("boom", gap=0.2)
            who = "Вы" if msg.get("name") == self.my_name else msg.get("name")
            self._log(f"💥 {who} подорвался на ({msg.get('x')}, {msg.get('y')})", "err")
        elif kind == "restart":
            self.sounds.play("restart")
            self._log(f"Новая партия — мин {msg.get('mines', 0):,}", "sys")
            self._clear_overlay()
            self.cells.clear()
            self.dead = False
            self.game_over = False
            self._map_found = -1
            self._dirty = True
        elif kind == "gameover":
            self._log(
                "Все подорвались — партия окончена. Новую запускает хозяин игры.",
                "err",
            )
        elif kind == "win":
            self.sounds.play("win", gap=1.0)
            self._log("Поле очищено! Победа.", "sys")

    # -- камера ---------------------------------------------------------
    def _request_view(self, force: bool = False) -> None:
        if not self.running or not self.ready:
            return
        now = time.time()
        if not force and now - self._last_view_sent < 0.03:
            return
        self._last_view_sent = now
        width = self.canvas.winfo_width()
        height = self.canvas.winfo_height()
        if width <= 1:
            width, height = 1000, 600
        self.cols = max(4, width // self.cs)
        self.rows = max(4, height // self.cs)
        self.cam_x = max(0, min(self.board_w - self.cols, self.cam_x))
        self.cam_y = max(0, min(self.board_h - self.rows, self.cam_y))
        self._ensure_grid()
        self.net.send({"t": "view", "x": self.cam_x, "y": self.cam_y,
                       "w": self.cols, "h": self.rows})

    def _ensure_grid(self) -> None:
        key = (self.cols, self.rows, self.cs)
        if key == self.built_key:
            return
        self.built_key = key
        self.canvas.delete("all")
        self.cursor_items.clear()
        self.overlay.clear()
        self._font = ("Consolas", max(7, int(self.cs * 0.60)), "bold")
        self.slots = [[None] * self.cols for _ in range(self.rows)]
        self.shown = [[""] * self.cols for _ in range(self.rows)]
        self._dirty = True

    def _slot(self, j: int, i: int) -> tuple[int, int]:
        items = self.slots[j][i]
        if items is None:
            cs = self.cs
            x0, y0 = i * cs, j * cs
            rect = self.canvas.create_rectangle(
                x0, y0, x0 + cs, y0 + cs, fill=COL_HIDDEN_A, outline=COL_GRID, tags="cell"
            )
            text = self.canvas.create_text(
                x0 + cs / 2, y0 + cs / 2, text="", fill=FG, font=self._font,
                state="hidden", tags="cell",
            )
            items = (rect, text)
            self.slots[j][i] = items
        return items

    def _draw(self) -> None:
        """Перерисовка окна: трогаем только те клетки, что изменились."""
        if not self.slots:
            return
        self._dirty = False
        self._paint()
        self._sync_cursors()
        self._draw_overlay()
        self._update_minimap()

    def _paint(self) -> None:
        """Красит только те клетки, что изменились с прошлого кадра."""
        canvas = self.canvas
        cells = self.cells
        board_w, board_h = self.board_w, self.board_h
        shown = self.shown
        for j in range(self.rows):
            by = self.cam_y + j
            row_shown = shown[j]
            for i in range(self.cols):
                bx = self.cam_x + i
                if 0 <= bx < board_w and 0 <= by < board_h:
                    ch = cells.get((bx, by), "?")
                else:
                    ch = " "
                if ch == row_shown[i]:
                    continue
                row_shown[i] = ch
                rect, text = self._slot(j, i)
                if ch == "?":
                    fill = COL_HIDDEN_A if (i + j) & 1 else COL_HIDDEN_B
                    canvas.itemconfigure(rect, fill=fill, outline=COL_GRID)
                    canvas.itemconfigure(text, state="hidden")
                elif ch == "F":
                    fill = COL_HIDDEN_A if (i + j) & 1 else COL_HIDDEN_B
                    canvas.itemconfigure(rect, fill=fill, outline=COL_GRID)
                    canvas.itemconfigure(text, text="F", fill=COL_FLAG, state="normal")
                elif ch == "*":
                    canvas.itemconfigure(rect, fill=COL_BLAST, outline=COL_GRID)
                    canvas.itemconfigure(text, text="✸", fill="#fff5f5", state="normal")
                elif ch == ".":
                    fill = COL_OPEN_A if (i + j) & 1 else COL_OPEN_B
                    canvas.itemconfigure(rect, fill=fill, outline=fill)
                    canvas.itemconfigure(text, state="hidden")
                elif ch.isdigit():
                    fill = COL_OPEN_A if (i + j) & 1 else COL_OPEN_B
                    canvas.itemconfigure(rect, fill=fill, outline=fill)
                    canvas.itemconfigure(text, text=ch, fill=NUM_COLORS.get(int(ch), FG),
                                         state="normal")
                else:
                    canvas.itemconfigure(rect, fill=COL_OUT, outline=COL_OUT)
                    canvas.itemconfigure(text, state="hidden")

    def _sync_cursors(self) -> None:
        cs = self.cs
        for player in self.players:
            pid = player.get("id")
            cursor = player.get("cursor")
            if pid == self.my_id or not cursor:
                continue
            cx, cy = cursor
            i, j = cx - self.cam_x, cy - self.cam_y
            if not (0 <= i < self.cols and 0 <= j < self.rows):
                if pid in self.cursor_items:
                    self.canvas.itemconfigure(self.cursor_items[pid][0], state="hidden")
                    self.canvas.itemconfigure(self.cursor_items[pid][1], state="hidden")
                continue
            color = player.get("color", ACCENT)
            name = player.get("name", "?")
            items = self.cursor_items.get(pid)
            if items is None:
                rect = self.canvas.create_rectangle(0, 0, 1, 1, outline=color, width=2,
                                                    tags="cursor")
                label = self.canvas.create_text(0, 0, text=name, fill=color, anchor="sw",
                                                font=("Segoe UI", 8, "bold"), tags="cursor")
                items = (rect, label)
                self.cursor_items[pid] = items
            x0, y0 = i * cs, j * cs
            self.canvas.coords(items[0], x0 + 1, y0 + 1, x0 + cs - 1, y0 + cs - 1)
            self.canvas.coords(items[1], x0 + 2, y0 - 1)
            self.canvas.itemconfigure(items[0], state="normal", outline=color)
            self.canvas.itemconfigure(items[1], state="normal", text=name, fill=color)

    def _draw_overlay(self) -> None:
        text = None
        color = DANGER
        if self.game_over:
            text = ("Партия окончена · R — новая партия" if self.is_host
                    else "Партия окончена · ждём хозяина игры")
            color = "#ffd43b"
        elif self.dead:
            text = ("Вы подорвались · R — новая партия" if self.is_host
                    else "Вы подорвались · ждём хозяина игры")
        if text is None:
            self._clear_overlay()
            return
        width = self.canvas.winfo_width()
        if not self.overlay:
            box = self.canvas.create_rectangle(0, 0, 1, 1, fill="#101418",
                                               outline=color, width=2, tags="overlay")
            label = self.canvas.create_text(0, 0, text="", fill=color,
                                            font=("Segoe UI", 12, "bold"), tags="overlay")
            self.overlay = [box, label]
        box, label = self.overlay
        self.canvas.itemconfigure(label, text=text, fill=color)
        self.canvas.itemconfigure(box, outline=color)
        self.canvas.coords(box, 12, 10, width - 12, 48)
        self.canvas.coords(label, width / 2, 29)
        self.canvas.tag_raise("overlay")

    def _clear_overlay(self) -> None:
        for item in self.overlay:
            self.canvas.delete(item)
        self.overlay = []

    # -- ввод -----------------------------------------------------------
    def _cell_at(self, event) -> tuple[int, int] | None:
        if not self.ready:
            return None
        bx = self.cam_x + int(event.x) // self.cs
        by = self.cam_y + int(event.y) // self.cs
        if 0 <= bx < self.board_w and 0 <= by < self.board_h:
            return bx, by
        return None

    def _on_reveal(self, event) -> None:
        cell = self._cell_at(event)
        if cell is None or self.dead:
            return
        self.net.send({"t": "reveal", "x": cell[0], "y": cell[1]})

    def _on_flag(self, event) -> None:
        cell = self._cell_at(event)
        if cell is None or self.dead or self.game_over:
            return
        self.sounds.play("flag")
        self.net.send({"t": "flag", "x": cell[0], "y": cell[1]})

    def _on_motion(self, event) -> None:
        self._edge_track(event.x, event.y)
        cell = self._cell_at(event)
        if cell is None:
            return
        if cell != self.hover:
            self.hover = cell
            self._update_info()
        now = time.time()
        if now - self._last_cursor > 0.1:
            self._last_cursor = now
            self.net.send({"t": "cursor", "x": cell[0], "y": cell[1]})

    # -- автопрокрутка у края окна --------------------------------------
    def _edge_track(self, x: int, y: int) -> None:
        if self._drag or not self.ready:
            self._edge_stop()
            return
        width = max(1, self.canvas.winfo_width())
        height = max(1, self.canvas.winfo_height())
        dx = -1 if x <= EDGE_ZONE else 1 if x >= width - EDGE_ZONE else 0
        dy = -1 if y <= EDGE_ZONE else 1 if y >= height - EDGE_ZONE else 0
        if dx and abs(x - (0 if dx < 0 else width)) <= EDGE_FAST:
            self._edge_speed = 2
        elif dy and abs(y - (0 if dy < 0 else height)) <= EDGE_FAST:
            self._edge_speed = 2
        else:
            self._edge_speed = 1
        self._edge = (dx, dy)
        if dx or dy:
            if self._edge_timer is None:
                self._edge_timer = self.root.after(EDGE_TICK, self._edge_step)
        else:
            self._edge_stop()

    def _edge_step(self) -> None:
        self._edge_timer = None
        dx, dy = self._edge
        if not self.running or not self.ready or not (dx or dy):
            return
        self._pan(dx * self._edge_speed, dy * self._edge_speed)
        if self._edge != (0, 0):
            self._edge_timer = self.root.after(EDGE_TICK, self._edge_step)

    def _edge_stop(self, event=None) -> None:
        self._edge = (0, 0)
        if self._edge_timer is not None:
            try:
                self.root.after_cancel(self._edge_timer)
            except tk.TclError:
                pass
            self._edge_timer = None

    def _on_wheel(self, event) -> None:
        # колесо — масштаб: от себя приближает, к себе отдаляет
        self._zoom(1 if event.delta > 0 else -1)

    def _on_wheel_h(self, event) -> None:
        self._pan(-4 if event.delta > 0 else 4, 0)

    def _on_drag_start(self, event) -> None:
        self._drag = (event.x, event.y, self.cam_x, self.cam_y)

    def _on_drag_end(self, event=None) -> None:
        self._drag = None

    def _on_drag(self, event) -> None:
        sx, sy, cx, cy = self._drag or (event.x, event.y, self.cam_x, self.cam_y)
        self.cam_x = max(0, min(self.board_w - self.cols, cx - (event.x - sx) // self.cs))
        self.cam_y = max(0, min(self.board_h - self.rows, cy - (event.y - sy) // self.cs))
        self._request_view(force=True)
        self._draw()

    def _pan(self, dx: int, dy: int) -> None:
        if not self.ready:
            return
        new_x = max(0, min(self.board_w - self.cols, self.cam_x + dx))
        new_y = max(0, min(self.board_h - self.rows, self.cam_y + dy))
        if (new_x, new_y) == (self.cam_x, self.cam_y):
            return
        self.cam_x, self.cam_y = new_x, new_y
        self._request_view(force=True)
        self._draw()

    def _zoom(self, step: int) -> None:
        new = max(CELL_MIN, min(CELL_MAX, self.cs + (4 if step > 0 else -4)))
        if new == self.cs:
            return
        center_x = self.cam_x + self.cols // 2
        center_y = self.cam_y + self.rows // 2
        self.cs = new
        self.cols = max(4, self.canvas.winfo_width() // self.cs)
        self.rows = max(4, self.canvas.winfo_height() // self.cs)
        self.cam_x = max(0, min(self.board_w - self.cols, center_x - self.cols // 2))
        self.cam_y = max(0, min(self.board_h - self.rows, center_y - self.rows // 2))
        self._request_view(force=True)
        self._draw()

    def _jump(self, x: int, y: int) -> None:
        if not self.ready:
            return
        self.cam_x = max(0, min(self.board_w - self.cols, x - self.cols // 2))
        self.cam_y = max(0, min(self.board_h - self.rows, y - self.rows // 2))
        self._request_view(force=True)
        self._draw()

    def _ask_jump(self) -> None:
        value = simpledialog.askstring(
            "Переход", "Координаты x, y:", parent=self.root,
            initialvalue=f"{self.hover[0]}, {self.hover[1]}",
        )
        if not value:
            return
        parts = value.replace(";", ",").replace(" ", ",").split(",")
        try:
            x, y = int(parts[0]), int(parts[1])
        except (ValueError, IndexError):
            self._log("Не понял координаты. Пример: 5000, 5000", "err")
            return
        self._jump(x, y)

    def _send_chat(self, event=None) -> None:
        text = self.chat_var.get().strip()
        self.chat_var.set("")
        self.canvas.focus_set()
        if text:
            self.net.send({"t": "chat", "text": text})

    def _typing(self) -> bool:
        try:
            return self.root.focus_get() is self.chat
        except (KeyError, tk.TclError):
            return False

    # -- подписи --------------------------------------------------------
    def _update_info(self) -> None:
        if not self.ready:
            return
        hx, hy = self.hover
        self.info.configure(
            text=(
                f"мин {self.total_mines:,}   ·   "
                f"открыто {self.found:,} / {self.safe_total:,}   ·   "
                f"x={hx} y={hy}   ·   клетка {self.cs}px"
            )
        )

    def _update_players_line(self) -> None:
        if not self.players:
            self.score.configure(text="")
            return
        # строка счёта не резиновая: показываем лучших и обязательно себя
        order = sorted(self.players, key=lambda p: -int(p.get("score", 0)))
        shown = order[:4]
        if not any(p.get("id") == self.my_id for p in shown):
            mine = [p for p in order if p.get("id") == self.my_id]
            shown = (shown[:3] + mine)[:4]
        chunks = []
        for player in shown:
            mark = "" if player.get("alive") else " 💥"
            me = " (вы)" if player.get("id") == self.my_id else ""
            crown = "👑 " if player.get("host") else ""
            chunks.append(f"{crown}{player.get('name')}{me}: {player.get('score', 0):,}{mark}")
        if len(order) > len(shown):
            chunks.append(f"+{len(order) - len(shown)}")
        self.score.configure(text="   ".join(chunks))

    def set_map_visible(self, visible: bool) -> None:
        if visible:
            self.minimap.place(relx=1.0, rely=0.0, x=-12, y=12, anchor="ne")
            self._update_minimap()
        else:
            self.minimap.place_forget()

    def _toggle_map(self) -> None:
        visible = not self.minimap.winfo_ismapped()
        self.set_map_visible(visible)
        self.app.settings["minimap"] = visible
        self.app.persist_settings()

    # -- сохранение -----------------------------------------------------
    def _save_game(self) -> None:
        server = self.app.server
        if server is None:
            self._log("Сохранить может только хозяин игры.", "err")
            return
        if self.game_over:
            self._log("Партия окончена — сохранять нечего.", "err")
            return
        default = time.strftime("Партия %Y-%m-%d %H-%M")
        title = simpledialog.askstring(
            "Сохранить партию", "Название сохранения:",
            initialvalue=default, parent=self.root,
        )
        if not title:
            return
        path = saves.save_path(self.app.saves_dir, title)
        if path.exists() and not messagebox.askyesno(
            "Сохранить партию",
            f"«{path.stem}» уже есть в папке сохранений. Заменить?",
            parent=self.root,
        ):
            return
        mode = "net" if self.app.host_mode == "net" else "solo"
        try:
            path = saves.write_save(
                self.app.saves_dir, path.stem, server.board,
                score=server.board.revealed_count,
                cam=(self.cam_x + self.cols // 2, self.cam_y + self.rows // 2),
                cs=self.cs, started=True, mode=mode,
                server=server.server_name, max_players=server.max_players,
            )
        except OSError as exc:
            self._log(f"Не удалось сохранить: {exc}", "err")
            return
        self._log(f"Сохранено: {path.name} → {path.parent}", "sys")
        if mode == "net":
            self._log(
                "Партия сетевая: при загрузке игра снова появится в сети для друзей.",
                "sys",
            )
        self.sounds.play("chat")

    def _update_minimap(self) -> None:
        self.minimap.update_view(
            self.cam_x, self.cam_y, self.cols, self.rows,
            self.board_w, self.board_h, self.players, self.my_id,
        )

    def _log(self, text: str, tag: str | None = None) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", text + "\n", tag or ())
        self.log.see("end")
        self.log.configure(state="disabled")

    # -- выход ----------------------------------------------------------
    def _leave(self) -> None:
        if self.app.host_mode == "net":
            if not messagebox.askyesno(
                "Выйти из игры",
                "Вы хостите эту игру. Если выйти, она закроется для всех. Выйти?",
                parent=self.root,
            ):
                return
        self.app.leave_game()

    def shutdown(self) -> None:
        if not self.running and self._closed:
            return
        self.running = False
        self._edge_stop()
        for name in ("_timer_poll", "_timer_watch", "_timer_map", "_timer_disconnect"):
            handle = getattr(self, name, None)
            if handle:
                try:
                    self.root.after_cancel(handle)
                except tk.TclError:
                    pass
                setattr(self, name, None)
        self._unbind_keys()
        self.net.close()


# ----------------------------------------------------------------------
# приложение: одно окно и переключение экранов
# ----------------------------------------------------------------------
class App:
    def __init__(self, root: tk.Tk, name: str | None, port: int = DEFAULT_PORT,
                 density: float = 0.16, seed: int | None = None) -> None:
        self.root = root
        self.settings = load_settings()
        for key, value in DEFAULT_SETTINGS.items():
            self.settings.setdefault(key, value)
        self.sounds = SoundPlayer(
            enabled=bool(self.settings.get("sound", True)),
            volume=int(self.settings.get("volume", 70)),
        )
        self.saves_dir = saves.saves_dir()
        fallback = socket.gethostname().split(".")[0] or "игрок"
        self.name = clean_name(name or self.settings.get("name", ""), fallback)
        self.last_host = str(self.settings.get("last_host", ""))
        self.port = port
        self.density = density
        self.seed = seed

        self.screen: tk.Frame | None = None
        self.server = None            # свой сервер, если хостим или играем соло
        self.host_mode: str | None = None   # None | "solo" | "net"

        root.title("Сапёр 500 × 500")
        root.configure(bg=BG)
        root.geometry("1040x760")
        root.minsize(760, 560)
        root.protocol("WM_DELETE_WINDOW", self.quit)

    # -- настройки ------------------------------------------------------
    def save_name(self) -> None:
        self.settings["name"] = self.name
        save_settings(self.settings)

    def remember_host(self, address: str) -> None:
        self.last_host = address
        self.settings["last_host"] = address
        save_settings(self.settings)

    def persist_settings(self) -> None:
        save_settings(self.settings)
        self.sounds.configure(
            bool(self.settings.get("sound", True)),
            int(self.settings.get("volume", 70)),
        )

    def open_settings(self) -> None:
        SettingsDialog(self.root, self)

    def show_saves(self) -> None:
        self._swap(lambda: SavesScreen(self.root, self))

    # -- переключение экранов -------------------------------------------
    def _swap(self, factory) -> None:
        if self.screen is not None:
            if isinstance(self.screen, GameScreen):
                self.screen.shutdown()
            self.screen.destroy()
            self.screen = None
        self.screen = factory()
        self.screen.pack(fill="both", expand=True)

    def show_menu(self, message: str = "") -> None:
        self.root.title("Сапёр 500 × 500")
        self._swap(lambda: MenuScreen(self.root, self, message))

    def show_servers(self, error: str = "") -> None:
        self._swap(lambda: ServersScreen(self.root, self, error))

    def show_create(self) -> None:
        self._swap(lambda: CreateScreen(self.root, self))

    def show_direct(self) -> None:
        self._swap(lambda: DirectScreen(self.root, self))

    def ask_password(self, info: dict) -> None:
        self._swap(lambda: PasswordScreen(self.root, self, info))

    def show_game(self, net: NetClient, note: str,
                  restore: dict | None = None) -> None:
        self._swap(lambda: GameScreen(self.root, self, net, note, restore))

    # -- действия -------------------------------------------------------
    def start_solo(self) -> None:
        from .server import GameServer

        self.save_name()
        server = GameServer(
            port=0, name="Соло", max_players=1, advertise=False,
            density=self.density, seed=self.seed, log=lambda *_: None,
        )
        server.start()
        self.server = server
        self.host_mode = "solo"
        net = NetClient("127.0.0.1", server.port, self.name)
        net.start()
        self.show_game(net, f"Соло-игра — поле {FIELD_SIZE} × {FIELD_SIZE}")

    def load_save(self, meta: dict) -> str | None:
        """Поднимает игру с полем из сохранения и входит в неё.

        Соло-партия остаётся соло, а сохранённая сетевая снова становится
        видимой в локальной сети — друзья могут подключиться и продолжить.
        """
        from .server import GameServer

        try:
            data = saves.read_save(meta["path"])
        except (OSError, ValueError) as exc:
            return f"Не удалось прочитать сохранение: {exc}"

        self.save_name()
        players = max(1, min(32, int(data["max_players"])))
        net_mode = players > 1
        options = dict(
            name=data.get("server") or "Сапёр LAN",
            max_players=players,
            advertise=net_mode,
            width=data["w"], height=data["h"],
            density=data["density"], seed=data["seed"],
            log=lambda *_: None,
        )
        server = None
        for port in ((self.port, 0) if net_mode else (0,)):
            try:
                server = GameServer(port=port, **options)
                server.start()
                break
            except OSError:
                server = None
        if server is None:
            return "Не удалось занять порт для игры"
        try:
            saves.apply_save(server.board, data)
        except ValueError as exc:
            server.stop()
            return f"Сохранение не подошло: {exc}"
        # в соло очки — это открытые клетки; в сетевой у каждого свой счёт
        server.score_start = 0 if net_mode else int(server.board.revealed_count)
        server.started = bool(data.get("started", True))

        self.server = server
        self.host_mode = "net" if net_mode else "solo"
        net = NetClient("127.0.0.1", server.port, self.name)
        net.start()
        if net_mode:
            note = (
                f"Вы хостите «{server.server_name}»: {lan_ip()}:{server.port}"
                " — партия загружена"
            )
        else:
            note = f"Продолжение партии «{data.get('name', '')}»"
        self.show_game(
            net, note,
            restore={"cam": data.get("cam"), "cs": data.get("cs")},
        )
        return None

    def host_game(self, name: str, max_players: int, password: str) -> str | None:
        from .server import GameServer

        self.save_name()
        options = dict(
            name=name, max_players=max_players, password=password,
            density=self.density, seed=self.seed,
            log=lambda *_: None,
        )
        server = None
        for port in (self.port, 0):
            try:
                server = GameServer(port=port, **options)
                server.start()
                break
            except OSError:
                server = None
        if server is None:
            return "Не удалось занять порт для игры"

        self.server = server
        self.host_mode = "net"
        note = f"Вы хостите «{name}»: {lan_ip()}:{server.port}"
        if password:
            note += " · вход по паролю"
        net = NetClient("127.0.0.1", server.port, self.name, password)
        net.start()
        self.show_game(net, note)
        return None

    def join(self, info: dict, password: str = "") -> str | None:
        host = info["host"]
        port = int(info.get("port", DEFAULT_PORT))
        self.save_name()
        try:
            net = NetClient(host, port, self.name, password)
        except JoinDenied as exc:
            return exc.message
        except OSError as exc:
            return f"Не удалось подключиться к {host}:{port} — {exc}"
        net.start()
        self.show_game(net, f"Подключено к «{info.get('name', 'игра')}» ({host}:{port})")
        return None

    def leave_game(self) -> None:
        self._stop_server()
        self.host_mode = None
        self.show_menu()

    def on_disconnected(self) -> None:
        self._stop_server()
        self.host_mode = None
        self.show_menu("Соединение с сервером потеряно")

    def _stop_server(self) -> None:
        if self.server is not None:
            self.server.stop()
            self.server = None

    def quit(self) -> None:
        self._stop_server()
        self.save_name()
        if isinstance(self.screen, GameScreen):
            self.screen.shutdown()
        try:
            for after_id in self.root.tk.splitlist(self.root.tk.call("after", "info")):
                self.root.after_cancel(after_id)
        except tk.TclError:
            pass
        self.root.destroy()


# ----------------------------------------------------------------------
def _set_window_icon(root: tk.Tk) -> None:
    """Иконка окна: рядом с exe (PyInstaller распаковывает её во временную папку)."""
    base = getattr(sys, "_MEIPASS", None)
    candidates = []
    if base:
        candidates.append(Path(base) / "icon.ico")
    here = Path(__file__).resolve().parent
    candidates += [here / "icon.ico", here.parent / "icon.ico"]
    for path in candidates:
        if not path.is_file():
            continue
        try:
            root.iconbitmap(default=str(path))
            return
        except (OSError, tk.TclError):
            continue


def run_client(host: str | None, port: int, name: str | None, *,
               density: float = 0.16, seed: int | None = None) -> None:
    """Открывает окно. Без host сразу подключается, иначе показывает меню."""
    root = tk.Tk()
    _set_window_icon(root)
    app = App(root, name, port=port, density=density, seed=seed)

    if host is None:
        app.show_menu()
    else:
        # прямой вход по адресу: раз пароля нет, просто пробуем подключиться
        error = app.join({"host": host, "port": port, "name": f"{host}:{port}"}, "")
        if error:
            app.show_menu(f"{error}")
    try:
        root.mainloop()
    finally:
        app._stop_server()
