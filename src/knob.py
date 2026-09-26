#!/usr/bin/env python3
"""
Knob service: owns the rotary encoder, the LED ring and the speaker volume. Runs as its own process next to the
assistant, so volume and the ring keep working while the assistant restarts or is down.

    .venv/bin/python src/knob.py

    turn        — volume (`Speaker` 94..127, 24 steps): tick sound + level bar on the ring
    long press  — wake word and microphone off / on: two notes down / up + ring animation; while off — red center dot.
                  The state survives restarts (~/.config/raspidr/knob.json)
    short press — "interrupt" for the assistant

Unix socket (controls.KNOB_SOCKET), newline-separated text:
    assistant → knob:  mode <leds mode>
    knob → assistant:  wake on | wake off  (on connect and on every toggle), press
"""
import json
import os
import re
import signal
import socket
import subprocess
import sys
import threading
import time

from controls import KNOB_SOCKET
from leds import MODES, MUTED_DOT, OFF, Ring, volume_frame, wake_off_frame, wake_on_frame

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SOUNDS = os.path.join(ROOT, "sounds", "knob")
STATE = os.path.expanduser("~/.config/raspidr/knob.json")
ENC_A, ENC_B, ENC_BTN = 27, 22, 23
EDGES_PER_STEP = 4  # one detent = a full quadrature cycle
LONG_PRESS_S = 0.8
TICK_GAP_S = 0.15
CARD = "wm8960soundcard"
LEVELS = [round(94 + 33 * i / 23) for i in range(24)]  # `Speaker` values (1 dB units), 4 steps per LED
QUIET_MODES = ("greet", "speak")  # the voice is playing: its volume is the feedback, no ticks on top

# (prev_state << 2) | state → quadrature step; ±1 per edge (as in tools/hwtest.py)
_QUAD = {0b0001: 1, 0b0111: 1, 0b1110: 1, 0b1000: 1,
         0b0010: -1, 0b1011: -1, 0b1101: -1, 0b0100: -1}


def log(msg):
    print(time.strftime("%H:%M:%S ") + msg, flush=True)


def play(name):
    return subprocess.Popen(["aplay", "-q", "-D", "default", os.path.join(SOUNDS, name + ".wav")],
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def load_awake():
    try:
        with open(STATE) as f:
            return bool(json.load(f).get("wake", True))
    except (OSError, ValueError):
        return True


def save_awake(awake):
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    with open(STATE, "w") as f:
        json.dump({"wake": awake}, f)


class Volume:
    def __init__(self):
        out = subprocess.run(["amixer", "-c", CARD, "sget", "Speaker"], capture_output=True, text=True).stdout
        m = re.search(r"Front Left: Playback (\d+)", out)
        value = int(m.group(1)) if m else LEVELS[-1]
        self.level = min(range(len(LEVELS)), key=lambda i: abs(LEVELS[i] - value))

    def step(self, delta):
        """±1 level. Returns False at the end of the range."""
        new = self.level + delta
        if not 0 <= new < len(LEVELS):
            return False
        self.level = new
        subprocess.run(["amixer", "-q", "-c", CARD, "sset", "Speaker", str(LEVELS[new])])
        return True


class Encoder:
    """Rotation via pigpio edge callbacks, the button is polled (pressed())."""

    def __init__(self):
        import pigpio

        self.pi = pigpio.pi()
        if not self.pi.connected:
            raise RuntimeError("pigpiod не запущен")
        for pin in (ENC_A, ENC_B, ENC_BTN):
            self.pi.set_mode(pin, pigpio.INPUT)
            self.pi.set_pull_up_down(pin, pigpio.PUD_UP)
            self.pi.set_glitch_filter(pin, 100)
        self.prev = (self.pi.read(ENC_A) << 1) | self.pi.read(ENC_B)
        self.acc, self.steps = 0, 0
        self.lock = threading.Lock()
        self.cbs = [self.pi.callback(pin, pigpio.EITHER_EDGE, self._edge) for pin in (ENC_A, ENC_B)]

    def _edge(self, gpio, level, tick):
        cur = (self.pi.read(ENC_A) << 1) | self.pi.read(ENC_B)
        with self.lock:
            self.acc += _QUAD.get((self.prev << 2) | cur, 0)
            self.prev = cur
            while abs(self.acc) >= EDGES_PER_STEP:
                d = 1 if self.acc > 0 else -1
                self.acc -= d * EDGES_PER_STEP
                self.steps += d

    def take_steps(self):
        with self.lock:
            steps, self.steps = self.steps, 0
        return steps

    def pressed(self):
        return self.pi.read(ENC_BTN) == 0

    def close(self):
        for cb in self.cbs:
            cb.cancel()
        for pin in (ENC_A, ENC_B, ENC_BTN):
            self.pi.set_glitch_filter(pin, 0)
        self.pi.stop()


class Server:
    """Unix socket for the assistant: takes ring modes, sends button events and the wake word state."""

    def __init__(self, path, on_line, on_connect, on_disconnect):
        self.path = path
        self.on_line, self.on_connect, self.on_disconnect = on_line, on_connect, on_disconnect
        self.clients = []
        self.lock = threading.Lock()
        if os.path.exists(path):
            os.unlink(path)
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.bind(path)
        self.sock.listen()
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            with self.lock:
                self.clients.append(conn)
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn):
        log("[KNOB] ассистент подключился")
        self.send(self.on_connect(), conn)
        try:
            for line in conn.makefile(encoding="utf-8"):
                self.on_line(line.strip())
        except OSError:
            pass
        with self.lock:
            self.clients.remove(conn)
        conn.close()
        log("[KNOB] ассистент отключился")
        self.on_disconnect()

    def send(self, line, conn=None):
        with self.lock:
            for c in [conn] if conn else self.clients:
                try:
                    c.sendall((line + "\n").encode())
                except OSError:
                    pass

    def close(self):
        self.sock.close()
        if os.path.exists(self.path):
            os.unlink(self.path)


class Knob:
    def __init__(self):
        self.ring = Ring("pi")
        self.volume = Volume()
        self.awake = load_awake()
        self.ring.set_status(OFF if self.awake else MUTED_DOT)
        self.tick, self.tick_t = None, 0.0
        self.encoder = Encoder()
        self.server = Server(KNOB_SOCKET, self.on_line, self.wake_line, lambda: self.ring.set_mode("off"))
        log(f"[KNOB] громкость {self.volume.level + 1}/{len(LEVELS)}, wake word "
            + ("вкл" if self.awake else "ВЫКЛ") + f", сокет {KNOB_SOCKET}")

    def wake_line(self):
        return "wake on" if self.awake else "wake off"

    def on_line(self, line):
        cmd, _, arg = line.partition(" ")
        if cmd == "mode" and arg in MODES:
            self.ring.set_mode(arg)

    def toggle(self):
        self.awake = not self.awake
        save_awake(self.awake)
        play("wake_on" if self.awake else "wake_off")
        self.ring.overlay(wake_on_frame if self.awake else wake_off_frame)
        self.ring.set_status(OFF if self.awake else MUTED_DOT)
        self.server.send(self.wake_line())
        log("[KNOB] wake word " + ("вкл" if self.awake else "ВЫКЛ"))

    def turn(self, delta):
        moved = self.volume.step(delta)
        level = self.volume.level
        # every step restarts the overlay: the bar holds while turning
        self.ring.overlay(lambda t: volume_frame(level + 1, len(LEVELS), t, not moved))
        if self.ring.mode not in QUIET_MODES:
            if not moved:
                play("bump")
            elif self.tick is None or (self.tick.poll() is not None and time.time() - self.tick_t >= TICK_GAP_S):
                self.tick, self.tick_t = play("tick_up" if delta > 0 else "tick_down"), time.time()
        log(f"[KNOB] громкость {level + 1}/{len(LEVELS)}" + ("" if moved else " — предел"))

    def run(self):
        pressed_at, fired, low = None, False, 0
        while True:
            time.sleep(0.02)
            # button: 2 reads in a row = pressed (debounce); the long press fires while still held
            low = low + 1 if self.encoder.pressed() else 0
            if low >= 2 and pressed_at is None:
                pressed_at, fired = time.time(), False
            elif low == 0 and pressed_at is not None:
                if not fired:
                    log("[KNOB] нажатие → перебить")
                    self.server.send("press")
                pressed_at = None
            if pressed_at is not None and not fired and time.time() - pressed_at >= LONG_PRESS_S:
                fired = True
                self.toggle()
            steps = self.encoder.take_steps()
            for _ in range(abs(steps)):
                self.turn(1 if steps > 0 else -1)

    def close(self):
        self.server.close()
        self.encoder.close()
        self.ring.close()


def main():
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    try:
        knob = Knob()
    except Exception as e:
        sys.exit(f"[KNOB] не запустился: {e}")
    try:
        knob.run()
    except KeyboardInterrupt:
        pass
    finally:
        knob.close()


if __name__ == "__main__":
    main()
