"""
Assistant controls: the "interrupt" button (event) and the wake word switch (awake).

KnobLink        — Pi: the knob service (src/knob.py) owns the encoder and the ring; this is its client over a unix
                  socket. Also stands in for leds.Ring (set_mode/log) — ring modes go to the knob.
InterruptButton — standalone: encoder button on GPIO23 via pigpiod / Enter on the Mac; the wake word is always on.
"""
import os
import socket
import sys
import threading

BUTTON_GPIO = 23
KNOB_SOCKET = os.environ.get("KNOB_SOCKET", "/tmp/raspidr-knob.sock")


class KnobLink:
    RETRY_S = 2.0

    def __init__(self, path=KNOB_SOCKET):
        self.path = path
        self.event = threading.Event()  # short press: interrupt when busy, listen when idle
        self.double = threading.Event()  # second click of a double click (the knob shows the battery)
        self.summon = threading.Event()  # Triki token button: interrupt when busy, greeting + listening when idle
        self.awake = threading.Event()  # wake word and microphone on (long press toggles)
        self.awake.set()
        self.battery = None  # (percent or None — the gauge doesn't answer, charging) from the knob; None — no word yet
        self.mode = "off"
        self.sock = None
        self.lock = threading.Lock()
        self.stopped = threading.Event()
        threading.Thread(target=self._run, daemon=True).start()

    # ------------------------------------------------------------ leds.Ring interface

    def set_mode(self, mode):
        with self.lock:
            self.mode = mode
            self._send(f"mode {mode}")

    def log(self, msg):
        print(msg, flush=True)

    # ------------------------------------------------------------ connection

    def _send(self, line):
        """Under self.lock. No connection — dropped: the current mode is resent on reconnect."""
        if self.sock:
            try:
                self.sock.sendall((line + "\n").encode())
            except OSError:
                pass

    def _run(self):
        warned = False
        while not self.stopped.is_set():
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                s.connect(self.path)
            except OSError:
                s.close()
                if not warned:
                    self.log(f"[KNOB] service unavailable ({self.path}) — no ring and no button, waiting")
                    warned = True
                self.stopped.wait(self.RETRY_S)
                continue
            warned = False
            with self.lock:
                self.sock = s
                self._send(f"mode {self.mode}")  # the knob may have restarted: bring the ring back
            self.log("[KNOB] connected")
            try:
                for line in s.makefile(encoding="utf-8"):
                    self._handle(line.strip())
            except OSError:
                pass
            with self.lock:
                self.sock = None
            s.close()
            if not self.stopped.is_set():
                # the wake word state stays as it was: the knob sends it again on reconnect
                self.log("[KNOB] link lost")

    def _handle(self, line):
        if line == "press":
            self.event.set()
        elif line == "double":
            self.double.set()
        elif line == "summon":
            self.summon.set()
        elif line == "wake on":
            self.awake.set()
        elif line == "wake off":
            self.awake.clear()
        elif line.startswith("battery "):
            _, soc, power = (line.split() + ["", ""])[:3]
            self.battery = (int(soc) if soc.isdigit() else None, power == "charger")

    def close(self):
        self.stopped.set()
        with self.lock:
            if self.sock:
                try:
                    self.sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass


class InterruptButton:
    """Pi: encoder button on GPIO23 via pigpiod (PUD_UP, press = falling edge, 100 µs glitch filter). Mac: Enter."""

    def __init__(self):
        self.event = threading.Event()
        self.double = threading.Event()  # never set: no double click here
        self.summon = threading.Event()  # never set: no Triki token here
        self.awake = threading.Event()
        self.awake.set()
        self.battery = None  # no knob — the battery is unknown
        self.pi = self.cb = None
        if sys.platform == "darwin":
            if sys.stdin.isatty():
                threading.Thread(target=self._stdin, daemon=True).start()
            return
        try:
            import pigpio
            self.pi = pigpio.pi()
            if not self.pi.connected:
                raise RuntimeError("pigpiod is not running")
            self.pi.set_mode(BUTTON_GPIO, pigpio.INPUT)
            self.pi.set_pull_up_down(BUTTON_GPIO, pigpio.PUD_UP)
            self.pi.set_glitch_filter(BUTTON_GPIO, 100)
            self.cb = self.pi.callback(BUTTON_GPIO, pigpio.FALLING_EDGE, lambda *_: self.event.set())
        except Exception as e:
            print(f"[BTN] button unavailable: {e}", file=sys.stderr)
            self.pi = None

    def _stdin(self):
        for _ in sys.stdin:
            self.event.set()

    def close(self):
        if self.cb:
            self.cb.cancel()
        if self.pi:
            self.pi.set_glitch_filter(BUTTON_GPIO, 0)
            self.pi.stop()
