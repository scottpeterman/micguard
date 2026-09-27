"""Alarm tones. Generated as WAV on first use so nothing platform-specific needs bundling."""
import math
import struct
import wave

from .backends.base import state_dir

RATE = 44100

# name -> list of (frequency Hz or 0 for silence, duration s)
TONES = {
    "Beep": [(880, 0.25)],
    "Double beep": [(988, 0.12), (0, 0.08), (988, 0.12)],
    "Klaxon": [(660, 0.18), (440, 0.18), (660, 0.18), (440, 0.18)],
    "Chirp": [(1320, 0.06), (0, 0.04), (1760, 0.06)],
}


def _write(path, pattern, volume=0.6):
    frames = bytearray()
    for freq, dur in pattern:
        n = int(RATE * dur)
        fade = min(int(RATE * 0.005), n // 2)  # 5 ms ramps, no clicks
        for i in range(n):
            if freq == 0:
                s = 0.0
            else:
                env = min(1.0, i / fade if fade else 1.0, (n - i) / fade if fade else 1.0)
                s = volume * env * math.sin(2 * math.pi * freq * i / RATE)
            frames += struct.pack("<h", int(s * 32767))
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(bytes(frames))


def tone_path(name):
    d = state_dir() / "sounds"
    d.mkdir(exist_ok=True)
    path = d / f"{name.lower().replace(' ', '_')}.wav"
    if not path.exists():
        _write(path, TONES.get(name, TONES["Beep"]))
    return path
