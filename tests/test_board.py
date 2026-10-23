"""Проверки ядра: битмаски сравниваются с наивной реализацией."""

from __future__ import annotations

import random
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from minesweeper.board import Board


def naive_game(w: int, h: int, grid: list[list[int]]):
    """Эталонный сапёр на списках — для сверки."""
    rev = [[False] * w for _ in range(h)]
    flags = [[False] * w for _ in range(h)]

    def cnt(x, y):
        return sum(
            grid[yy][xx]
            for yy in range(max(0, y - 1), min(h, y + 2))
            for xx in range(max(0, x - 1), min(w, x + 2))
            if (xx, yy) != (x, y)
        )

    def reveal(x, y):
        if not (0 <= x < w and 0 <= y < h):
            return 0
        if rev[y][x] or flags[y][x]:
            return 0
        if cnt(x, y):
            rev[y][x] = True
            return 1
        n = 0
        stack = [(x, y)]
        while stack:
            cx, cy = stack.pop()
            if not (0 <= cx < w and 0 <= cy < h) or rev[cy][cx] or flags[cy][cx]:
                continue
            rev[cy][cx] = True
            n += 1
            if cnt(cx, cy) == 0:
                for dy in (-1, 0, 1):
                    for dx in (-1, 0, 1):
                        stack.append((cx + dx, cy + dy))
        # кайма из чисел уже открыта обходом (она попадает в стек и не пустая)
        return n

    return rev, reveal


def test_matches_naive() -> None:
    for seed in range(25):
        w, h = 17, 13
        b = Board(w, h, 0.18, seed=seed)
        grid = [[(b.mines[y] >> x) & 1 for x in range(w)] for y in range(h)]
        rev, reveal = naive_game(w, h, grid)
        rnd = random.Random(seed * 7 + 1)
        for _ in range(40):
            x, y = rnd.randrange(w), rnd.randrange(h)
            if (b.revealed[y] >> x) & 1 or grid[y][x]:
                continue
            n_bit, boom = b.reveal(x, y)
            n_ref = reveal(x, y)
            assert not boom, (seed, x, y)
            assert n_bit == n_ref, f"seed={seed} click=({x},{y}) bit={n_bit} ref={n_ref}"
            for yy in range(h):
                for xx in range(w):
                    assert bool((b.revealed[yy] >> xx) & 1) == rev[yy][xx], (
                        f"seed={seed} расхождение в ({xx},{yy})"
                    )
    print("ok: открытие областей совпадает с наивной реализацией")


def test_counts_and_flags() -> None:
    b = Board(40, 30, 0.2, seed=99)
    grid = [[(b.mines[y] >> x) & 1 for x in range(40)] for y in range(30)]
    for y in range(30):
        for x in range(40):
            exp = sum(
                grid[yy][xx]
                for yy in range(max(0, y - 1), min(30, y + 2))
                for xx in range(max(0, x - 1), min(40, x + 2))
                if (xx, yy) != (x, y)
            )
            assert b.neighbor_count(x, y) == exp, (x, y)
    assert b.toggle_flag(1, 1) is True
    assert b.is_flagged(1, 1)
    assert b.toggle_flag(1, 1) is False
    assert not b.is_flagged(1, 1)
    print("ok: счётчики соседей и флаги")


def test_make_safe() -> None:
    b = Board(200, 200, 0.2, seed=5)
    before = b.total_mines
    hit = sum(b.mines[y].bit_count() for y in range(200))
    b.make_safe(100, 100)
    after = sum(b.mines[y].bit_count() for y in range(200))
    assert before == hit == after, (before, hit, after)
    for yy in range(99, 102):
        for xx in range(99, 102):
            assert not b.is_mine(xx, yy)
    print("ok: первый клик всегда безопасен, число мин сохраняется")


def test_view() -> None:
    b = Board(50, 50, 0.2, seed=3)
    b.reveal(25, 25)
    rows = b.view_rows(20, 20, 12, 8)
    assert len(rows) == 8 and all(len(r) == 12 for r in rows)
    assert any(ch.isdigit() or ch == "." for row in rows for ch in row)
    edge = b.view_rows(-3, -2, 6, 4)
    assert edge[0] == "      " and edge[2].startswith("   ")
    print("ok: выдача прямоугольника поля")


def bench() -> None:
    t = time.perf_counter()
    b = Board(500, 500, 0.16, seed=1)
    gen = time.perf_counter() - t
    mem = sum(sys.getsizeof(row) for row in b.mines) / 2**10

    t = time.perf_counter()
    rows = b.view_rows(250, 250, 60, 40)
    view = time.perf_counter() - t
    assert len(rows) == 40

    b.make_safe(250, 250)
    t = time.perf_counter()
    n, boom = b.reveal(250, 250)
    single = time.perf_counter() - t

    # редкое поле: одна пустая область размером во всё поле
    t = time.perf_counter()
    big = Board(500, 500, 0.001, seed=2)
    big.make_safe(250, 250)
    big_gen = time.perf_counter() - t
    t = time.perf_counter()
    n_big, _ = big.reveal(250, 250)
    flood_all = time.perf_counter() - t

    print(f"ok: 500x500 генерация {gen*1000:.1f} мс, битмаски мин {mem:.0f} КБ")
    print(f"ok: view 60x40 = {view*1000:.2f} мс, одиночный клик {single*1000:.2f} мс")
    print(
        f"ok: плотность 0.1% → генерация {big_gen*1000:.1f} мс, "
        f"flood {n_big:,} клеток за {flood_all*1000:.1f} мс"
    )
    assert gen < 2.0 and view < 0.2 and flood_all < 2.0
    assert n_big > 220_000


if __name__ == "__main__":
    test_matches_naive()
    test_counts_and_flags()
    test_make_safe()
    test_view()
    bench()
    print("\nвсе проверки ядра пройдены")
