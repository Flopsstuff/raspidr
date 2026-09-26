"""
"Interrupt" button → threading.Event.

Pi:  encoder button on GPIO23 via pigpiod (PUD_UP, press = falling edge, 100 µs glitch filter — like rotary_encoder.py).
Mac: Enter in the terminal.
"""
import sys
import threading

BUTTON_GPIO = 23


class InterruptButton:
    def __init__(self):
        self.event = threading.Event()
        self.pi = self.cb = None
        if sys.platform == "darwin":
            if sys.stdin.isatty():
                threading.Thread(target=self._stdin, daemon=True).start()
            return
        try:
            import pigpio
            self.pi = pigpio.pi()
            if not self.pi.connected:
                raise RuntimeError("pigpiod не запущен")
            self.pi.set_mode(BUTTON_GPIO, pigpio.INPUT)
            self.pi.set_pull_up_down(BUTTON_GPIO, pigpio.PUD_UP)
            self.pi.set_glitch_filter(BUTTON_GPIO, 100)
            self.cb = self.pi.callback(BUTTON_GPIO, pigpio.FALLING_EDGE, lambda *_: self.event.set())
        except Exception as e:
            print(f"[BTN] кнопка недоступна: {e}", file=sys.stderr)
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
