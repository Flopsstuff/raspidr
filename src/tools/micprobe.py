#!/usr/bin/env python3
"""
Which physical microphone records into which channel: a step-by-step test with cues on the ring.

    source ~/venv/bin/activate
    python3 micprobe.py            # 6 s stages
    python3 micprobe.py -s 8

LED 0 — stage color (red = pause between stages), 1..3 — L level, 4..6 — R level.
At the end: a table of levels per stage and playback of the whole recording.
"""
import argparse
import array
import math
import subprocess
import sys
import time
import wave

import board
import neopixel

from micmeter import CHUNK, LEFT_LEDS, RATE, RIGHT_LEDS, dbfs, free_sound_card, show_level

PAUSE_S = 2.0
STAGES = [
    ("speak, nothing covered", (0, 255, 0)),
    ("cover LEFT and speak", (255, 180, 0)),
    ("cover RIGHT and speak", (160, 0, 255)),
    ("cover BOTH and speak", (255, 60, 0)),
    ("scratch LEFT with a nail", (0, 200, 255)),
    ("scratch RIGHT with a nail", (255, 255, 255)),
]


def loud(levels):
    """90th percentile of window levels — stage loudness without stray clicks."""
    s = sorted(levels)
    return s[int(len(s) * 0.9)] if s else -120.0


def main():
    p = argparse.ArgumentParser(description="Which microphone records into which channel")
    p.add_argument("-s", "--seconds", type=float, default=6)
    p.add_argument("-o", "--out", default="/tmp/micprobe.wav")
    p.add_argument("--alternate", type=int, metavar="N",
                   help="instead of the standard stages: N stages alternating 'speak into LEFT' / 'speak into RIGHT'")
    args = p.parse_args()
    stages = STAGES
    if args.alternate:
        stages = [(("speak into LEFT", (0, 255, 0)), ("speak into RIGHT", (0, 0, 255)))[i % 2]
                  for i in range(args.alternate)]

    free_sound_card()
    px = neopixel.NeoPixel(board.D10, 7, brightness=0.2, pixel_order=neopixel.GRB, auto_write=False)
    rec = subprocess.Popen(
        ["arecord", "-q", "-D", "default", "-f", "S16_LE", "-r", str(RATE), "-c", "2", "-t", "raw"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def read_chunk():
        data = rec.stdout.read(CHUNK * 4)
        if len(data) < CHUNK * 4:
            raise RuntimeError("arecord: " + rec.stderr.read().decode().strip())
        frames = array.array("h", data)
        return frames, dbfs(frames[0::2]), dbfs(frames[1::2])

    def run_for(seconds, color, on_chunk):
        px.fill((0, 0, 0))
        px[0] = color
        px.show()
        end = time.time() + seconds
        while time.time() < end:
            on_chunk(*read_chunk())

    recorded = array.array("h")
    results = []
    try:
        print(f"Background {PAUSE_S:g} s — stay quiet (red)", flush=True)
        floor = ([], [])
        run_for(PAUSE_S, (255, 0, 0), lambda f, l, r: (floor[0].append(l), floor[1].append(r)))
        base_l, base_r = sorted(floor[0])[len(floor[0]) // 2], sorted(floor[1])[len(floor[1]) // 2]
        print(f"   background: L {base_l:.1f} dBFS, R {base_r:.1f} dBFS", flush=True)

        for n, (what, color) in enumerate(stages, 1):
            print(f"Pause {PAUSE_S:g} s (red), then stage {n}: {what}", flush=True)
            run_for(PAUSE_S, (255, 0, 0), lambda f, l, r: recorded.extend(f))
            print(f"Stage {n}: {what} — {args.seconds:g} s", flush=True)
            levels = ([], [])

            def on_chunk(f, l, r):
                recorded.extend(f)
                levels[0].append(l)
                levels[1].append(r)
                show_level(px, LEFT_LEDS, l - base_l)
                show_level(px, RIGHT_LEDS, r - base_r)
                px.show()

            run_for(args.seconds, color, on_chunk)
            results.append((n, what, loud(levels[0]), max(levels[0]), loud(levels[1]), max(levels[1])))
    finally:
        rec.terminate()
        rec.wait()

    print(f"\nBackground: L {base_l:.1f} dBFS, R {base_r:.1f} dBFS")
    print(f"{'stage':<34} {'L loud':>9} {'L peak':>7}   {'R loud':>9} {'R peak':>7}   (dBFS)")
    for n, what, l90, lmax, r90, rmax in results:
        print(f"{n}. {what:<31} {l90:9.1f} {lmax:7.1f}   {r90:9.1f} {rmax:7.1f}")

    with wave.open(args.out, "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(recorded.tobytes())
    print(f"\nSaved: {args.out}. Playback (blue)...", flush=True)
    try:
        px.fill((0, 0, 0))
        px[0] = (0, 0, 255)
        px.show()
        subprocess.run(["aplay", "-q", "-D", "default", args.out])

        right = recorded[1::2]
        # 99.9th percentile rather than the peak: otherwise a single click drives the gain to zero
        mags = sorted(abs(x) for x in right)
        peak = (mags[int(len(mags) * 0.999)] if mags else 0) or 1
        gain = min(32000 / peak, 1000)
        print(f"Right channel alone, gain x{gain:.0f} ({20 * math.log10(gain):+.0f} dB), to both speakers "
              f"(LEDs 4..6 purple)", flush=True)
        boosted = array.array("h")
        for x in right:
            v = max(-32768, min(32767, int(x * gain)))
            boosted.append(v)
            boosted.append(v)
        right_out = args.out.replace(".wav", "_right.wav")
        with wave.open(right_out, "wb") as w:
            w.setnchannels(2)
            w.setsampwidth(2)
            w.setframerate(RATE)
            w.writeframes(boosted.tobytes())
        px.fill((0, 0, 0))
        for led in RIGHT_LEDS:
            px[led] = (160, 0, 255)
        px.show()
        subprocess.run(["aplay", "-q", "-D", "default", right_out])
    finally:
        px.fill((0, 0, 0))
        px.show()
        px.deinit()
    return 0


if __name__ == "__main__":
    sys.exit(main())
