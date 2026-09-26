"""
Speaker's NeoPixel ring: 0 — center (usually off), 1..6 — around the circle.
Each stage of the voice loop has its own animation, running in a separate thread:

    off     — everything dark (smooth fade-out from the previous mode)
    greet   — fast white comet: the speaker woke up and says hello
    listen  — rainbow comet: listening for a phrase
    think   — slow amber breathing: transcribing and waiting for the answer
    think_long — slowly spinning amber light: Hermes is thinking for a while (going to memory/tools)
    speak   — two blue dots moving toward each other: speaking
    error   — three red flashes, then off

Backends: "pi" — NeoPixel over SPI (board.D10), "console" — the ring as a terminal line (Mac), "off" — nothing.
"""
import colorsys
import math
import sys
import threading
import time

MODES = ("off", "greet", "listen", "think", "think_long", "speak", "error")
OFF = (0, 0, 0)


def _rgb(h, s, v):
    r, g, b = colorsys.hsv_to_rgb(h % 1.0, s, max(0.0, min(1.0, v)))
    return int(r * 255), int(g * 255), int(b * 255)


def _comet(head, tail, color_at):
    """6 outer LEDs: brightness fades over tail positions behind the "head"."""
    out = []
    for i in range(6):
        behind = (head - i) % 6
        out.append(color_at(i, max(0.0, 1 - behind / tail)))
    return out


def outer_frame(mode, t):
    """Frame of the 6 outer LEDs for a mode, t seconds after it was switched on."""
    if mode == "greet":
        return _comet((t * 3.0 * 6) % 6, 2.0, lambda i, v: _rgb(0, 0, v))
    if mode == "listen":
        hue = t * 0.3
        return _comet((t * 2.5 * 6) % 6, 3.0, lambda i, v: _rgb(hue + i * 0.06, 1.0, v))
    if mode == "think":
        v = 0.2 + 0.8 * (0.5 - 0.5 * math.cos(2 * math.pi * t / 1.6))
        return [_rgb(0.09, 1.0, v)] * 6
    if mode == "think_long":
        return _comet((t * 0.7 * 6) % 6, 2.5, lambda i, v: _rgb(0.09, 1.0, v))
    if mode == "speak":
        a, b = (t * 1.2 * 6) % 6, (-t * 1.2 * 6) % 6
        out = []
        for i in range(6):
            d = min(min(abs(i - a), 6 - abs(i - a)), min(abs(i - b), 6 - abs(i - b)))
            out.append(_rgb(0.62, 1.0, max(0.12, 1 - d / 1.5)))
        return out
    if mode == "error":
        on = t < 0.9 and int(t / 0.15) % 2 == 0
        return [(255, 0, 0) if on else OFF] * 6
    return [OFF] * 6


class Ring:
    FPS = 25
    FADE_S = 0.4

    def __init__(self, backend="pi", brightness=0.06):
        self.backend = backend
        self.brightness = brightness
        self.px = None
        self.mode = "off"
        self.mode_start = time.time()
        self.until = None  # auto-return to off (for wake())
        self.status = OFF  # center — only for test hints
        self.last = [OFF] * 7
        self.fade_from = [OFF] * 7
        self.lock = threading.Lock()
        self.stopped = threading.Event()
        self.out_lock = threading.Lock()  # console: don't interleave the ring and the log
        if backend == "pi":
            try:
                import board
                import neopixel
                self.px = neopixel.NeoPixel(board.D10, 7, brightness=brightness, pixel_order=neopixel.GRB,
                                            auto_write=False)
            except Exception as e:
                print(f"[LED] кольцо недоступно: {e}", file=sys.stderr)
                self.backend = "off"
        if self.backend == "off":
            return
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    # ------------------------------------------------------------ control

    def set_mode(self, mode):
        assert mode in MODES, mode
        with self.lock:
            if mode != self.mode:
                self.fade_from = list(self.last)
                self.mode, self.mode_start, self.until = mode, time.time(), None

    def wake(self, seconds, mode="listen"):
        """Animate for seconds seconds, then off (for wakeword.py)."""
        self.set_mode(mode)
        with self.lock:
            self.until = time.time() + seconds

    def set_status(self, color):
        with self.lock:
            self.status = color

    def log(self, msg):
        """Print a log line so the console ring doesn't overwrite it."""
        with self.out_lock:
            if self.backend == "console":
                sys.stdout.write("\r\x1b[2K" + msg + "\n")
                self._draw_console(self.last)
            else:
                sys.stdout.write(msg + "\n")
            sys.stdout.flush()

    # ------------------------------------------------------------ rendering

    def _frame(self, now):
        with self.lock:
            if self.until and now >= self.until:
                self.fade_from = list(self.last)
                self.mode, self.mode_start, self.until = "off", now, None
            mode, t, status, fade_from = self.mode, now - self.mode_start, self.status, self.fade_from
        if mode == "error" and t >= 0.9:
            self.set_mode("off")
        if mode == "off":
            k = max(0.0, 1 - t / self.FADE_S)
            outer = [tuple(int(c * k) for c in px) for px in fade_from[1:]]
        else:
            outer = outer_frame(mode, t)
            if t < 0.15:  # soft fade-in
                outer = [tuple(int(c * t / 0.15) for c in px) for px in outer]
        return [status] + outer

    def _draw_console(self, frame):
        dots = "".join(f"\x1b[38;2;{r};{g};{b}m●\x1b[0m" if (r, g, b) != OFF else "\x1b[90m·\x1b[0m"
                       for r, g, b in frame[1:])
        sys.stdout.write(f"\r\x1b[2K  [{dots}] {self.mode}")

    def _run(self):
        while not self.stopped.wait(1 / self.FPS):
            frame = self._frame(time.time())
            if frame == self.last:
                continue
            self.last = frame
            if self.px:
                for i, c in enumerate(frame):
                    self.px[i] = c
                self.px.show()
            elif self.backend == "console":
                with self.out_lock:
                    self._draw_console(frame)
                    sys.stdout.flush()

    def close(self):
        if self.backend == "off":
            return
        self.stopped.set()
        self.thread.join(timeout=1)
        if self.px:
            self.px.fill(OFF)
            self.px.show()
            self.px.deinit()
        elif self.backend == "console":
            sys.stdout.write("\r\x1b[2K")
            sys.stdout.flush()
