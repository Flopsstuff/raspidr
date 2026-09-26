#!/usr/bin/env python3
"""
Procedural sounds for the speaker (no third-party samples).

    python3 src/tools/make_sounds.py drops            # sounds/think/drops.wav — "drops" played while waiting for an answer
    python3 src/tools/make_sounds.py drops --seed 7 --seconds 12 --rate 1.2 --gain -26
    python3 src/tools/make_sounds.py knob             # sounds/knob/*.wav — encoder feedback (wake word on/off, volume)

"Drops": random pentatonic notes with a marimba timbre (fundamental + 4th harmonic, fast decay),
light echo. The file is seamless: note tails at the end wrap around to the start — it can be looped.

Knob: the same marimba timbre. wake_off — two notes down (G5 → C5), wake_on — two notes up (C5 → G5),
tick_up / tick_down — short clicks after a volume step, bump — dull low knock at the end of the volume range.
"""
import argparse
import os
import wave

import numpy as np

SR = 24000
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # project root (src/tools/../..)


def note(freq, dur=0.9, sr=SR):
    t = np.arange(int(dur * sr)) / sr
    body = np.sin(2 * np.pi * freq * t) * np.exp(-t * 6.0)
    overtone = 0.25 * np.sin(2 * np.pi * freq * 4.0 * t) * np.exp(-t * 18.0)  # "wooden" overtone
    attack = np.minimum(1.0, t / 0.004)  # no click at the start
    return (body + overtone) * attack


def drops(seconds, rate, seed, gain_db):
    rng = np.random.default_rng(seed)
    # C major pentatonic, octaves 5–6
    scale = [523.25, 587.33, 659.25, 783.99, 880.00, 1046.50, 1174.66, 1318.51]
    n = int(seconds * SR)
    out = np.zeros(n + SR * 2)
    t, prev = 0.0, None
    while t < seconds:
        idx = int(np.clip((prev if prev is not None else 3) + rng.integers(-2, 3), 0, len(scale) - 1))
        prev = idx
        tone = note(scale[idx]) * rng.uniform(0.5, 1.0)
        a = int(t * SR)
        out[a:a + len(tone)] += tone
        t += rng.exponential(1 / rate) + 0.12  # random rhythm, but no more than ~8 per second
    # echo: two quiet delayed copies
    for delay, k in ((0.23, 0.35), (0.47, 0.15)):
        d = int(delay * SR)
        out[d:] += out[:-d] * k
    loop = out[:n].copy()
    loop[:len(out) - n] += out[n:]  # tails go to the start: seamless loop
    loop /= np.abs(loop).max() + 1e-9
    return loop * 10 ** (gain_db / 20)


def click(freq, dur=0.09, sr=SR):
    t = np.arange(int(dur * sr)) / sr
    return np.sin(2 * np.pi * freq * t) * np.exp(-t * 45.0) * np.minimum(1.0, t / 0.002)


def sequence(parts, gain_db):
    """parts: [(start_s, samples)] → one normalized sound with the peak at gain_db dBFS."""
    n = max(int(start * SR) + len(x) for start, x in parts)
    out = np.zeros(n)
    for start, x in parts:
        a = int(start * SR)
        out[a:a + len(x)] += x
    out /= np.abs(out).max() + 1e-9
    return out * 10 ** (gain_db / 20)


def knob(gain_db):
    c5, g5 = 523.25, 783.99
    return {
        "wake_off": sequence([(0.0, note(g5, 0.6)), (0.14, note(c5, 0.7))], gain_db),
        "wake_on": sequence([(0.0, note(c5, 0.6)), (0.14, note(g5, 0.7))], gain_db),
        "tick_up": sequence([(0.0, click(1567.98))], gain_db - 4),
        "tick_down": sequence([(0.0, click(1174.66))], gain_db - 4),
        "bump": sequence([(0.0, click(196.0, 0.15)), (0.0, 0.3 * click(392.0, 0.08))], gain_db),
    }


def save(path, x):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    pcm = (np.clip(x, -1, 1) * 32767).astype(np.int16)
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(pcm.tobytes())


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("sound", choices=["drops", "knob"])
    p.add_argument("--seconds", type=float, default=12.0)
    p.add_argument("--rate", type=float, default=1.2, help="average notes per second")
    p.add_argument("--seed", type=int, default=3)
    p.add_argument("--gain", type=float, help="peak in dBFS (answer voice is ~ -3 dBFS; default: drops -26, knob -10)")
    p.add_argument("-o", "--out", help="output file (drops) or directory (knob)")
    a = p.parse_args()
    if a.sound == "drops":
        out = a.out or os.path.join(ROOT, "sounds", "think", "drops.wav")
        save(out, drops(a.seconds, a.rate, a.seed, -26.0 if a.gain is None else a.gain))
        print(out)
    else:
        out = a.out or os.path.join(ROOT, "sounds", "knob")
        for name, x in knob(-10.0 if a.gain is None else a.gain).items():
            save(os.path.join(out, name + ".wav"), x)
            print(os.path.join(out, name + ".wav"))


if __name__ == "__main__":
    main()
