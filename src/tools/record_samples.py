#!/usr/bin/env python3
"""
Record wake word samples on the speaker: ring RED — wait, GREEN — speak (recording).

    .venv/bin/python src/tools/record_samples.py --label hey-peedor -n 15
    .venv/bin/python src/tools/record_samples.py --label negative -n 10 --record 3   # "not the phrase" samples for testing

Clips: recordings/<label>/<label>_<date-time>_NN.wav — 16 kHz, 16-bit, mono (openWakeWord format).
"""
import argparse
import math
import os
import subprocess
import sys
import time
import wave

import board
import neopixel

RATE = 16000


def level_dbfs(path):
    with wave.open(path, "rb") as w:
        data = w.readframes(w.getnframes())
    samples = memoryview(data).cast("h")
    if not samples:
        return -120.0
    peak = max(abs(s) for s in samples)
    return 20 * math.log10(max(peak, 1) / 32768)


def main():
    p = argparse.ArgumentParser(description="Record wake word samples with ring cues")
    p.add_argument("--label", default="hey-peedor")
    p.add_argument("-n", "--count", type=int, default=15)
    p.add_argument("--wait", type=float, default=5.0, help="red pause before recording, s")
    p.add_argument("--record", type=float, default=3.0, help="recording length (green), s")
    p.add_argument("--device", default="default")
    args = p.parse_args()

    out_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "recordings", args.label)
    os.makedirs(out_dir, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")

    subprocess.run(["systemctl", "--user", "stop", "pulseaudio.socket", "pulseaudio.service"],
                   capture_output=True)
    px = neopixel.NeoPixel(board.D10, 7, brightness=0.2, pixel_order=neopixel.GRB)
    try:
        for i in range(1, args.count + 1):
            px.fill((255, 0, 0))
            print(f"[{i:2d}/{args.count}] red — wait {args.wait:g} s", flush=True)
            time.sleep(args.wait)

            path = os.path.join(out_dir, f"{args.label}_{stamp}_{i:02d}.wav")
            px.fill((0, 255, 0))
            rec = subprocess.run(
                ["arecord", "-q", "-D", args.device, "-f", "S16_LE", "-r", str(RATE), "-c", "1",
                 "-d", str(math.ceil(args.record)), path],
                capture_output=True, text=True)
            if rec.returncode:
                print(f"   arecord: {rec.stderr.strip()}", file=sys.stderr)
                continue
            peak = level_dbfs(path)
            note = "  ⚠️ quiet — looks like nothing was said" if peak < -25 else ""
            print(f"   recorded {os.path.basename(path)}  peak {peak:.1f} dBFS{note}", flush=True)
    except KeyboardInterrupt:
        print("\ninterrupted")
    finally:
        px.fill((0, 0, 0))
        px.deinit()
    print(f"done: {out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
