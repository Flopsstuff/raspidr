#!/usr/bin/env python3
"""
Microphone level meter on the NeoPixel ring + recording and playback.

    source ~/venv/bin/activate
    python3 micmeter.py              # 10 s of recording, then playback
    python3 micmeter.py -s 20 --no-play

Ring:
    0     — status: red = background calibration (stay quiet), green = recording, blue = playback
    1..3  — left microphone level
    4..6  — right microphone level
Level is relative to each channel's noise floor: +6 / +15 / +25 dB → 1 / 2 / 3 LEDs.
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

RATE = 48000
CHUNK = RATE // 20  # 50 ms
CALIBRATE_S = 1.5
STEPS_DB = (6, 15, 25)
LEVEL_COLORS = ((0, 180, 0), (180, 140, 0), (220, 0, 0))
LEFT_LEDS, RIGHT_LEDS = (1, 2, 3), (4, 5, 6)


def dbfs(samples):
    rms = math.sqrt(sum(s * s for s in samples) / len(samples)) if samples else 0
    return 20 * math.log10(max(rms, 1e-9) / 32768)


def show_level(px, leds, rise_db):
    for i, led in enumerate(leds):
        px[led] = LEVEL_COLORS[i] if rise_db >= STEPS_DB[i] else (0, 0, 0)


def bar(rise_db):
    return "#" * max(0, min(30, int(rise_db)))


def free_sound_card():
    # Socket-activated PulseAudio grabs the card, and arecord/aplay via dsnoop/dmix get "busy".
    subprocess.run(["systemctl", "--user", "stop", "pulseaudio.socket", "pulseaudio.service"],
                   capture_output=True)


def main():
    p = argparse.ArgumentParser(description="Microphone levels on the ring")
    p.add_argument("-s", "--seconds", type=float, default=10)
    p.add_argument("-o", "--out", default="/tmp/micmeter.wav")
    p.add_argument("--no-play", action="store_true")
    args = p.parse_args()

    free_sound_card()
    px = neopixel.NeoPixel(board.D10, 7, brightness=0.2, pixel_order=neopixel.GRB, auto_write=False)
    rec = subprocess.Popen(
        ["arecord", "-q", "-D", "default", "-f", "S16_LE", "-r", str(RATE), "-c", "2", "-t", "raw"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    recorded = array.array("h")
    base_l, base_r = [], []
    try:
        px.fill((0, 0, 0))
        px[0] = (255, 0, 0)
        px.show()
        print(f"Background calibration {CALIBRATE_S} s — stay quiet (LED 0 red)", flush=True)
        start = time.time()
        recording = False
        while True:
            data = rec.stdout.read(CHUNK * 4)
            if len(data) < CHUNK * 4:
                print("arecord:", rec.stderr.read().decode().strip(), file=sys.stderr)
                return 1
            frames = array.array("h", data)
            left, right = frames[0::2], frames[1::2]
            elapsed = time.time() - start

            if not recording:
                base_l.append(dbfs(left))
                base_r.append(dbfs(right))
                if elapsed >= CALIBRATE_S:
                    floor_l = sorted(base_l)[len(base_l) // 2]
                    floor_r = sorted(base_r)[len(base_r) // 2]
                    print(f"Background: L {floor_l:.1f} dBFS, R {floor_r:.1f} dBFS")
                    print(f"Recording {args.seconds:g} s — speak (LED 0 green)", flush=True)
                    px[0] = (0, 255, 0)
                    recording = True
                    start = time.time()
                px.show()
                continue

            recorded.extend(frames)
            rise_l, rise_r = dbfs(left) - floor_l, dbfs(right) - floor_r
            show_level(px, LEFT_LEDS, rise_l)
            show_level(px, RIGHT_LEDS, rise_r)
            px.show()
            print(f"\r  L {rise_l:+5.1f} dB {bar(rise_l):<30} R {rise_r:+5.1f} dB {bar(rise_r):<30}",
                  end="", flush=True)
            if elapsed >= args.seconds:
                break
        print()
    finally:
        rec.terminate()
        rec.wait()

    with wave.open(args.out, "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(recorded.tobytes())
    print(f"Saved: {args.out}")

    try:
        if not args.no_play:
            px.fill((0, 0, 0))
            px[0] = (0, 0, 255)
            px.show()
            print("Playback (LED 0 blue): left mic → left speaker, right → right")
            subprocess.run(["aplay", "-q", "-D", "default", args.out])
    finally:
        px.fill((0, 0, 0))
        px.show()
        px.deinit()
    return 0


if __name__ == "__main__":
    sys.exit(main())
