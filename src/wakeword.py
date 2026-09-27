#!/usr/bin/env python3
"""
Wake word on openWakeWord.

On the Pi (microphone via ALSA):
    .venv/bin/python src/wakeword.py --model hey_peedor --leds --sounds sounds/greetings
Locally / to test on a recording:
    .venv/bin/python src/wakeword.py --model hey_jarvis --wav sample.wav

--model: our own model from models/ (hey_peedor), a stock openWakeWord model (hey_jarvis, alexa, ...)
or a path to .npz/.onnx/.tflite. Fires when score >= --threshold for --patience frames in a row
(frame = 80 ms); after firing, --cooldown seconds of quiet.
--sounds: on firing, play a random WAV from the folder; while it plays (plus --mute-after s)
the detector is deaf — so it doesn't fire on its own reply.
"""
import os

# For our model's tiny matrices OpenBLAS spawns threads that just spin idle (~300% CPU)
for _v in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS"):
    os.environ.setdefault(_v, "1")

import argparse
import collections
import glob
import math
import random
import signal
import subprocess
import sys
import time
import wave

import numpy as np
import openwakeword
from openwakeword.model import Model
from openwakeword.utils import AudioFeatures

from leds import Ring

RATE = 16000
FRAME = 1280  # 80 ms — openWakeWord step
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # project root (src/..)
MODELS_DIR = os.path.join(ROOT, "models")


class NpzModel:
    """Our own model (training/train.py): an MLP over 16 frames of openWakeWord features, computed in numpy."""

    def __init__(self, path):
        w = np.load(path)
        self.name = os.path.splitext(os.path.basename(path))[0]
        self.layers = [(w["w1"], w["b1"], w["g1"], w["be1"]), (w["w2"], w["b2"], w["g2"], w["be2"])]
        self.w3, self.b3 = w["w3"], w["b3"]

    def __call__(self, feats):
        x = feats.reshape(1, -1).astype(np.float32)
        for wt, b, g, be in self.layers:
            x = x @ wt.T + b
            x = (x - x.mean(-1, keepdims=True)) / np.sqrt(x.var(-1, keepdims=True) + 1e-5) * g + be
            x = np.maximum(x, 0)
        z = float((x @ self.w3.T + self.b3)[0, 0])
        return 1 / (1 + np.exp(-z))


def fast_features(framework):
    """openWakeWord features with a short raw-audio buffer. The stock one keeps 10 s and copies all of it into a
    Python list on every frame (~16 ms of the ~45 ms per frame on the Pi), while only the last frame + 480 samples
    are used. Needs frames of exactly FRAME samples."""
    feats = AudioFeatures(inference_framework=framework)
    feats.raw_data_buffer = collections.deque(maxlen=FRAME + 480)
    return feats


class QuietGate:
    """Skips wake word inference while it's quiet. Frames below the noise floor + margin_db go to a short pre-roll
    instead of the model; the first loud frame releases the pre-roll, so the model still sees the pause before the
    phrase and its onset, and the gate stays open for hold_s after the last loud frame. The floor follows the noise
    down fast and up slowly (~0.3 dB/s), so a steady fan or hum closes the gate again.

    The level is measured in the speech band only: with a full-band RMS, low-frequency noise (a washing machine,
    the mic's hum) hid quiet or distant phrases, and the gate lost ~40% of them at -16 dB in a simulation on a
    recorded home background. 1 s of pre-roll kept the same recall as 2 s (docs/power_efficiency.md).

    gate(frame) → the frames to feed into the features now (empty while quiet). seen / fed count frames for stats."""

    def __init__(self, margin_db=8.0, hold_s=1.5, preroll_s=1.0, min_dbfs=-65.0, band=(300, 4000)):
        frame_s = FRAME / RATE
        self.margin = 10 ** (margin_db / 20)
        self.min_rms = 32768 * 10 ** (min_dbfs / 20)
        self.hold = int(hold_s / frame_s)
        self.pending = collections.deque(maxlen=int(preroll_s / frame_s))
        self.floor = None
        self.left = 0  # frames the gate stays open
        self.seen = self.fed = 0
        freqs = np.fft.rfftfreq(FRAME, 1 / RATE)
        self.band = (freqs >= band[0]) & (freqs <= band[1])
        self.window = np.hanning(FRAME).astype(np.float32)
        self.norm = 2 / (FRAME * float(np.sum(self.window ** 2)))  # band power → the scale of a plain RMS²

    def level(self, frame):
        """RMS of the frame in the speech band."""
        spec = np.fft.rfft(frame.astype(np.float32) * self.window)[self.band]
        return math.sqrt(float(np.sum(spec.real ** 2 + spec.imag ** 2)) * self.norm)

    def __call__(self, frame):
        self.seen += 1
        rms = self.level(frame)
        if self.floor is None or rms < self.floor:
            self.floor = rms if self.floor is None else 0.8 * self.floor + 0.2 * rms
        else:
            self.floor *= 1.003
        if rms > max(self.floor * self.margin, self.min_rms):
            self.left = self.hold
        elif self.left > 0:
            self.left -= 1
        else:
            self.pending.append(frame)
            return []
        out = list(self.pending) + [frame]
        self.pending.clear()
        self.fed += len(out)
        return out

    def floor_dbfs(self):
        return 20 * math.log10(max(self.floor or 1.0, 1.0) / 32768)

    def is_open(self):
        return self.left > 0

    def close(self):
        """Not listening for the wake word (dialog in progress): drop the pre-roll, next loud frame reopens."""
        self.pending.clear()
        self.left = 0


def ts():
    t = time.time()
    return time.strftime("%H:%M:%S", time.localtime(t)) + f".{int(t % 1 * 1000):03d}"


class Greeter:
    """A random reply from a folder of WAVs (never repeating the previous one), via aplay."""

    def __init__(self, folder):
        self.files = sorted(glob.glob(os.path.join(folder, "*.wav")))
        if not self.files:
            sys.exit(f"в {folder} нет WAV")
        self.last = None
        self.proc = None

    def play(self):
        """Start a random file, return its duration in seconds."""
        choices = [f for f in self.files if f != self.last] or self.files
        f = random.choice(choices)
        self.last = f
        with wave.open(f) as w:
            # the header may hold a garbage length (file was written as a stream) — cap at what's actually in the file
            frames = min(w.getnframes(), (os.path.getsize(f) - 44) // (w.getnchannels() * w.getsampwidth()))
            seconds = frames / w.getframerate()
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
        self.proc = subprocess.Popen(["aplay", "-q", "-D", "default", f], stderr=subprocess.DEVNULL)
        print(f"[SAY] {os.path.basename(f)} ({seconds:.1f} с)", flush=True)
        return seconds


def free_sound_card():
    # Socket-activated PulseAudio grabs the card — arecord/aplay via dsnoop/dmix get "busy".
    subprocess.run(["systemctl", "--user", "stop", "pulseaudio.socket", "pulseaudio.service"],
                   capture_output=True)


def mic_frames(device):
    proc = subprocess.Popen(
        ["arecord", "-q", "-D", device, "-f", "S16_LE", "-r", str(RATE), "-c", "1", "-t", "raw"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        while True:
            data = proc.stdout.read(FRAME * 2)
            if len(data) < FRAME * 2:
                raise RuntimeError("arecord: " + proc.stderr.read().decode().strip())
            yield np.frombuffer(data, dtype=np.int16)
    finally:
        proc.terminate()
        proc.wait()


def wav_frames(path):
    with wave.open(path, "rb") as w:
        if w.getframerate() != RATE or w.getsampwidth() != 2:
            sys.exit(f"{path}: нужен WAV 16 кГц 16 бит (есть {w.getframerate()} Гц, {w.getsampwidth() * 8} бит)")
        channels = w.getnchannels()
        while True:
            data = w.readframes(FRAME)
            if len(data) < FRAME * 2 * channels:
                return
            samples = np.frombuffer(data, dtype=np.int16)
            yield samples[::channels]  # take the first channel


def resolve_model(name, framework):
    if os.path.exists(name):
        return name
    own = os.path.join(MODELS_DIR, name + ".npz")
    if os.path.exists(own):
        return own
    models_dir = os.path.join(os.path.dirname(openwakeword.__file__), "resources", "models")
    for f in sorted(os.listdir(models_dir)):
        if f.startswith(name) and f.endswith("." + framework):
            return os.path.join(models_dir, f)
    sys.exit(f"модель не найдена: {name} ({framework})")


def main():
    p = argparse.ArgumentParser(description="Wake word on openWakeWord")
    p.add_argument("--model", action="append", required=True, help="stock model name or path, may be repeated")
    p.add_argument("--threshold", type=float, default=0.5)
    p.add_argument("--patience", type=int, default=2, help="consecutive frames above the threshold")
    p.add_argument("--cooldown", type=float, default=2.0, help="seconds without triggers after a trigger")
    p.add_argument("--framework", choices=("tflite", "onnx"), default="tflite",
                   help="inference engine (tflite is usually faster on ARM)")
    p.add_argument("--vad", type=float, default=0.0, help="Silero VAD threshold (0 — off)")
    p.add_argument("--device", default="default", help="ALSA capture device")
    p.add_argument("--wav", help="16 kHz WAV instead of the microphone")
    p.add_argument("--leds", action="store_true", help="NeoPixel ring animation on trigger")
    p.add_argument("--sounds", help="folder of WAVs — play a random one on trigger")
    p.add_argument("--mute-after", type=float, default=1.5,
                   help="how many seconds after the reply the detector stays deaf (model window ~1.3 s)")
    p.add_argument("--keep-pulseaudio", action="store_true", help="don't stop PulseAudio")
    p.add_argument("--stats", type=float, default=10.0, help="CPU stats period, s (0 — off)")
    p.add_argument("--debug", action="store_true", help="print the score of every frame above 0.1")
    p.add_argument("--test", metavar="NEG,POS",
                   help="test: NEG s — red, do NOT say the phrase (false triggers); POS s — blue, say the phrase. "
                        "Then a summary and exit. Example: --test 25,30")
    args = p.parse_args()
    phases = None
    if args.test:
        neg, pos = (float(x) for x in args.test.split(","))
        phases = [("НЕ говори фразу", neg, (255, 0, 0)), ("говори фразу", pos, (0, 0, 255))]

    paths = [resolve_model(m, args.framework) for m in args.model]
    own = [NpzModel(p) for p in paths if p.endswith(".npz")]
    stock = [p for p in paths if not p.endswith(".npz")]
    if stock:
        model = Model(wakeword_models=stock, inference_framework=args.framework, vad_threshold=args.vad)
        features = model.preprocessor
        predict = model.predict
    else:
        model = None
        features = AudioFeatures(inference_framework=args.framework)

        def predict(frame):
            features(frame)
            return {}
    names = ([] if model is None else list(model.models.keys())) + [m.name for m in own]
    print(f"[RUN] модели: {', '.join(names)}  порог={args.threshold} patience={args.patience}", flush=True)

    if not args.wav and not args.keep_pulseaudio:
        free_sound_card()
    # SIGTERM (systemd, timeout) — same as Ctrl+C: exit through finally and turn the ring off
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    ring = Ring("pi" if args.leds else "off")
    greeter = Greeter(args.sounds) if args.sounds else None
    mute_until = -1e9  # in audio time: until when the detector is deaf (reply playing)
    streak = {n: 0 for n in names}
    last_fire = {n: -1e9 for n in names}
    frames = wav_frames(args.wav) if args.wav else mic_frames(args.device)
    stat_wall, stat_cpu, stat_n, stat_infer = time.time(), time.process_time(), 0, 0.0
    audio_t = 0.0  # audio time — for a WAV, triggers are printed with their position in the file
    phase_i, phase_end, fired = 0, 0.0, []
    if phases:
        ring.set_status(phases[0][2])
        phase_end = phases[0][1]
        print(f"[TEST] этап 1: {phases[0][0]} — {phases[0][1]:g} с (красный)", flush=True)

    try:
        for frame in frames:
            t0 = time.perf_counter()
            scores = predict(frame)
            if own:
                feats = features.get_features(16)
                for m in own:
                    # for the first frames the feature buffer isn't filled with real audio yet
                    scores[m.name] = m(feats) if audio_t >= 5 * FRAME / RATE else 0.0
            stat_infer += time.perf_counter() - t0
            stat_n += 1
            audio_t += FRAME / RATE
            if phases and audio_t >= phase_end:
                phase_i += 1
                if phase_i >= len(phases):
                    break
                ring.set_status(phases[phase_i][2])
                phase_end += phases[phase_i][1]
                print(f"[TEST] этап 2: {phases[phase_i][0]} — {phases[phase_i][1]:g} с (синий)", flush=True)

            if audio_t < mute_until:
                streak = {n: 0 for n in names}
                continue
            for name, score in scores.items():
                if args.debug and score > 0.1:
                    print(f"[DBG] {audio_t:7.2f}s {name} {score:.3f}", flush=True)
                streak[name] = streak[name] + 1 if score >= args.threshold else 0
                if streak[name] >= args.patience and audio_t - last_fire[name] >= args.cooldown:
                    last_fire[name] = audio_t
                    streak[name] = 0
                    where = f"{audio_t:.2f}s" if args.wav else ts()
                    tag = f" [этап {phase_i + 1}]" if phases else ""
                    print(f"[WAKE] {where} {name} score={score:.3f}{tag}", flush=True)
                    fired.append(phase_i)
                    if greeter:
                        seconds = greeter.play()
                        ring.wake(seconds, mode="greet")
                        mute_until = audio_t + seconds + args.mute_after
                    else:
                        ring.wake(0.8, mode="greet")

            if args.stats and not args.wav and time.time() - stat_wall >= args.stats:
                wall = time.time() - stat_wall
                cpu = time.process_time() - stat_cpu
                print(f"[STAT] CPU {100 * cpu / wall:5.1f}% одного ядра, "
                      f"инференс {1000 * stat_infer / stat_n:5.1f} мс/кадр (бюджет 80 мс)", flush=True)
                stat_wall, stat_cpu, stat_n, stat_infer = time.time(), time.process_time(), 0, 0.0
    except KeyboardInterrupt:
        pass
    finally:
        ring.close()
    if phases:
        print(f"[TEST] ложных срабатываний (этап 1, {phases[0][1]:g} с): {fired.count(0)}")
        print(f"[TEST] срабатываний на фразу (этап 2): {fired.count(1)} — сравни с тем, сколько раз сказал")
    return 0


if __name__ == "__main__":
    sys.exit(main())
