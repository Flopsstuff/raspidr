#!/usr/bin/env python3
"""
Knob service: owns the rotary encoder, the LED ring, the speaker volume and the UPS battery. Runs as its own process
next to the assistant, so volume and the ring keep working while the assistant restarts or is down.

    .venv/bin/python src/knob.py

    turn        — volume (`Speaker` 94..127, 24 steps): tick sound + level bar on the ring
    long press  — wake word and microphone off / on: two notes down / up + ring animation; while off — red center dot.
                  The state survives restarts (~/.config/raspidr/knob.json)
    short press — for the assistant: interrupt when it's busy, start listening (no wake word needed) when idle
    double click — charge bar on the ring + a note; the first click is sent right away as a press, the second as
                   "double" (the assistant drops the listening the first one started)

Battery (UPS-Lite: CW2015 gauge on I2C 0x62, power-good on GPIO4), polled every 30 s:
    below 15% on battery — a steady amber LED + two low notes once; off above 18% or on the charger
    below 3.55 V on battery, 3 reads in a row — the ring drains, two low notes + three down, then a clean poweroff
                           (the gauge's % is unreliable near empty: it showed 0% an hour before the cell hit 2.8 V
                           and the Pi just died — no shutdown, a deep-discharged LiPo, a risk for the SD card)
    charger in / out     — green fills the ring + three notes up / amber drains + three notes down

Unix socket (controls.KNOB_SOCKET), newline-separated text:
    assistant → knob:  mode <leds mode>
    knob → assistant:  wake on | wake off  (on connect and on every toggle), press, double
"""
import json
import os
import re
import signal
import socket
import struct
import subprocess
import sys
import threading
import time

from controls import KNOB_SOCKET
from leds import (LOW_BATTERY, MODES, MUTED_DOT, OFF, Ring, battery_frame, power_frame, volume_frame,
                  wake_off_frame, wake_on_frame)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SOUNDS = os.path.join(ROOT, "sounds", "knob")
STATE = os.path.expanduser("~/.config/raspidr/knob.json")
ENC_A, ENC_B, ENC_BTN = 27, 22, 23
POWER_GPIO = 4  # UPS-Lite power-good: high = charger connected
CW2015 = 0x62
EDGES_PER_STEP = 4  # one detent = a full quadrature cycle
LONG_PRESS_S = 0.8
DOUBLE_CLICK_S = 0.4
BATTERY_POLL_S = 30
GAUGE_TRIES, GAUGE_RETRY_S = 5, 0.06  # the gauge drops ~1 read in 3 in a regular rhythm; a retry gets through
LOW_ON, LOW_OFF = 15.0, 18.0  # % — low battery indicator with hysteresis
SHUTDOWN_V, SHUTDOWN_READS = 3.55, 3  # cell volts on battery, reads in a row (a load dip mustn't power it off)
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


class Gauge:
    """CW2015 fuel gauge on the UPS-Lite. read() → percent or None when the board doesn't answer.
    Single reads fail often (EIO in a regular ~0.25 s rhythm, likely I2C clock stretching), so each read is retried."""

    def __init__(self):
        import smbus

        self.bus = smbus.SMBus(1)
        self.ok = None

    def _word(self, reg):
        raw = self.bus.read_word_data(CW2015, reg)
        return struct.unpack("<H", struct.pack(">H", raw))[0]  # the gauge is big-endian

    def _read_once(self):
        if self.ok is None and self.bus.read_byte_data(CW2015, 0x0A) & 0xC0:
            self.bus.write_byte_data(CW2015, 0x0A, 0x00)  # asleep → wake (no quick-start: keeps its estimate)
        return min(100.0, self._word(0x04) / 256), self._word(0x02) * 0.305 / 1000

    def read(self):
        """→ (percent, volts), or (None, None) when the gauge doesn't answer."""
        for attempt in range(GAUGE_TRIES):
            try:
                soc, volts = self._read_once()
                break
            except OSError as e:
                err = e
                time.sleep(GAUGE_RETRY_S)
        else:
            if self.ok is not False:
                log(f"[BAT] UPS не отвечает ({GAUGE_TRIES} попыток: {err}) — проверь плату UPS (pogo-пины)")
            self.ok = False
            return None, None
        if not self.ok:
            log(f"[BAT] заряд {soc:.0f}%, {volts:.2f} В")
        self.ok = True
        return soc, volts

    def close(self):
        self.bus.close()


class Encoder:
    """Rotation via pigpio edge callbacks, the button is polled (pressed()). Also watches the UPS power-good pin."""

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
        self.pi.set_mode(POWER_GPIO, pigpio.INPUT)
        self.pi.set_glitch_filter(POWER_GPIO, 50000)  # the charger plug bounces
        self.power_flips = 0
        self.cbs.append(self.pi.callback(POWER_GPIO, pigpio.EITHER_EDGE, self._power))

    def _power(self, gpio, level, tick):
        with self.lock:
            self.power_flips += 1

    def take_power_flips(self):
        with self.lock:
            flips, self.power_flips = self.power_flips, 0
        return flips

    def charging(self):
        return self.pi.read(POWER_GPIO) == 1

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
        for pin in (ENC_A, ENC_B, ENC_BTN, POWER_GPIO):
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
        self.gauge = Gauge()
        self.soc, self.low = None, False
        self.volts, self.under = None, 0  # under: reads in a row below SHUTDOWN_V
        self.charging = self.encoder.charging()
        self.battery_now = threading.Event()  # poll right away (charger plugged in/out)
        self.server = Server(KNOB_SOCKET, self.on_line, self.wake_line, lambda: self.ring.set_mode("off"))
        log(f"[KNOB] громкость {self.volume.level + 1}/{len(LEVELS)}, wake word "
            + ("вкл" if self.awake else "ВЫКЛ") + f", сокет {KNOB_SOCKET}; питание: "
            + ("зарядка" if self.charging else "батарея"))
        threading.Thread(target=self._battery_loop, daemon=True).start()

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

    def _battery_loop(self):
        """Own thread: a gauge read with retries takes up to ~0.3 s, the main loop must keep polling the button."""
        while True:
            self.poll_battery()
            self.battery_now.wait(BATTERY_POLL_S)
            self.battery_now.clear()

    def poll_battery(self):
        self.soc, self.volts = self.gauge.read()
        self.check_shutdown()
        low = self.soc is not None and not self.charging and self.soc < (LOW_OFF if self.low else LOW_ON)
        if low != self.low:
            self.low = low
            self.ring.set_badge(LOW_BATTERY if low else None)
            if low:
                play("battery_low")
            log(f"[BAT] низкий заряд: {self.soc:.0f}%" if low else "[BAT] заряд в норме")

    def check_shutdown(self):
        if self.volts is None:
            return  # no read — neither a reason to power off nor to reset the count
        if self.charging or self.volts >= SHUTDOWN_V:
            if self.under:
                log(f"[BAT] {self.volts:.2f} В — выключение отменено")
            self.under = 0
            return
        self.under += 1
        log(f"[BAT] {self.volts:.2f} В < {SHUTDOWN_V} В на батарее ({self.under}/{SHUTDOWN_READS})")
        if self.under >= SHUTDOWN_READS:
            self.shutdown()

    def shutdown(self):
        """Battery empty: say goodbye on the ring and with sound, then a clean poweroff (flop has sudo without a password)."""
        log(f"[BAT] батарея разряжена ({self.volts:.2f} В) — выключаюсь")
        self.ring.overlay(lambda t: power_frame(False, t % 1.2))  # drains again and again until the power goes
        play("battery_low").wait()
        play("power_off").wait()
        time.sleep(0.5)
        subprocess.run(["sudo", "-n", "systemctl", "poweroff"])

    def show_battery(self):
        if self.soc is None:
            play("bump")
            log("[BAT] заряд неизвестен — UPS не отвечает")
            return
        soc = self.soc
        self.ring.overlay(lambda t: battery_frame(soc, t))
        play("battery")
        log(f"[BAT] заряд {soc:.0f}%" + (", заряжается" if self.charging else ""))

    def power_changed(self):
        charging = self.encoder.charging()
        if charging == self.charging:
            return  # bounced back
        self.charging = charging
        self.ring.overlay(lambda t: power_frame(charging, t))
        play("power_on" if charging else "power_off")
        log("[BAT] зарядка подключена" if charging else "[BAT] зарядка отключена — на батарее")
        self.battery_now.set()

    def run(self):
        pressed_at, fired, low = None, False, 0
        last_click = 0.0
        while True:
            time.sleep(0.02)
            now = time.time()
            # button: 2 reads in a row = pressed (debounce); the long press fires while still held
            low = low + 1 if self.encoder.pressed() else 0
            if low >= 2 and pressed_at is None:
                pressed_at, fired = time.time(), False
            elif low == 0 and pressed_at is not None:
                if not fired:
                    if now - last_click <= DOUBLE_CLICK_S:
                        log("[KNOB] двойной клик")
                        self.server.send("double")
                        self.show_battery()
                        last_click = 0.0
                    else:
                        log("[KNOB] нажатие")
                        self.server.send("press")
                        last_click = now
                pressed_at = None
            if pressed_at is not None and not fired and time.time() - pressed_at >= LONG_PRESS_S:
                fired = True
                self.toggle()
            steps = self.encoder.take_steps()
            for _ in range(abs(steps)):
                self.turn(1 if steps > 0 else -1)
            if self.encoder.take_power_flips():
                self.power_changed()

    def close(self):
        self.server.close()
        self.encoder.close()
        self.gauge.close()
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
