"""Звуки игры: короткие сигналы, синтезированные на месте.

Никаких внешних файлов и библиотек: волна считается в памяти, кладётся
во временную папку и проигрывается ``winsound`` из стандартной библиотеки
Windows. Если звук выключен или система не Windows, все вызовы просто
ничего не делают.
"""

from __future__ import annotations

import io
import math
import shutil
import struct
import sys
import tempfile
import time
import wave
from pathlib import Path

#: частота дискретизации: 22 кГц хватает коротким сигналам
RATE = 22050

#: «ноты»: (форма, частота Гц, длительность с, громкость 0..1)
#: формы: "sine" — чистый тон, "square" — резче, "noise" — взрыв
SOUNDS: dict[str, tuple[tuple[str, float, float, float], ...]] = {
    "open": (("sine", 880, 0.028, 0.35),),
    "flag": (("sine", 1040, 0.030, 0.40), ("sine", 1560, 0.035, 0.30)),
    "boom": (("noise", 0, 0.38, 0.85), ("sine", 58, 0.42, 0.75)),
    "win": (
        ("sine", 523, 0.11, 0.45), ("sine", 659, 0.11, 0.45),
        ("sine", 784, 0.11, 0.45), ("sine", 1047, 0.30, 0.50),
    ),
    "restart": (("sine", 660, 0.07, 0.40), ("sine", 990, 0.11, 0.35)),
    "chat": (("sine", 1180, 0.035, 0.28),),
}

#: детерминированный «белый шум» для взрыва
_NOISE = [((math.sin(i * 12.9898) * 43758.5453) % 1.0) * 2 - 1 for i in range(RATE)]


def _sample(kind: str, freq: float, t: float, index: int) -> float:
    if kind == "noise":
        return _NOISE[index % RATE]
    value = math.sin(2 * math.pi * freq * t)
    return 1.0 if kind == "square" and value >= 0 else (-1.0 if kind == "square" else value)


def render(name: str, volume: float) -> bytes:
    """Собирает WAV в память. ``volume`` — 0..1."""
    segments = SOUNDS[name]
    frames = bytearray()
    for kind, freq, duration, amp in segments:
        count = max(1, int(RATE * duration))
        for i in range(count):
            pos = i / count
            # мягкая атака и спад, иначе на стыках слышны щелчки
            envelope = min(1.0, pos / 0.06) * min(1.0, (1.0 - pos) / 0.30)
            value = _sample(kind, freq, i / RATE, i)
            frames += struct.pack("<h", int(32767 * volume * amp * envelope * value))
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(RATE)
        wav.writeframes(bytes(frames))
    return buffer.getvalue()


class SoundPlayer:
    """Проигрывает сигналы из настроек, не чаще, чем нужно.

    ``winsound`` не умеет играть из памяти асинхронно, поэтому готовые
    WAV-файлы складываются в ``%TEMP%\\minesweeper-sounds``: на каждую
    громкость — своя папка, при смене громкости старая удаляется.
    """

    def __init__(self, enabled: bool = True, volume: int = 70) -> None:
        self.enabled = bool(enabled)
        self.volume = max(0, min(100, int(volume)))
        self._files: dict[str, str] = {}
        self._last: dict[str, float] = {}
        self._root = Path(tempfile.gettempdir()) / "minesweeper-sounds"
        self._winsound = None
        if sys.platform == "win32":
            try:
                import winsound

                self._winsound = winsound
            except ImportError:            # pragma: no cover - Windows без winsound
                self._winsound = None

    # ------------------------------------------------------------------
    @property
    def available(self) -> bool:
        return self._winsound is not None

    def configure(self, enabled: bool, volume: int) -> None:
        volume = max(0, min(100, int(volume)))
        if volume != self.volume:
            self._files.clear()
            shutil.rmtree(self._root / f"v{self.volume}", ignore_errors=True)
        self.enabled = bool(enabled)
        self.volume = volume

    def _file(self, name: str) -> str | None:
        path = self._files.get(name)
        if path:
            return path
        folder = self._root / f"v{self.volume}"
        target = folder / f"{name}.wav"
        if not target.is_file():
            try:
                folder.mkdir(parents=True, exist_ok=True)
                temp = target.with_suffix(".tmp")
                temp.write_bytes(render(name, self.volume / 100))
                temp.replace(target)
            except OSError:
                return None
        self._files[name] = str(target)
        return str(target)

    def play(self, name: str, gap: float = 0.05) -> None:
        """``gap`` — минимальный интервал между одинаковыми сигналами."""
        if not self.enabled or self._winsound is None or self.volume <= 0:
            return
        if name not in SOUNDS:
            return
        now = time.perf_counter()
        if now - self._last.get(name, 0.0) < gap:
            return
        self._last[name] = now
        path = self._file(name)
        if path is None:
            return
        try:
            self._winsound.PlaySound(
                path,
                self._winsound.SND_FILENAME | self._winsound.SND_ASYNC
                | self._winsound.SND_NODEFAULT,
            )
        except (RuntimeError, OSError):
            pass
