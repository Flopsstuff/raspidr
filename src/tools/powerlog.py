#!/usr/bin/env python3
"""
Power draw of the speaker, estimated from how fast the UPS-Lite battery drains (the CW2015 has no current sensor).

    .venv/bin/python src/tools/powerlog.py log [--every 30] [-o ~/power.csv]      # until Ctrl+C / kill
    .venv/bin/python src/tools/powerlog.py report [-o ~/power.csv] [--window 10]

log:    every --every s appends time, charge %, cell volts, charger on/off and CPU busy % (of all 4 cores) to a CSV.
report: for every stretch on battery (charger off) — a linear fit of the charge over time → average watts,
        W ≈ %/h / 100 × 3.7 Wh (1000 mAh × 3.7 V nominal), and the same per --window minutes next to the CPU load,
        so quiet and busy stretches can be told apart. The gauge's charge is a model estimate: expect ±10–15%.
"""
import argparse
import csv
import datetime as dt
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

BATTERY_WH = 3.7  # UPS-Lite: 1000 mAh LiPo × 3.7 V
POWER_GPIO = 4  # UPS-Lite power-good: high = charger connected


def cpu_times():
    with open("/proc/stat") as f:
        v = [int(x) for x in f.readline().split()[1:]]
    return sum(v), v[3] + v[4]  # total, idle + iowait


def charging():
    out = subprocess.run(["pinctrl", "get", str(POWER_GPIO)], capture_output=True, text=True).stdout
    return " hi " in out


def read_gauge(gauge, tries=8):
    """(charge %, volts) or (None, None): the gauge drops ~1 read in 3, so retry like knob.Gauge does."""
    for _ in range(tries):
        try:
            return gauge._read_once()
        except OSError:
            time.sleep(0.06)
    return None, None


def log(path, every):
    from knob import Gauge  # CW2015 on I2C (no log lines: we call its single read directly)

    gauge = Gauge()
    new = not os.path.exists(path)
    with open(path, "a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["time", "soc", "volts", "charging", "cpu"])
        prev = cpu_times()
        while True:
            time.sleep(every)
            total, idle = cpu_times()
            cpu = 100 * (1 - (idle - prev[1]) / max(total - prev[0], 1))
            prev = (total, idle)
            soc, volts = read_gauge(gauge)
            w.writerow([dt.datetime.now().isoformat(timespec="seconds"), f"{soc:.2f}" if soc is not None else "",
                        f"{volts:.3f}" if volts else "", int(charging()), f"{cpu:.1f}"])
            f.flush()


def fit(rows):
    """Least squares %/h over rows [(t, soc)]."""
    t0 = rows[0][0]
    xs = [(t - t0).total_seconds() / 3600 for t, _ in rows]
    ys = [s for _, s in rows]
    mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
    den = sum((x - mx) ** 2 for x in xs)
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / den if den else 0.0


def report(path, window):
    rows = []
    with open(path) as f:
        for r in csv.DictReader(f):
            if r["soc"]:
                rows.append((dt.datetime.fromisoformat(r["time"]), float(r["soc"]), r["charging"] == "1", float(r["cpu"])))
    stretches, cur = [], []
    for r in rows:
        if r[2]:  # charger on: ends a stretch
            if cur:
                stretches.append(cur)
            cur = []
        else:
            cur.append(r)
    if cur:
        stretches.append(cur)
    hm = lambda t: t.strftime("%H:%M")
    for s in stretches:
        minutes = (s[-1][0] - s[0][0]).total_seconds() / 60
        if minutes < 10:
            continue
        rate = -fit([(t, soc) for t, soc, _, _ in s])
        cpu = sum(r[3] for r in s) / len(s)
        print(f"{hm(s[0][0])}–{hm(s[-1][0])} ({minutes:.0f} мин) на батарее: {s[0][1]:.1f}% → {s[-1][1]:.1f}%, "
              f"{rate:.1f} %/ч ≈ {rate / 100 * BATTERY_WH:.2f} Вт, CPU {cpu:.0f}%")
        start = s[0][0]
        while start < s[-1][0]:
            end = start + dt.timedelta(minutes=window)
            part = [r for r in s if start <= r[0] < end]
            if len(part) >= 4:
                rate = -fit([(t, soc) for t, soc, _, _ in part])
                print(f"   {hm(start)}–{hm(end)}  {rate:5.1f} %/ч ≈ {rate / 100 * BATTERY_WH:.2f} Вт  "
                      f"CPU {sum(r[3] for r in part) / len(part):4.0f}%")
            start = end


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("cmd", choices=["log", "report"])
    p.add_argument("-o", "--out", default=os.path.expanduser("~/power.csv"))
    p.add_argument("--every", type=float, default=30.0, help="log: seconds between samples")
    p.add_argument("--window", type=int, default=10, help="report: minutes per row")
    a = p.parse_args()
    try:
        log(a.out, a.every) if a.cmd == "log" else report(a.out, a.window)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
