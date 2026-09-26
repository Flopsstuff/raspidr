#!/usr/bin/env python3
"""
Test all of the speaker hardware (see docs/hardware.md).

Run on the Pi:
    source ~/venv/bin/activate
    python3 hwtest.py                 # all tests, with "can you see/hear it?" questions
    python3 hwtest.py --yes           # no questions, objective checks only
    python3 hwtest.py --only audio,leds

Tests: system, i2c, ups, leds, encoder, speakers, mics.
"""
import argparse
import array
import math
import os
import struct
import subprocess
import sys
import tempfile
import time
import wave

I2CDETECT = "/usr/sbin/i2cdetect"
WM8960_ADDR = 0x1A
CW2015_ADDR = 0x62
POWER_GPIO = 4
LED_COUNT = 7
ENC_A, ENC_B, ENC_BTN = 27, 22, 23

RATE = 48000
TONE_HZ = 1000
ALSA_DEV = "default"

PASS, FAIL, SKIP = "PASS", "FAIL", "SKIP"


class Ctx:
    def __init__(self, interactive, encoder_timeout):
        self.interactive = interactive
        self.encoder_timeout = encoder_timeout
        self.tmp = tempfile.mkdtemp(prefix="hwtest-")

    def ask(self, question):
        """Yes/no from the user. In non-interactive mode — None (not checked)."""
        if not self.interactive:
            return None
        while True:
            answer = input(f"   ? {question} [y/n]: ").strip().lower()
            if answer in ("y", "yes", "д", "да"):
                return True
            if answer in ("n", "no", "н", "нет"):
                return False

    def wait_enter(self, prompt):
        if self.interactive:
            input(f"   > {prompt} — нажми Enter ")


def log(msg):
    print(f"   {msg}", flush=True)


def run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


# ---------------------------------------------------------------- system

def test_system(ctx):
    model = open("/proc/device-tree/model").read().strip("\0 \n")
    log(f"model: {model}")
    throttled = run(["vcgencmd", "get_throttled"]).stdout.strip()
    temp = run(["vcgencmd", "measure_temp"]).stdout.strip()
    log(f"{throttled}  {temp}")
    pigpiod = run(["systemctl", "is-active", "pigpiod"]).stdout.strip()
    log(f"pigpiod: {pigpiod}")

    problems = []
    if not throttled.endswith("=0x0"):
        problems.append(f"питание/перегрев ({throttled})")
    if pigpiod != "active":
        problems.append("pigpiod не запущен")
    if not os.path.exists("/dev/spidev0.0"):
        problems.append("нет /dev/spidev0.0 (SPI выключен)")
    return (FAIL, "; ".join(problems)) if problems else (PASS, model)


# ---------------------------------------------------------------- i2c

def test_i2c(ctx):
    out = run(["sudo", I2CDETECT, "-y", "1"], timeout=30).stdout
    cells = {}
    for line in out.splitlines()[1:]:
        row, _, rest = line.partition(":")
        for i, cell in enumerate(rest.split()):
            if cell not in ("--", ""):
                cells[int(row, 16) + i + (3 if row.strip() == "00" else 0)] = cell
    found = ", ".join(f"0x{a:02x}({c})" for a, c in sorted(cells.items())) or "пусто"
    log(f"i2c-1: {found}")

    missing = []
    if WM8960_ADDR not in cells:
        missing.append("WM8960 0x1a")
    if CW2015_ADDR not in cells:
        missing.append("UPS CW2015 0x62 (переставь плату UPS — pogo-пины)")
    return (FAIL, "нет: " + ", ".join(missing)) if missing else (PASS, found)


# ---------------------------------------------------------------- ups

def _cw2015_word(bus, reg):
    raw = bus.read_word_data(CW2015_ADDR, reg)
    return struct.unpack("<H", struct.pack(">H", raw))[0]


def _power_good(pi):
    return pi.read(POWER_GPIO) == 1


def test_ups(ctx):
    import pigpio
    import smbus

    bus = smbus.SMBus(1)
    pi = pigpio.pi()
    if not pi.connected:
        return FAIL, "pigpiod не отвечает"
    try:
        try:
            version = bus.read_byte_data(CW2015_ADDR, 0x00)
            bus.write_word_data(CW2015_ADDR, 0x0A, 0x30)  # quick-start
            time.sleep(0.2)
            volts = _cw2015_word(bus, 0x02) * 0.305 / 1000
            soc = _cw2015_word(bus, 0x04) / 256
        except OSError as e:
            return FAIL, f"CW2015 не отвечает ({e}) — переставь плату UPS"

        power = _power_good(pi)
        log(f"CW2015 version=0x{version:02x}  {volts:.2f} V  {soc:.1f}%  "
            f"питание: {'зарядка' if power else 'батарея'}")
        if not 3.0 < volts < 4.4:
            return FAIL, f"странное напряжение {volts:.2f} V"

        detail = f"{volts:.2f} V, {soc:.0f}%, {'зарядка' if power else 'батарея'}"
        if ctx.interactive and ctx.ask(f"Проверить GPIO4? Нужно {'выдернуть' if power else 'воткнуть'} зарядку UPS"):
            log("жду смену состояния до 20 с...")
            deadline = time.time() + 20
            while time.time() < deadline and _power_good(pi) == power:
                time.sleep(0.1)
            if _power_good(pi) == power:
                return FAIL, detail + "; GPIO4 не переключился"
            log(f"GPIO4 → {'зарядка' if not power else 'батарея'} ✓")
            detail += "; GPIO4 переключается"
        return PASS, detail
    finally:
        pi.stop()
        bus.close()


# ---------------------------------------------------------------- leds

def test_leds(ctx):
    import board
    import neopixel

    pixels = neopixel.NeoPixel(board.D10, LED_COUNT, brightness=0.2,
                               auto_write=False, pixel_order=neopixel.GRB)
    try:
        for name, color in (("красный", (255, 0, 0)), ("зелёный", (0, 255, 0)),
                            ("синий", (0, 0, 255)), ("белый", (255, 255, 255))):
            log(f"все {LED_COUNT}: {name}")
            pixels.fill(color)
            pixels.show()
            time.sleep(0.7)
        log("бегущий огонь по одному светодиоду")
        for i in range(LED_COUNT * 2):
            pixels.fill((0, 0, 0))
            pixels[i % LED_COUNT] = (0, 128, 128)
            pixels.show()
            time.sleep(0.15)
        pixels.fill((0, 0, 0))
        pixels.show()
    finally:
        pixels.deinit()

    ok = ctx.ask(f"Видел красный, зелёный, синий, белый и бегущий огонь по всем {LED_COUNT}?")
    if ok is None:
        return SKIP, "отрисовано, визуально не подтверждено (--yes)"
    return (PASS, "цвета и все светодиоды ок") if ok else (FAIL, "пользователь не увидел")


# ---------------------------------------------------------------- encoder

# (prev_state << 2) | state → quadrature step; ±1 per edge
_QUAD = {0b0001: 1, 0b0111: 1, 0b1110: 1, 0b1000: 1,
         0b0010: -1, 0b1011: -1, 0b1101: -1, 0b0100: -1}


def test_encoder(ctx):
    if not ctx.interactive:
        return SKIP, "нужны руки (без --yes)"
    import pigpio

    pi = pigpio.pi()
    if not pi.connected:
        return FAIL, "pigpiod не отвечает"
    for pin in (ENC_A, ENC_B, ENC_BTN):
        pi.set_mode(pin, pigpio.INPUT)
        pi.set_pull_up_down(pin, pigpio.PUD_UP)
        pi.set_glitch_filter(pin, 100)

    st = {"prev": (pi.read(ENC_A) << 1) | pi.read(ENC_B), "fwd": 0, "back": 0, "presses": 0}

    def on_enc(gpio, level, tick):
        cur = (pi.read(ENC_A) << 1) | pi.read(ENC_B)
        step = _QUAD.get((st["prev"] << 2) | cur, 0)
        if step > 0:
            st["fwd"] += 1
        elif step < 0:
            st["back"] += 1
        st["prev"] = cur

    def on_btn(gpio, level, tick):
        if level == 0:
            st["presses"] += 1

    cbs = [pi.callback(ENC_A, pigpio.EITHER_EDGE, on_enc),
           pi.callback(ENC_B, pigpio.EITHER_EDGE, on_enc),
           pi.callback(ENC_BTN, pigpio.FALLING_EDGE, on_btn)]
    try:
        log(f"Покрути энкодер в ОБЕ стороны и нажми кнопку (до {ctx.encoder_timeout} с)")
        deadline = time.time() + ctx.encoder_timeout
        last = None
        while time.time() < deadline:
            now = (st["fwd"], st["back"], st["presses"])
            if now != last:
                print(f"\r   → в одну сторону: {now[0]:3d}  в другую: {now[1]:3d}  нажатий: {now[2]}   ",
                      end="", flush=True)
                last = now
            if now[0] >= 4 and now[1] >= 4 and now[2] >= 1:
                break
            time.sleep(0.05)
        print()
    finally:
        for cb in cbs:
            cb.cancel()
        for pin in (ENC_A, ENC_B, ENC_BTN):
            pi.set_glitch_filter(pin, 0)
        pi.stop()

    detail = f"{st['fwd']}/{st['back']} шагов, {st['presses']} нажатий"
    problems = []
    if st["fwd"] < 4 or st["back"] < 4:
        problems.append("вращение не в обе стороны")
    if st["presses"] < 1:
        problems.append("кнопка не нажималась")
    return (FAIL, detail + "; " + ", ".join(problems)) if problems else (PASS, detail)


# ---------------------------------------------------------------- audio helpers

def make_tone(path, seconds, left=True, right=True, freq=TONE_HZ, amp=0.3):
    n = int(RATE * seconds)
    fade = int(RATE * 0.02)
    frames = array.array("h")
    for i in range(n):
        env = min(1.0, i / fade, (n - i) / fade)
        s = int(32767 * amp * env * math.sin(2 * math.pi * freq * i / RATE))
        frames.append(s if left else 0)
        frames.append(s if right else 0)
    with wave.open(path, "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(frames.tobytes())


def play(path):
    return run(["aplay", "-q", "-D", ALSA_DEV, path])


def record(path, seconds):
    return subprocess.Popen(
        ["arecord", "-q", "-D", ALSA_DEV, "-f", "S16_LE", "-r", str(RATE), "-c", "2",
         "-d", str(seconds), path],
        stderr=subprocess.PIPE, text=True)


def read_channels(path, skip_s=0.3):
    with wave.open(path, "rb") as w:
        data = array.array("h", w.readframes(w.getnframes()))
    data = data[int(RATE * skip_s) * 2:]  # the first fraction of a second — clicks/transients
    return data[0::2], data[1::2]


def dbfs(samples):
    if not samples:
        return -120.0
    rms = math.sqrt(sum(s * s for s in samples) / len(samples))
    return 20 * math.log10(max(rms, 1e-9) / 32768)


def tone_db(samples, freq=TONE_HZ):
    """Level of a specific frequency (Goertzel) in dBFS — separates the tone from noise."""
    n = len(samples)
    if n == 0:
        return -120.0
    k = 2 * math.cos(2 * math.pi * freq / RATE)
    s1 = s2 = 0.0
    for x in samples:
        s1, s2 = x + k * s1 - s2, s1
    power = s1 * s1 + s2 * s2 - k * s1 * s2
    amp = 2 * math.sqrt(max(power, 0)) / n
    return 20 * math.log10(max(amp, 1e-9) / 32768)


# ---------------------------------------------------------------- speakers

def test_speakers(ctx):
    results = []
    for name, left, right in (("ЛЕВЫЙ", True, False), ("ПРАВЫЙ", False, True), ("оба", True, True)):
        path = os.path.join(ctx.tmp, f"tone_{name}.wav")
        make_tone(path, 1.0, left, right)
        log(f"тон {TONE_HZ} Гц: {name} канал")
        r = play(path)
        if r.returncode != 0:
            return FAIL, f"aplay: {r.stderr.strip()}"
        heard = ctx.ask(f"Слышал тон в {name.lower()} канале?")
        results.append((name, heard))
        time.sleep(0.3)

    if all(h is None for _, h in results):
        return SKIP, "aplay отработал, на слух не подтверждено (--yes; объективно — в тесте mics)"
    bad = [n for n, h in results if h is False]
    return (FAIL, "не слышно: " + ", ".join(bad)) if bad else (PASS, "L, R и оба канала слышны")


# ---------------------------------------------------------------- mics

class RingCue:
    """Ring cue: red — get ready, green — recording. No-op without NeoPixel."""

    def __init__(self):
        try:
            import board
            import neopixel
            self.px = neopixel.NeoPixel(board.D10, LED_COUNT, brightness=0.2, pixel_order=neopixel.GRB)
        except Exception:
            self.px = None

    def set(self, color):
        if self.px:
            self.px.fill(color)

    def close(self):
        if self.px:
            self.px.fill((0, 0, 0))
            self.px.deinit()


def loudest_window_db(samples, window_s=0.5):
    """RMS of the loudest window — voice/claps produce spikes, steady noise doesn't."""
    step = int(RATE * window_s)
    return max((dbfs(samples[i:i + step]) for i in range(0, len(samples), step)), default=-120.0)


def test_mics(ctx):
    silence = os.path.join(ctx.tmp, "silence.wav")
    loop = os.path.join(ctx.tmp, "loop.wav")
    tone = os.path.join(ctx.tmp, "tone_loop.wav")
    make_tone(tone, 2.0)

    log("фон: 2 с тишины")
    rec = record(silence, 2)
    if rec.wait() != 0:
        return FAIL, f"arecord: {rec.stderr.read().strip()}"
    base = read_channels(silence)

    # Speaker → mic loopback: informational only, the speakers are quiet and 1 kHz is picked up poorly.
    log(f"петля: играю {TONE_HZ} Гц через динамики и пишу микрофонами (инфо)")
    rec = record(loop, 3)
    time.sleep(0.4)
    play(tone)
    if rec.wait() != 0:
        return FAIL, f"arecord: {rec.stderr.read().strip()}"
    got = read_channels(loop)
    for ch, b, g in (("L", base[0], got[0]), ("R", base[1], got[1])):
        log(f"mic {ch}: фон {dbfs(b):6.1f} dBFS, тон {TONE_HZ} Гц +{tone_db(g) - tone_db(b):.1f} dB")

    dead = [ch for ch, b in (("L", base[0]), ("R", base[1])) if dbfs(b) < -95]
    if not ctx.interactive:
        if dead:
            return FAIL, "канал не пишет вообще: " + ", ".join(dead)
        return SKIP, "каналы пишут, голосом не проверено (--yes)"

    voice = os.path.join(ctx.tmp, "voice.wav")
    log("Смотри на кольцо: КРАСНЫЙ — готовься, ЗЕЛЁНЫЙ — говори громко и хлопай у платы 5 с")
    ctx.wait_enter("Готов")
    cue = RingCue()
    try:
        cue.set((255, 0, 0))
        time.sleep(3)
        rec = record(voice, 5)
        time.sleep(0.2)
        cue.set((0, 255, 0))
        rec.wait()
        cue.set((0, 0, 0))
        vl, vr = read_channels(voice)
        log("воспроизвожу запись (кольцо синее)")
        cue.set((0, 0, 255))
        play(voice)
    finally:
        cue.close()

    details, bad = [], []
    for ch, b, v in (("L", base[0], vl), ("R", base[1], vr)):
        rise = loudest_window_db(v) - dbfs(b)
        log(f"mic {ch}: фон {dbfs(b):6.1f} dBFS, громче всего {loudest_window_db(v):6.1f} dBFS (+{rise:.1f} dB)")
        details.append(f"{ch} +{rise:.0f}dB")
        if rise < 6:
            bad.append(f"{ch} не реагирует на голос")
    if ctx.ask("Слышал свой голос в воспроизведении?") is False:
        bad.append("голос не слышен в воспроизведении")

    summary = "голос: " + ", ".join(details)
    return (FAIL, summary + "; " + "; ".join(bad)) if bad else (PASS, summary)


# ---------------------------------------------------------------- main

TESTS = [
    ("system", "Система, питание, pigpiod, SPI", test_system),
    ("i2c", "Шина I2C: WM8960 + UPS", test_i2c),
    ("ups", "UPS-Lite: CW2015 + GPIO4", test_ups),
    ("leds", "NeoPixel кольцо", test_leds),
    ("encoder", "Rotary encoder + кнопка", test_encoder),
    ("speakers", "Динамики L/R", test_speakers),
    ("mics", "Микрофоны (петля через динамики)", test_mics),
]


def main():
    p = argparse.ArgumentParser(description="RaspiDR hardware test")
    p.add_argument("--yes", action="store_true", help="no questions, objective checks only")
    p.add_argument("--only", help="comma-separated: " + ",".join(t[0] for t in TESTS))
    p.add_argument("--encoder-timeout", type=int, default=20)
    args = p.parse_args()

    selected = set(args.only.split(",")) if args.only else {t[0] for t in TESTS}
    unknown = selected - {t[0] for t in TESTS}
    if unknown:
        p.error(f"неизвестные тесты: {', '.join(sorted(unknown))}")

    ctx = Ctx(interactive=not args.yes and sys.stdin.isatty(), encoder_timeout=args.encoder_timeout)
    summary = []
    for key, title, fn in TESTS:
        if key not in selected:
            continue
        print(f"\n== {title} [{key}]", flush=True)
        try:
            status, detail = fn(ctx)
        except KeyboardInterrupt:
            print()
            status, detail = SKIP, "прервано"
        except Exception as e:
            status, detail = FAIL, f"{type(e).__name__}: {e}"
        mark = {PASS: "✅", FAIL: "❌", SKIP: "⏭️ "}[status]
        print(f"   {mark} {status}: {detail}", flush=True)
        summary.append((key, status, detail))

    print("\n== Итог")
    for key, status, detail in summary:
        print(f"   {status:4}  {key:9} {detail}")
    return 1 if any(s == FAIL for _, s, _ in summary) else 0


if __name__ == "__main__":
    sys.exit(main())
