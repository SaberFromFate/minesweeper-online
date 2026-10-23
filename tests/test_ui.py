"""Проверка окна: меню, настройки, сохранения, список игр и сам геймплей.

Нужен рабочий стол: тест гоняет настоящие события мыши и клавиш.
"""

from __future__ import annotations

import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import tkinter as tk

import minesweeper.client as client
from minesweeper import saves
from minesweeper.client import (
    App,
    CreateScreen,
    DirectScreen,
    GameScreen,
    MenuScreen,
    PasswordScreen,
    SavesScreen,
    ServersScreen,
)
from minesweeper.server import GameServer


def pump(root: tk.Tk, cond, timeout: float = 8.0, what: str = "") -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        root.update()
        if cond():
            return
        time.sleep(0.01)
    raise AssertionError(f"не дождались: {what}")


def click_cell(screen: GameScreen, bx: int, by: int, button: int = 1) -> None:
    x = (bx - screen.cam_x) * screen.cs + screen.cs // 2
    y = (by - screen.cam_y) * screen.cs + screen.cs // 2
    screen.canvas.event_generate(f"<Button-{button}>", x=x, y=y)
    screen.root.update()


def new_app(root: tk.Tk) -> App:
    app = App(root, "Тест")
    app.settings["minimap"] = True          # тест не зависит от настроек пользователя
    app.sounds.configure(False, 70)         # и не шумит
    app.show_menu()
    root.update()
    return app


def test_menu_and_network(root: tk.Tk) -> None:
    app = new_app(root)
    assert isinstance(app.screen, MenuScreen), app.screen

    # никнейм меняется прямо в меню
    menu = app.screen
    texts = [
        child.cget("text")
        for widget in menu.winfo_children()
        for child in widget.winfo_children()
        if isinstance(child, tk.Button)
    ]
    assert set(texts) == {"Играть соло", "Играть по сети", "Продолжить",
                          "Настройки", "Выйти"}, texts
    print("ok: в меню кнопки:", ", ".join(texts))

    menu.name_var.set("Пилот")
    menu._nick_changed()
    menu._nick_save()
    assert app.name == "Пилот", app.name
    print(f"ok: никнейм из меню — {app.name}")

    # «Играть по сети» открывает список игр и запускает сканирование
    app.show_servers()
    root.update()
    assert isinstance(app.screen, ServersScreen)
    servers = app.screen
    pump(root, lambda: "Найдено" in servers.status.cget("text")
         or "не найдено" in servers.status.cget("text"), what="конец сканирования")
    print(f"ok: список игр, статус «{servers.status.cget('text')}»")

    # у экрана четыре кнопки: Назад / Прямое подключение / Обновить / Создать игру
    texts = []
    for widget in servers.winfo_children():
        for child in widget.winfo_children():
            if isinstance(child, tk.Button):
                texts.append(child.cget("text"))
    assert set(texts) == {"Назад", "Прямое подключение", "Обновить", "Создать игру"}, texts
    print("ok: на экране списка кнопки:", ", ".join(texts))

    # экран создания игры
    app.show_create()
    root.update()
    create = app.screen
    assert isinstance(create, CreateScreen)
    create.name_var.set("Тестовая игра")
    create.max_var.set("4")
    create.pass_var.set("секрет")
    create._create()
    root.update()
    assert isinstance(app.screen, GameScreen), app.screen
    assert app.server is not None and app.server.server_name == "Тестовая игра"
    assert app.server.max_players == 4 and app.server.password == "секрет"
    print("ok: создание игры с названием, лимитом и паролем")

    app.quit()


def test_settings_and_saves(root: tk.Tk) -> None:
    app = new_app(root)
    temp = Path(tempfile.mkdtemp(prefix="saper-test-"))
    app.saves_dir = temp

    # -- окно настроек --------------------------------------------------
    app.open_settings()
    root.update()
    dialog = next(
        widget for widget in root.winfo_children()
        if isinstance(widget, client.SettingsDialog)
    )
    dialog.sound_var.set(False)
    dialog._apply()
    assert app.settings["sound"] is False and app.sounds.enabled is False
    dialog.volume_var.set(35)
    dialog._volume("35")
    assert app.settings["volume"] == 35 and app.sounds.volume == 35
    dialog.map_var.set(False)
    dialog._apply()
    assert app.settings["minimap"] is False
    dialog.destroy()
    root.update()
    print("ok: настройки — звук, громкость и миникарта")

    # -- соло-партия, сохранение ---------------------------------------
    app.settings["sound"] = False
    app.settings["minimap"] = True
    app.sounds.configure(False, 70)
    app.start_solo()
    root.update()
    screen = app.screen
    assert isinstance(screen, GameScreen)
    pump(root, lambda: screen.ready and screen.cells, what="первое окно поля")

    board = app.server.board
    target = None
    for dy in range(0, 20):
        for dx in range(0, 20):
            x, y = screen.cam_x + dx, screen.cam_y + dy
            if board.inside(x, y) and not board.is_mine(x, y):
                target = (x, y)
                break
        if target:
            break
    click_cell(screen, *target)
    pump(root, lambda: screen.found > 0, what="открытие клетки")

    fx, fy = screen.cam_x + 30, screen.cam_y + 15
    click_cell(screen, fx, fy, button=3)
    pump(root, lambda: screen.cells.get((fx, fy)) == "F", what="флаг перед сохранением")

    found = board.revealed_count
    ask = client.simpledialog.askstring
    client.simpledialog.askstring = lambda *args, **kwargs: "Проверка"
    try:
        screen._save_game()
    finally:
        client.simpledialog.askstring = ask
    files = list(temp.glob("*.saper"))
    assert len(files) == 1, files
    print(f"ok: партия сохранена — {files[0].name} ({files[0].stat().st_size // 1024} КБ)")

    # -- возврат в меню и продолжение партии ---------------------------
    app.leave_game()
    root.update()
    app.show_saves()
    root.update()
    listing = app.screen
    assert isinstance(listing, SavesScreen), listing
    assert len(listing.items) == 1, listing.items
    iid = next(iter(listing.items))
    listing.tree.selection_set(iid)
    listing._load_selected()
    root.update()
    assert isinstance(app.screen, GameScreen), app.screen
    loaded = app.screen
    pump(root, lambda: loaded.ready and loaded.cells, what="загруженное поле")
    assert app.host_mode == "solo" and app.server.max_players == 1
    assert app.server.board.revealed_count == found, (
        app.server.board.revealed_count, found)
    assert app.server.board.is_flagged(fx, fy), "флаг не восстановился"
    pid = next(iter(app.server.players))
    assert app.server.players[pid].score == found, app.server.players[pid].score
    print(f"ok: партия продолжена — открыто {found}, флаг на месте, счёт {found}")

    # новый клик после загрузки работает
    before = loaded.found
    click_cell(loaded, target[0] + 1, target[1] + 1)
    pump(root, lambda: loaded.found >= before, what="клик после загрузки")
    print("ok: после загрузки поле открывается дальше")

    app.leave_game()
    root.update()
    shutil.rmtree(temp, ignore_errors=True)


def test_net_save(root: tk.Tk) -> None:
    app = new_app(root)
    temp = Path(tempfile.mkdtemp(prefix="saper-net-"))
    app.saves_dir = temp
    app.settings["sound"] = False
    app.sounds.configure(False, 70)

    error = app.host_game("Вечер", 4, "")
    assert error is None, error
    root.update()
    screen = app.screen
    assert isinstance(screen, GameScreen) and app.host_mode == "net"
    pump(root, lambda: screen.ready and screen.cells, what="окно сетевой игры")

    board = app.server.board
    target = None
    for dy in range(0, 20):
        for dx in range(0, 20):
            x, y = screen.cam_x + dx, screen.cam_y + dy
            if board.inside(x, y) and not board.is_mine(x, y):
                target = (x, y)
                break
        if target:
            break
    click_cell(screen, *target)
    pump(root, lambda: screen.found > 0, what="открытие клетки в сетевой игре")
    found = board.revealed_count

    ask = client.simpledialog.askstring
    client.simpledialog.askstring = lambda *args, **kwargs: "Сетевая"
    try:
        screen._save_game()
    finally:
        client.simpledialog.askstring = ask
    files = list(temp.glob("*.saper"))
    assert len(files) == 1, files
    meta = saves.list_saves(temp)[0]
    assert meta["mode"] == "net" and meta["max_players"] == 4, meta
    print(f"ok: сетевая партия сохранена — {files[0].name}, мест до {meta['max_players']}")

    app.leave_game()
    root.update()
    app.show_saves()
    root.update()
    listing = app.screen
    assert isinstance(listing, SavesScreen)
    iid = next(iter(listing.items))
    listing.tree.selection_set(iid)
    listing._load_selected()
    root.update()
    assert isinstance(app.screen, GameScreen), app.screen
    assert app.host_mode == "net", app.host_mode
    loaded = app.screen
    pump(root, lambda: loaded.ready and loaded.cells, what="загруженная сетевая партия")
    assert app.server.max_players == 4 and app.server.advertise
    assert app.server.board.revealed_count == found
    print(f"ok: сетевая партия снова в сети — открыто {found}, мест до 4")

    # друзья могут подключиться к загруженной партии и увидеть то же поле
    guest = client.NetClient("127.0.0.1", app.server.port, "Гость")
    guest.start()
    pump(root, lambda: len(app.server.players) == 2, what="второй игрок")
    guest.close()
    pump(root, lambda: len(app.server.players) == 1, what="выход гостя")
    print("ok: второй игрок подключился к загруженной партии")

    app.leave_game()
    root.update()
    shutil.rmtree(temp, ignore_errors=True)


def test_join_password_and_full(root: tk.Tk) -> None:
    hosted = GameServer(port=0, width=300, height=200, density=0.15, seed=3,
                        name="С паролем", max_players=1, password="ключ",
                        log=lambda *_: None)
    hosted.start()
    info = {"host": "127.0.0.1", "port": hosted.port, "name": "С паролем",
            "players": 0, "max_players": 1, "w": 300, "h": 200, "locked": True}

    app = new_app(root)
    # неверный пароль — остаёмся на экране ввода и видим ошибку
    app.ask_password(info)
    root.update()
    assert isinstance(app.screen, PasswordScreen)
    app.screen.pass_var.set("не тот")
    app.screen._submit()
    root.update()
    assert isinstance(app.screen, PasswordScreen)
    assert "пароль" in app.screen.error.cget("text").lower(), app.screen.error.cget("text")
    print("ok: неверный пароль не пускает")

    # верный пароль — заходим в игру
    app.screen.pass_var.set("ключ")
    app.screen._submit()
    root.update()
    assert isinstance(app.screen, GameScreen), app.screen
    print("ok: верный пароль пускает в игру")

    # лимит игроков: слот занят нами же
    error = app.join(info, "ключ")
    assert error and "заполнена" in error, error
    print(f"ok: лимит игроков соблюдается ({error})")

    app.leave_game()
    root.update()
    hosted.stop()

    # занятое имя/порт не мешают: экран меню вернулся
    assert isinstance(app.screen, MenuScreen)


def test_direct_connect(root: tk.Tk) -> None:
    hosted = GameServer(port=0, width=300, height=200, density=0.15, seed=9,
                        name="Напрямую", max_players=4, password="ключ",
                        log=lambda *_: None)
    hosted.start()

    app = new_app(root)
    app.show_direct()
    root.update()
    screen = app.screen
    assert isinstance(screen, DirectScreen), screen

    address = f"127.0.0.1:{hosted.port}"
    screen.host_var.set(address)
    screen.pass_var.set("не тот")
    screen._connect()
    root.update()
    assert isinstance(app.screen, DirectScreen), app.screen
    assert "пароль" in app.screen.error.cget("text").lower(), app.screen.error.cget("text")
    print("ok: прямое подключение с неверным паролем — ошибка на экране")

    app.screen.pass_var.set("ключ")
    app.screen._connect()
    root.update()
    assert isinstance(app.screen, GameScreen), app.screen
    assert app.last_host == address, app.last_host
    print(f"ok: прямое подключение по адресу {address}")

    app.leave_game()
    root.update()
    hosted.stop()


def test_game_flow(root: tk.Tk) -> None:
    app = new_app(root)
    app.start_solo()
    root.update()
    screen = app.screen
    assert isinstance(screen, GameScreen)
    assert app.host_mode == "solo" and app.server is not None
    pump(root, lambda: screen.ready and screen.cells, what="первое окно поля")
    print(f"ok: соло-игра, открыто {screen.found}, камера {screen.cam_x},{screen.cam_y}")

    # миникарта: приходит от сервера и показывает свой экран
    pump(root, lambda: screen.minimap._painted[0] != "", timeout=6,
         what="первая миникарта")
    assert len(screen.minimap._painted) == 160
    print(f"ok: миникарта {screen.minimap.size}×{screen.minimap.size}")
    screen.minimap._clicked(type("E", (), {"x": 20, "y": 20})())
    pump(root, lambda: screen.cam_x < screen.board_w // 2, what="переход по миникарте")
    print(f"ok: клик по миникарте перенёс камеру в {screen.cam_x},{screen.cam_y}")

    # на миникарте есть точки игроков (в соло — своя)
    pump(root, lambda: screen.my_id in screen.minimap.dots, what="точка игрока на карте")
    print("ok: миникарта рисует положение игрока")

    board = app.server.board
    target = None
    for dy in range(0, 12):
        for dx in range(0, 12):
            x, y = screen.cam_x + dx, screen.cam_y + dy
            if board.inside(x, y) and not board.is_mine(x, y):
                target = (x, y)
                break
        if target:
            break
    before = screen.found
    click_cell(screen, *target)
    pump(root, lambda: screen.found > before, what="открытие по клику")
    assert screen.cells.get(target) not in ("?", None)
    print(f"ok: клик открыл {target}, всего открыто {screen.found}")

    fx, fy = screen.cam_x + 20, screen.cam_y + 15
    click_cell(screen, fx, fy, button=3)
    pump(root, lambda: screen.cells.get((fx, fy)) == "F", what="флаг")
    click_cell(screen, fx, fy, button=3)
    pump(root, lambda: screen.cells.get((fx, fy)) == "?", what="снятие флага")
    print("ok: флаги ставятся и снимаются правой кнопкой")

    # колесо мыши меняет масштаб (раньше двигало камеру)
    before_cs = screen.cs
    screen.canvas.event_generate("<MouseWheel>", delta=120)
    pump(root, lambda: screen.cs == before_cs + 4, what="колесо приближает")
    screen.canvas.event_generate("<MouseWheel>", delta=-120)
    pump(root, lambda: screen.cs == before_cs, what="колесо отдаляет")
    print("ok: колесо мыши меняет масштаб")

    screen._zoom(-1)
    screen._zoom(-1)
    pump(root, lambda: screen.cs == 18, what="зум")
    cam_before = screen.cam_x
    screen._pan(10, 5)
    pump(root, lambda: screen.cam_x == cam_before + 10, what="панорама")
    screen._jump(350, 250)
    pump(root, lambda: screen.cam_x < 350, what="прыжок")
    print("ok: зум, панорама и переход по координатам")

    # у края окна камера едет сама, как в RTS
    cam_x, cam_y = screen.cam_x, screen.cam_y
    screen._edge_track(3, 5)
    assert screen._edge == (-1, -1), screen._edge
    screen._edge_step()
    pump(root, lambda: screen.cam_x < cam_x or screen.cam_y < cam_y, what="автопрокрутка")
    screen._edge_stop()
    # зона автопрокрутки стала втрое шире: 80 px от края — уже «у края»
    assert client.EDGE_ZONE == 84, client.EDGE_ZONE
    screen._edge_track(client.EDGE_ZONE - 4, screen.canvas.winfo_height() // 2)
    assert screen._edge == (-1, 0), screen._edge
    screen._edge_stop()
    screen._edge_track(screen.canvas.winfo_width() // 2, screen.canvas.winfo_height() // 2)
    assert screen._edge == (0, 0) and screen._edge_timer is None
    print("ok: у края окна камера едет сама, в центре — стоит")

    mine = None
    for y in range(screen.cam_y, screen.cam_y + screen.rows):
        for x in range(screen.cam_x, screen.cam_x + screen.cols):
            if board.inside(x, y) and board.is_mine(x, y):
                mine = (x, y)
                break
        if mine:
            break
    assert mine, "в окне не оказалось мины"
    click_cell(screen, *mine)
    pump(root, lambda: screen.dead, what="поражение")
    pump(root, lambda: bool(screen.overlay), what="баннер поражения")
    print(f"ok: подрыв на {mine}, показан баннер")

    assert root.bind("<KeyPress-r>"), "клавиша R не привязана"
    assert screen.is_host, "в соло-игре игрок сам себе хозяин"
    old_board = app.server.board
    screen._action("restart")
    pump(root, lambda: app.server.board is not old_board, what="рестарт")
    pump(root, lambda: not screen.dead and not screen.overlay, what="сброс баннера")
    print("ok: новая партия и снятый баннер")

    app.leave_game()
    root.update()
    assert isinstance(app.screen, MenuScreen)
    print("ok: кнопка «← Меню» возвращает в меню и гасит соло-сервер")


def run_test(test) -> None:
    """Каждому тесту — своё окно: App при выходе уничтожает root."""
    root = tk.Tk()
    root.geometry("1000x700")
    try:
        test(root)
    finally:
        try:
            root.destroy()
        except tk.TclError:
            pass


def main() -> None:
    original = client.messagebox.askyesno
    client.messagebox.askyesno = lambda *args, **kwargs: True
    saved = client.save_settings
    client.save_settings = lambda data: None          # не трогаем настройки пользователя
    try:
        for test in (test_menu_and_network, test_settings_and_saves,
                     test_net_save, test_join_password_and_full,
                     test_direct_connect, test_game_flow):
            run_test(test)
    finally:
        client.messagebox.askyesno = original
        client.save_settings = saved
    print("\nвсе проверки интерфейса пройдены")


if __name__ == "__main__":
    main()
