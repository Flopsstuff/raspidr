#!/usr/bin/env python3
"""
Dataset for the wake word «хэй пидор»: audio → augmentations → openWakeWord features (N, 16, 96).

Positive sources:
  ../hey-peedor/*.mp3        — user's TTS clips
  ../hey-peedor/pi-rec/*.wav — live recordings from the speaker (_11.._15 — test only)
  data/tts/pos/*.wav         — Piper + macOS say (generate_tts.py)
Negatives (hard): data/tts/neg/*.wav, phrase fragments («хэй пи…», «…пидор»), clean background.
Background for mixing: data/noise/*.wav (room silence from the speaker) + synthetic noise.

Output: data/features/{train_pos,train_neg,test_pos,test_pos_noisy,test_neg}.npy (float16)
"""
import glob
import hashlib
import os
import subprocess
import time
import wave

import numpy as np
from openwakeword.utils import AudioFeatures
from scipy.signal import fftconvolve, resample

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DATA = os.path.join(HERE, "data")
OUT = os.path.join(DATA, "features")
SR = 16000
WIN = 32000  # 2 s → exactly 16 feature frames
TEST_PI = {"11", "12", "13", "14", "15"}  # live recording numbers used for test only
TEST_XAI_VOICES = {"iris", "leo", "liora"}  # xAI voices for test only — checks on unseen voices

rng = np.random.default_rng(1234)


# ---------------------------------------------------------------- audio

def load(path):
    """Any file → float32 16 kHz mono."""
    if path.endswith(".wav"):
        with wave.open(path) as w:
            if w.getframerate() == SR and w.getnchannels() == 1 and w.getsampwidth() == 2:
                return np.frombuffer(w.readframes(w.getnframes()), np.int16).astype(np.float32) / 32768
    raw = subprocess.run(["ffmpeg", "-v", "quiet", "-i", path, "-f", "s16le", "-ac", "1", "-ar", str(SR), "-"],
                         capture_output=True, check=True).stdout
    return np.frombuffer(raw, np.int16).astype(np.float32) / 32768


def trim(x):
    """Trim leading/trailing silence by energy (20 ms frames)."""
    hop = 320
    n = len(x) // hop
    if n < 3:
        return x
    db = 20 * np.log10(np.sqrt((x[:n * hop].reshape(n, hop) ** 2).mean(1)) + 1e-9)
    thr = max(np.percentile(db, 10) + 12, db.max() - 35)
    on = np.where(db > thr)[0]
    if len(on) == 0:
        return x
    a, b = max(0, on[0] - 2), min(n, on[-1] + 4)
    return x[a * hop:b * hop]


def is_test(path, share=0.1):
    """Stable split by file name."""
    return int(hashlib.md5(os.path.basename(path).encode()).hexdigest(), 16) % 1000 < share * 1000


# ---------------------------------------------------------------- background

def load_noise_pool():
    pool = [load(p) for p in sorted(glob.glob(os.path.join(DATA, "noise", "*.wav")))]
    n = SR * 30
    white = rng.standard_normal(n).astype(np.float32)
    spec = np.fft.rfft(white)
    f = np.maximum(np.arange(len(spec)), 1)
    pink = np.fft.irfft(spec / np.sqrt(f), n).astype(np.float32)
    brown = np.fft.irfft(spec / f, n).astype(np.float32)
    for s in (white, pink, brown):
        pool.append(s / (np.abs(s).max() + 1e-9) * 0.05)
    return pool


def noise_window(pool, length):
    src = pool[rng.integers(len(pool))]
    if len(src) <= length:
        src = np.tile(src, length // len(src) + 1)
    a = rng.integers(0, len(src) - length)
    return src[a:a + length].copy()


# ---------------------------------------------------------------- augmentations

def rms(x):
    return float(np.sqrt(np.mean(x ** 2)) + 1e-9)


def reverb(x):
    rt60 = rng.uniform(0.15, 0.7)
    n = int(rt60 * SR)
    t = np.arange(n) / SR
    ir = rng.standard_normal(n) * np.exp(-6.9 * t / rt60)
    ir[0] = 1.0
    ir /= np.abs(ir).sum() ** 0.5
    wet = fftconvolve(x, ir)[:len(x) + n // 4]
    return wet / (np.abs(wet).max() + 1e-9) * np.abs(x).max()


def augment_phrase(x, strong=True):
    if strong:
        f = rng.uniform(0.88, 1.12)
        x = resample(x, max(1, int(len(x) / f))).astype(np.float32)
    if strong and rng.random() < 0.5:
        x = reverb(x)
    peak_db = rng.uniform(-28, -3)
    return x / (np.abs(x).max() + 1e-9) * 10 ** (peak_db / 20)


def place(phrase, pool, end_off_s, snr_range=(3, 30)):
    """Place the phrase in a WIN window so it ends end_off_s before the window end, and add background."""
    end = WIN - int(end_off_s * SR)
    phrase = phrase[-end:] if len(phrase) > end else phrase
    out = np.zeros(WIN, np.float32)
    out[end - len(phrase):end] = phrase
    snr = rng.uniform(*snr_range)
    bg = noise_window(pool, WIN)
    bg *= rms(phrase) / rms(bg) / 10 ** (snr / 20)
    out += bg
    return np.clip(out, -1, 1)


def to_i16(batch):
    return (np.stack(batch) * 32767).astype(np.int16)


# ---------------------------------------------------------------- assembly

def main():
    t0 = time.time()
    os.makedirs(OUT, exist_ok=True)
    pool = load_noise_pool()

    user_tts = sorted(glob.glob(os.path.join(ROOT, "hey-peedor", "*.mp3")))
    pi = sorted(glob.glob(os.path.join(ROOT, "hey-peedor", "pi-rec", "*.wav")))
    gen_pos = sorted(glob.glob(os.path.join(DATA, "tts", "pos", "*.wav")))
    gen_neg = sorted(glob.glob(os.path.join(DATA, "tts", "neg", "*.wav")))
    xai_pos = sorted(glob.glob(os.path.join(DATA, "xai", "pos", "*.mp3")))
    xai_neg = sorted(glob.glob(os.path.join(DATA, "xai", "neg", "*.mp3")))

    def xai_is_test(p):
        return os.path.basename(p).split("_")[1] in TEST_XAI_VOICES

    def pi_is_test(p):
        return p.rsplit("_", 1)[-1].split(".")[0] in TEST_PI

    # (path, number of variants, strong augmentations)
    train_pos_src = ([(p, 40, True) for p in user_tts if not is_test(p, 0.15)]
                     + [(p, 150, True) for p in pi if not pi_is_test(p)]
                     + [(p, 6, True) for p in gen_pos if not is_test(p)]
                     + [(p, 6, True) for p in xai_pos if not xai_is_test(p)])
    test_pos_src = ([p for p in user_tts if is_test(p, 0.15)] + [p for p in pi if pi_is_test(p)]
                    + [p for p in gen_pos if is_test(p)] + [p for p in xai_pos if xai_is_test(p)])
    train_neg_src = [p for p in gen_neg if not is_test(p)] + [p for p in xai_neg if not xai_is_test(p)]
    test_neg_src = [p for p in gen_neg if is_test(p)] + [p for p in xai_neg if xai_is_test(p)]
    print(f"позитивы: train источников {len(train_pos_src)}, test {len(test_pos_src)}; "
          f"негативы TTS: train {len(train_neg_src)}, test {len(test_neg_src)}", flush=True)

    cache = {}

    def phrase(p):
        if p not in cache:
            cache[p] = trim(load(p))
        return cache[p]

    # --- positives
    train_pos = []
    for p, k, strong in train_pos_src:
        x = phrase(p)
        for _ in range(k):
            train_pos.append(place(augment_phrase(x, strong), pool, rng.uniform(0.0, 0.3)))
    test_pos = [place(augment_phrase(phrase(p), False), pool, 0.1, (40, 40)) for p in test_pos_src]
    test_pos_noisy = [place(augment_phrase(phrase(p), False), pool, 0.1, (5, 15)) for p in test_pos_src]

    # --- negatives
    train_neg = []
    for p in train_neg_src:
        x = phrase(p)
        for _ in range(3 if "/xai/" in p else 6):
            train_neg.append(place(augment_phrase(x), pool, rng.uniform(0.0, 1.0)))
    # phrase fragments: head («хэй пи…») and tail («…пидор»)
    heads, tails = [], []
    for p, _, _ in train_pos_src:
        x = phrase(p)
        for _ in range(3):
            heads.append(place(augment_phrase(x[:int(len(x) * rng.uniform(0.35, 0.6))]), pool, rng.uniform(0, 0.3)))
            tails.append(place(augment_phrase(x[int(len(x) * rng.uniform(0.4, 0.55)):]), pool, rng.uniform(0, 0.3)))
    train_neg += heads + tails
    # clean background
    for _ in range(800):
        bg = noise_window(pool, WIN)
        train_neg.append(bg / (np.abs(bg).max() + 1e-9) * 10 ** (rng.uniform(-50, -15) / 20))
    test_neg = [place(augment_phrase(phrase(p), False), pool, 0.1, (40, 40)) for p in test_neg_src]

    print(f"клипов: train_pos {len(train_pos)}, train_neg {len(train_neg)}, test_pos {len(test_pos)}, "
          f"test_neg {len(test_neg)} ({time.time() - t0:.0f} с)", flush=True)

    feats = AudioFeatures(inference_framework="onnx", ncpu=4)
    for name, clips in (("train_pos", train_pos), ("train_neg", train_neg), ("test_pos", test_pos),
                        ("test_pos_noisy", test_pos_noisy), ("test_neg", test_neg)):
        f = feats.embed_clips(to_i16(clips), batch_size=64, ncpu=4)[:, -16:, :].astype(np.float16)
        np.save(os.path.join(OUT, f"{name}.npy"), f)
        print(f"  {name}: {f.shape} ({time.time() - t0:.0f} с)", flush=True)

    # list of test files — to re-listen and check with streaming eval
    with open(os.path.join(OUT, "test_files.txt"), "w") as fh:
        fh.write("\n".join(["# pos"] + test_pos_src + ["# neg"] + test_neg_src) + "\n")


if __name__ == "__main__":
    main()
