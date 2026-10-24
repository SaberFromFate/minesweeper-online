"""Логика поля сапёра.

Поле по умолчанию 500 x 500 — это 250 тыс. клеток. Хранить по объекту
(или хотя бы по байту) на клетку всё равно дорого, поэтому каждая строка
поля представлена одним Python int, бит ``x`` которого — клетка ``(x, y)``.

Из этого следуют приятные свойства:

* строка на 500 бит занимает ~70 байт, всё поле — ~35 КБ на слой;
* "соседи" клетки считаются тремя побитовыми ИЛИ по строкам;
* открытие пустой области (flood fill) выполняется не по клеткам,
  а по рядам: за один проход вычисляется целый интервал.

Именно последний пункт важен: наивный BFS по четверти миллиона клеток
на Python занимал бы заметно дольше, здесь же область открывается
за O(рядов).
"""

from __future__ import annotations

import random

#: символы, которыми поле отдаётся клиенту
HIDDEN = "?"
FLAG = "F"
EMPTY = "."
BLAST = "*"
OUTSIDE = " "

#: сколько единичных битов в каждом байте — для миникарты заполненности
_POPCOUNT = bytes(bin(i).count("1") for i in range(256))


class Board:
    """Состояние одной партии."""

    def __init__(
        self,
        width: int = 500,
        height: int = 500,
        density: float = 0.16,
        seed: int | None = None,
    ) -> None:
        self.w = int(width)
        self.h = int(height)
        self.density = float(density)
        self.seed = seed if seed is not None else random.randrange(1 << 31)
        self.full = (1 << self.w) - 1

        self.mines: list[int] = [0] * self.h
        self.revealed: list[int] = [0] * self.h
        self.flags: dict[int, int] = {}
        self.blasts: dict[int, int] = {}

        self.total_mines = 0
        self.revealed_count = 0
        #: версия состояния: любое изменение поля увеличивает её — так сервер
        #: понимает, что кэш видимых окон устарел
        self.rev = 0

        self._generate()

    # ------------------------------------------------------------------
    # генерация
    # ------------------------------------------------------------------
    def _generate(self) -> None:
        """Раскладывает мины с плотностью ``density``.

        Плотность задаётся сравнением 16 независимых случайных битовых
        слоёв с порогом: в каждой клетке получается независимая величина
        ``v < threshold``, то есть ровно нужная вероятность. Сравнение
        делается целыми строками (бит-срез), поэтому всё поле строится
        за доли секунды.
        """
        rnd = random.Random(self.seed)
        w, h = self.w, self.h
        bits = 16
        threshold = int(round(self.density * (1 << bits)))
        threshold = max(0, min((1 << bits) - 1, threshold))

        mines = self.mines
        full = self.full
        intervals = range(bits - 1, -1, -1)
        low = 0
        for y in range(h):
            planes = [rnd.getrandbits(w) for _ in range(bits)]
            row = 0
            eq = full
            for i in intervals:
                p = planes[i]
                if (threshold >> i) & 1:
                    row |= eq & ~p
                    eq &= p
                else:
                    eq &= ~p
            row &= full
            mines[y] = row
            low += row.bit_count()

        self.total_mines = low

    def make_safe(self, x: int, y: int, radius: int = 1) -> int:
        """Гарантирует, что первый клик не попадает на мину.

        Мины из окрестности убираются и переносятся в случайные свободные
        клетки, поэтому общее количество мин не меняется.
        """
        rnd = random.Random(self.seed ^ 0x5EED1234)
        moved = 0
        lo_x = max(0, x - radius)
        hi_x = min(self.w, x + radius + 1)
        for yy in range(max(0, y - radius), min(self.h, y + radius + 1)):
            row = self.mines[yy]
            box = ((1 << (hi_x - lo_x)) - 1) << lo_x
            hit = row & box
            if hit:
                moved += hit.bit_count()
                self.mines[yy] = row & ~box

        for _ in range(moved):
            for _try in range(4096):
                nx = rnd.randrange(self.w)
                ny = rnd.randrange(self.h)
                if abs(nx - x) <= radius + 2 and abs(ny - y) <= radius + 2:
                    continue
                if not (self.mines[ny] >> nx) & 1:
                    self.mines[ny] |= 1 << nx
                    break
        if moved:
            self.rev += 1
        return moved

    # ------------------------------------------------------------------
    # запросы
    # ------------------------------------------------------------------
    def inside(self, x: int, y: int) -> bool:
        return 0 <= x < self.w and 0 <= y < self.h

    def is_mine(self, x: int, y: int) -> bool:
        return bool((self.mines[y] >> x) & 1)

    def is_revealed(self, x: int, y: int) -> bool:
        return bool((self.revealed[y] >> x) & 1)

    def is_flagged(self, x: int, y: int) -> bool:
        return bool((self.flags.get(y, 0) >> x) & 1)

    def neighbor_count(self, x: int, y: int) -> int:
        lo = 0 if x == 0 else x - 1
        hi = self.w if x == self.w - 1 else x + 2
        mask = (1 << (hi - lo)) - 1
        n = 0
        for yy in (y - 1, y, y + 1):
            if 0 <= yy < self.h:
                n += ((self.mines[yy] >> lo) & mask).bit_count()
        if (self.mines[y] >> x) & 1:
            n -= 1                     # саму клетку не считаем
        return n

    # ------------------------------------------------------------------
    # действия
    # ------------------------------------------------------------------
    def toggle_flag(self, x: int, y: int) -> bool:
        """Ставит/снимает флаг. Возвращает новое состояние флага."""
        bit = 1 << x
        if self.revealed[y] & bit:
            return False
        row = self.flags.get(y, 0)
        if row & bit:
            row &= ~bit
            on = False
        else:
            row |= bit
            on = True
        if row:
            self.flags[y] = row
        else:
            self.flags.pop(y, None)
        self.rev += 1
        return on

    def blast(self, x: int, y: int) -> None:
        """Помечает клетку как взорвавшуюся мину (показывается клиентам).

        В счёт открытых клеток она не идёт: это мина, а не безопасная клетка.
        """
        bit = 1 << x
        self.blasts[y] = self.blasts.get(y, 0) | bit
        self.revealed[y] |= bit
        self.rev += 1

    def reveal(self, x: int, y: int) -> tuple[int, bool]:
        """Открывает клетку.

        Возвращает ``(сколько новых клеток открыто, был ли взрыв)``.
        """
        if not self.inside(x, y):
            return 0, False
        bit = 1 << x
        if self.revealed[y] & bit:
            return 0, False
        if self.flags.get(y, 0) & bit:
            return 0, False
        if self.mines[y] & bit:
            return 0, True

        if self.neighbor_count(x, y):
            self.revealed[y] |= bit
            self.revealed_count += 1
            self.rev += 1
            return 1, False

        return self._flood(x, y), False

    def _flood(self, x: int, y: int) -> int:
        """Открывает пустую область и её кайму из чисел."""
        w, h, full = self.w, self.h, self.full
        mines = self.mines
        revealed = self.revealed
        zero_cache: dict[int, int] = {}

        def zero_row(yy: int) -> int:
            """Маска клеток ряда yy, вокруг которых нет ни одной мины."""
            z = zero_cache.get(yy)
            if z is None:
                s = mines[yy]
                if yy > 0:
                    s |= mines[yy - 1]
                if yy + 1 < h:
                    s |= mines[yy + 1]
                s |= (s << 1) | (s >> 1)   # горизонтальная окрестность
                z = full & ~s
                zero_cache[yy] = z
            return z

        stack: list[tuple[int, int]] = [(y, 1 << x)]
        filled: dict[int, int] = {}
        added = 0

        while stack:
            yy, seeds = stack.pop()
            open_mask = zero_row(yy) & ~revealed[yy] & full
            seeds &= open_mask
            if not seeds:
                continue

            blockers = full & ~open_mask
            run = 0
            # каждый бит-«затравка» расширяем до границ его непрерывного
            # интервала: границы ищутся парой битовых операций, а не циклом
            while seeds:
                low = seeds & -seeds
                i = low.bit_length() - 1
                seeds ^= low

                left = blockers & ((1 << i) - 1)
                start = left.bit_length()
                right = blockers >> (i + 1)
                if right:
                    # ближайшая помеха справа — младший установленный бит
                    end = i + (right & -right).bit_length()
                else:
                    end = w

                span = ((1 << (end - start)) - 1) << start
                run |= span
                seeds &= ~span

            if not run:
                continue

            filled[yy] = filled.get(yy, 0) | run
            revealed[yy] |= run
            added += run.bit_count()

            # кайма на 8-связности: пустая клетка «зажигает» соседей
            # по диагонали тоже, поэтому затравка расширяется на 1 вбок
            spread = (run | (run << 1) | (run >> 1)) & full
            if yy > 0:
                stack.append((yy - 1, spread))
            if yy + 1 < h:
                stack.append((yy + 1, spread))

        # кайма: все числовые клетки, примыкающие к открытой пустоте
        for yy, run in filled.items():
            spread = (run | (run << 1) | (run >> 1)) & full
            for r in (yy - 1, yy, yy + 1):
                if 0 <= r < h:
                    cand = spread & ~mines[r] & ~revealed[r]
                    if cand:
                        revealed[r] |= cand
                        added += cand.bit_count()

        self.revealed_count += added
        self.rev += 1
        return added

    # ------------------------------------------------------------------
    # выдача клиенту
    # ------------------------------------------------------------------
    def overview(self, size: int = 160) -> list[str]:
        """Карта заполненности поля ``size x size``: ``0``…``9`` — доля открытого.

        Нужна для миникарты: миллион клеток в миниатюру не влезает, поэтому
        каждая клетка карты — блок поля. Строки блока складываются по ИЛИ
        (открыта ли клетка хоть в одном ряду блока), а затем в столбцах блока
        считается число «задетых» — это и есть заполненность от 0 до 9.
        """
        size = max(8, min(256, int(size)))
        nbytes = (self.w + 7) // 8
        popcount = _POPCOUNT
        rows: list[str] = []
        for my in range(size):
            y0 = my * self.h // size
            y1 = max(y0 + 1, (my + 1) * self.h // size)
            acc = 0
            for y in range(y0, min(y1, self.h)):
                acc |= self.revealed[y]
            if not acc:
                rows.append("0" * size)
                continue
            data = acc.to_bytes(nbytes, "little")
            out = bytearray(b"0" * size)
            for mx in range(size):
                x0 = mx * self.w // size
                bw = max(1, (mx + 1) * self.w // size - x0)
                byte = x0 >> 3
                if bw <= 16:
                    # блок уже двух байт — считаем его биты точно
                    chunk = data[byte]
                    if byte + 1 < nbytes:
                        chunk |= data[byte + 1] << 8
                    bits = ((chunk >> (x0 & 7)) & ((1 << bw) - 1)).bit_count()
                    span = bw
                else:
                    bits = popcount[data[byte]]
                    if byte + 1 < nbytes:
                        bits += popcount[data[byte + 1]]
                    span = 16
                level = (bits * 9 + span // 2) // span
                if level > 9:
                    level = 9
                out[mx] = 48 + level
            rows.append(out.decode("ascii"))
        return rows

    def view_rows(self, x0: int, y0: int, w: int, h: int) -> list[str]:
        """Прямоугольник поля в виде строк символов."""
        rows: list[str] = []
        nbytes = (self.w + 7) // 8
        blank = OUTSIDE * w

        # окно на 1 клетку шире среза: в нём сразу видны все три соседа
        win = w + 2
        winmask = (1 << win) - 1
        shift = x0 - 1

        for y in range(y0, y0 + h):
            if not (0 <= y < self.h):
                rows.append(blank)
                continue

            rev = self.revealed[y].to_bytes(nbytes, "little")
            flg = self.flags.get(y, 0).to_bytes(nbytes, "little")
            bl = self.blasts.get(y, 0).to_bytes(nbytes, "little")

            # ряд ещё вообще не тронут — нечего перебирать по клеткам:
            # вся строка «?», а слева от поля « »
            if not (rev or flg or bl):
                if x0 >= 0:
                    rows.append("?" * w)
                else:
                    outside = min(w, -x0)
                    rows.append(" " * outside + "?" * (w - outside))
                continue

            # по одному «окну» на каждую из трёх строк: OR строк потерял бы
            # кратность мин, стоящих в одном столбце разных строк
            spans = []
            for r in (y - 1, y, y + 1):
                if 0 <= r < self.h:
                    spans.append((self.mines[r] >> shift if shift >= 0
                                  else self.mines[r] << -shift) & winmask)
                else:
                    spans.append(0)

            out: list[str] = []
            for i in range(w):
                x = x0 + i
                if not (0 <= x < self.w):
                    out.append(OUTSIDE)
                    continue
                bi, bs = x >> 3, x & 7
                if (bl[bi] >> bs) & 1:
                    out.append(BLAST)
                elif (rev[bi] >> bs) & 1:
                    n = 0
                    for sp in spans:
                        n += ((sp >> i) & 0b111).bit_count()
                    out.append(EMPTY if n == 0 else str(n))
                elif (flg[bi] >> bs) & 1:
                    out.append(FLAG)
                else:
                    out.append(HIDDEN)
            rows.append("".join(out))
        return rows

    # ------------------------------------------------------------------
    def stats(self) -> dict[str, int]:
        return {
            "revealed": self.revealed_count,
            "mines": self.total_mines,
            "safe_total": self.w * self.h - self.total_mines,
            "width": self.w,
            "height": self.h,
        }
