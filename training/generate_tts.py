#!/usr/bin/env python3
"""
Synthetic data for wake word training: Piper (4 Russian voices) + macOS say (Milena).

  data/tts/pos/  — the phrase «хэй пидор» in various voices/tempos/intonations
  data/tts/neg/  — similar phrases and ordinary commands (hard negatives)

All files are WAV 16 kHz mono. A rerun skips files that already exist.
"""
import itertools
import os
import subprocess
import wave

import numpy as np
from piper import PiperVoice, SynthesisConfig
from scipy.signal import resample_poly

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "data", "tts")
PIPER_DIR = os.path.join(HERE, "data", "piper")
VOICES = ["denis", "dmitri", "irina", "ruslan"]

POS_TEXTS = ["хэй пидор", "хэй, пидор", "хэй пидор!", "хэй, пидор!"]
NEG_TEXTS = [
    # similar sounding / parts of the phrase
    "хэй", "пидор", "пидор хэй", "хэй пират", "хэй повар", "хэй пилот", "хэй пинок", "хэй пикап",
    "хэй педаль", "хэй подарок", "хэй погода", "хэй привет", "эй подожди", "эй водитель", "эй бидон",
    "пидорас", "хэй кто там", "хэй вы", "эй, Пётр", "хэй, диор", "окей гугл", "эй сири",
    # ordinary speech
    "включи музыку", "сделай погромче", "какая сегодня погода", "привет, как дела",
    "я иду домой", "выключи свет", "поставь таймер на пять минут", "что нового",
]


def save16k(path, audio, rate):
    """float32 [-1..1] or int16 at any rate → WAV 16 kHz int16."""
    if audio.dtype != np.float32:
        audio = audio.astype(np.float32) / 32768
    if rate != 16000:
        g = np.gcd(rate, 16000)
        audio = resample_poly(audio, 16000 // g, rate // g)
    pcm = np.clip(audio * 32768, -32768, 32767).astype(np.int16)
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(pcm.tobytes())


def piper_clip(voice, text, cfg):
    chunks = list(voice.synthesize(text, syn_config=cfg))
    audio = np.concatenate([c.audio_float_array for c in chunks]).astype(np.float32)
    return audio, chunks[0].sample_rate


def say_clip(text, rate, tmp):
    subprocess.run(["say", "-v", "Milena", "-r", str(rate), "-o", tmp, text], check=True)
    out = tmp + ".wav"
    subprocess.run(["afconvert", "-f", "WAVE", "-d", "LEI16@16000", "-c", "1", tmp, out], check=True)
    with wave.open(out) as w:
        audio = np.frombuffer(w.readframes(w.getnframes()), np.int16)
    os.remove(tmp)
    os.remove(out)
    return audio, 16000


def slug(text):
    return "".join(c for c in text.replace(" ", "_") if c.isalnum() or c == "_")


def main():
    for d in ("pos", "neg"):
        os.makedirs(os.path.join(OUT, d), exist_ok=True)

    voices = {v: PiperVoice.load(os.path.join(PIPER_DIR, f"ru_RU-{v}-medium.onnx")) for v in VOICES}
    n = 0

    # positives: voice × text × tempo × variability
    # the name includes the text index: slug() drops punctuation, which changes intonation
    for v, (ti, text), ls, ns, nw in itertools.product(
            VOICES, enumerate(POS_TEXTS), (0.8, 0.9, 1.0, 1.15, 1.3), (0.4, 0.667, 0.9), (0.5, 0.8, 1.0)):
        path = os.path.join(OUT, "pos", f"piper_{v}_t{ti}_{slug(text)}_ls{ls}_ns{ns}_nw{nw}.wav")
        if not os.path.exists(path):
            cfg = SynthesisConfig(length_scale=ls, noise_scale=ns, noise_w_scale=nw)
            save16k(path, *piper_clip(voices[v], text, cfg))
            n += 1
    for (ti, text), rate in itertools.product(enumerate(POS_TEXTS), (130, 160, 190, 220, 250)):
        path = os.path.join(OUT, "pos", f"say_milena_t{ti}_{slug(text)}_r{rate}.wav")
        if not os.path.exists(path):
            save16k(path, *say_clip(text, rate, "/tmp/_say.aiff"))
            n += 1

    # hard negatives: voice × text × tempo
    for v, text, ls in itertools.product(VOICES, NEG_TEXTS, (0.85, 1.0, 1.2)):
        path = os.path.join(OUT, "neg", f"piper_{v}_{slug(text)}_ls{ls}.wav")
        if not os.path.exists(path):
            save16k(path, *piper_clip(voices[v], text, SynthesisConfig(length_scale=ls)))
            n += 1
    for text, rate in itertools.product(NEG_TEXTS, (150, 210)):
        path = os.path.join(OUT, "neg", f"say_milena_{slug(text)}_r{rate}.wav")
        if not os.path.exists(path):
            save16k(path, *say_clip(text, rate, "/tmp/_say.aiff"))
            n += 1

    pos = len(os.listdir(os.path.join(OUT, "pos")))
    neg = len(os.listdir(os.path.join(OUT, "neg")))
    print(f"new {n}; total pos={pos}, neg={neg}")


if __name__ == "__main__":
    main()
