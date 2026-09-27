#!/usr/bin/env python3
"""
Probe for the Żabka Triki BLE token (nRF52810 + LSM6DSL) as a wireless knob: catch it, stream the IMU, print it.

    .venv/bin/python src/tools/triki_probe.py [--rate 26] [--scan 3] [--pause 5] [--idle 20]
    .venv/bin/python src/tools/triki_probe.py --watch      # never connect: when does it advertise, when does it sleep

Loop: scan --scan s for an advertiser named "TRIKI…" (the token only advertises after its button wakes it), sleep
--pause s, repeat. Found → connect, read the battery, start the IMU stream at --rate Hz and print twice a second:
frames/s, accel (g), gyro (deg/s), face up/down and the heading integrated from gyro Z. Face flips are logged on
their own line. After --idle s without motion the probe disconnects so the token can go back to sleep.

Protocol (github.com/Flopsstuff/triki, docs/guide/ble-protocol.md): Nordic UART Service; write the 8-byte start
command `20 10 00 D0 07 <rate LE16> 03` to RX, then TX notifies 14-byte frames `22 00` + gyro XYZ + accel XYZ
(int16 LE; gyro / 14.286 = deg/s, accel / 2048 = g). No stop command: the stream ends on disconnect.
Axes: +Z out of the PCB face, −Z out of the metal cap face; at rest the sky-facing axis reads +1 g.
"""
import argparse
import asyncio
import os
import struct
import subprocess
import sys
import time

from bleak import BleakClient, BleakScanner

NUS_RX = "6e400002-b5a3-f393-e0a9-e50e24dcca9e"
NUS_TX = "6e400003-b5a3-f393-e0a9-e50e24dcca9e"
BATTERY_LEVEL = "00002a19-0000-1000-8000-00805f9b34fb"
NAME_PREFIX = "triki"
HEADER = b"\x22\x00"
FRAME_LEN = 14
GYRO_SCALE = 14.286  # LSB per deg/s (±2000 dps)
ACCEL_SCALE = 2048.0  # LSB per g (±16 g)
SKIP_FRAMES = 20  # the first frames after start are noise
FACE_G = 0.8  # |accel Z| above this → lying on a face
STILL_DPS = 8.0  # gyro magnitude below this counts as no motion
TAP_G = 0.15  # |accel| this far from 1 g → log a jolt (button press?)


def log(msg):
    print(f"{time.strftime('%H:%M:%S')} {msg}", flush=True)


def rss_mb():
    with open(f"/proc/{os.getpid()}/status") as f:
        for line in f:
            if line.startswith("VmRSS:"):
                return int(line.split()[1]) / 1024
    return 0.0


def start_cmd(hz):
    return bytes([0x20, 0x10, 0x00, 0xD0, 0x07, hz & 0xFF, hz >> 8, 0x03])


async def find(scan_s):
    """First advertiser named TRIKI… within scan_s, or None."""
    return await BleakScanner.find_device_by_filter(
        lambda d, adv: (adv.local_name or d.name or "").lower().startswith(NAME_PREFIX), timeout=scan_s)


class Stream:
    def __init__(self, rate):
        self.rate = rate
        self.buf = bytearray()
        self.frames = 0
        self.window = 0
        self.last = None  # (gx, gy, gz, ax, ay, az) in deg/s and g
        self.face = None  # "PCB up" / "cap up" / None (on an edge)
        self.heading = 0.0
        self.moved = time.monotonic()

    def feed(self, _, data):
        self.buf.extend(data)
        while True:
            j = self.buf.find(HEADER)
            if j < 0:
                if len(self.buf) > 64:
                    del self.buf[:-1]
                return
            if j:
                log(f"[TX] not a frame: {bytes(self.buf[:j]).hex(' ')}")
            del self.buf[:j]
            if len(self.buf) < FRAME_LEN:
                return
            raw = struct.unpack_from("<6h", self.buf, 2)
            del self.buf[:FRAME_LEN]
            self.frames += 1
            if self.frames <= SKIP_FRAMES:
                continue
            self.window += 1
            self.frame([v / GYRO_SCALE for v in raw[:3]] + [v / ACCEL_SCALE for v in raw[3:]])

    def frame(self, v):
        gx, gy, gz, ax, ay, az = v
        self.last = v
        self.heading += gz / self.rate
        if (gx * gx + gy * gy + gz * gz) ** 0.5 > STILL_DPS:
            self.moved = time.monotonic()
        jolt = abs((ax * ax + ay * ay + az * az) ** 0.5 - 1.0)
        if jolt > TAP_G:
            log(f"[TAP] |a| off by {jolt:.2f} g  acc [{ax:5.2f} {ay:5.2f} {az:5.2f}]  "
                f"gyro [{gx:6.0f} {gy:6.0f} {gz:6.0f}]")
        face = "PCB up" if az > FACE_G else "cap up" if az < -FACE_G else None
        if face and face != self.face:
            log(f"[FLIP] {self.face or '—'} → {face}")
            self.face = face


async def session(dev, a):
    t0 = time.monotonic()
    gone = asyncio.Event()
    s = Stream(a.rate)
    async with BleakClient(dev, disconnected_callback=lambda _: gone.set()) as client:
        try:
            battery = (await client.read_gatt_char(BATTERY_LEVEL))[0]
        except Exception as e:
            battery = f"? ({e})"
        log(f"[BLE] connected in {time.monotonic() - t0:.1f} s, battery {battery}%, RSS {rss_mb():.0f} MB")
        await client.start_notify(NUS_TX, s.feed)
        await client.write_gatt_char(NUS_RX, start_cmd(a.rate), response=False)
        started = time.monotonic()
        while not gone.is_set():
            await asyncio.sleep(0.5)
            hz, s.window = s.window * 2, 0
            if s.last:
                gx, gy, gz, ax, ay, az = s.last
                log(f"{hz:3d} Hz  acc [{ax:5.2f} {ay:5.2f} {az:5.2f}] g  gyro [{gx:7.1f} {gy:7.1f} {gz:7.1f}] °/s  "
                    f"heading {s.heading:7.1f}°  {s.face or 'edge'}")
            if time.monotonic() - s.moved > a.idle:
                log(f"[BLE] {a.idle:.0f} s without motion — disconnecting")
                break
        if gone.is_set():
            log(f"[BLE] the token disconnected by itself after {time.monotonic() - started:.0f} s of streaming")
    log(f"[BLE] session {time.monotonic() - t0:.0f} s, {s.frames} frames, RSS {rss_mb():.0f} MB")


async def watch(duration, on_s=0.0, off_s=0.0):
    """Scan only, never connect. One line per second with adverts (count, RSSI), silences ≥ 2 s on their own line,
    then a summary of every on-air burst — shows whether the token sleeps or just advertises more slowly."""
    t0 = time.monotonic()
    sec = {"n": 0, "rssi": []}
    bursts, last = [], None  # bursts: [first, last, adverts]

    def on_adv(d, adv):
        nonlocal last
        if not (adv.local_name or d.name or "").lower().startswith(NAME_PREFIX):
            return
        now = time.monotonic()
        if last is None or now - last >= 2.0:
            if last is not None:
                log(f"[AIR]   … silent {now - last:.1f} s")
            bursts.append([now, now, 0])
        bursts[-1][1] = now
        bursts[-1][2] += 1
        last = now
        sec["n"] += 1
        sec["rssi"].append(adv.rssi)

    mode = f", scan {on_s:g} s / pause {off_s:g} s" if on_s else ", continuous scan"
    log(f"[AIR] listening for {duration:.0f} s without connecting{mode}")
    scanner = BleakScanner(on_adv)
    report = t0 + 1
    while time.monotonic() - t0 < duration:
        await scanner.start()
        stop_at = time.monotonic() + on_s if on_s else t0 + duration
        while (now := time.monotonic()) < stop_at and now - t0 < duration:
            await asyncio.sleep(min(0.1, max(stop_at - now, 0.01)))
            if time.monotonic() >= report:
                report += 1
                if sec["n"]:
                    r = sec["rssi"]
                    log(f"[AIR] {sec['n']:3d} adverts/s  RSSI {min(r)}…{max(r)}")
                    sec.update(n=0, rssi=[])
        await scanner.stop()
        if off_s:
            await asyncio.sleep(off_s)
    log(f"[AIR] summary: {len(bursts)} bursts on air")
    for first, end_, n in bursts:
        log(f"[AIR]   +{first - t0:6.1f} s … +{end_ - t0:6.1f} s  ({end_ - first:5.1f} s, {n} adverts)")


def kill_link(reason):
    """Drop our LE link to the token with an HCI disconnect reason of our choice (hcitool ledc), or "reset" — reset the
    controller, so the token sees the link just vanish (supervision timeout)."""
    if reason == "reset":
        return subprocess.run(["sudo", "hciconfig", "hci0", "reset"], capture_output=True, text=True)
    out = subprocess.run(["hcitool", "con"], capture_output=True, text=True).stdout
    handle = next((line.split("handle")[1].split()[0] for line in out.splitlines() if " LE " in line), None)
    if handle is None:
        log(f"[CMD] no LE connection in hcitool con: {out!r}")
        return None
    return subprocess.run(["sudo", "hcitool", "ledc", handle, str(int(reason, 0))], capture_output=True, text=True)


async def command(hexcmds, duration, hold=5.0, kill=None):
    """Connect, write raw commands to RX (comma-separated, 3 s apart), log every TX notification, stay connected
    `hold` s after the last one, then watch the air for `duration` s: does it answer, drop, reboot, sleep?"""
    dev = await find(15)
    if dev is None:
        log("[CMD] token not found (press its button)")
        return
    gone = asyncio.Event()
    t0 = time.monotonic()
    async with BleakClient(dev, disconnected_callback=lambda _: gone.set()) as client:
        log(f"[CMD] connected in {time.monotonic() - t0:.1f} s")
        await client.start_notify(NUS_TX, lambda _, d: log(f"[CMD] TX {bytes(d).hex(' ')}"))
        cmds = hexcmds.split(",")
        for i, c in enumerate(cmds):
            if i:
                await asyncio.sleep(3)
            await client.write_gatt_char(NUS_RX, bytes.fromhex(c), response=False)
            log(f"[CMD] sent {c}")
        sent = time.monotonic()
        try:
            await asyncio.wait_for(gone.wait(), hold)
            log(f"[CMD] the token dropped the link {time.monotonic() - sent:.1f} s after the command")
        except asyncio.TimeoutError:
            if kill:
                r = kill_link(kill)
                log(f"[CMD] killed the link: {kill} → {r and (r.returncode, (r.stdout + r.stderr).strip())}")
                try:
                    await asyncio.wait_for(gone.wait(), 10)
                except asyncio.TimeoutError:
                    log("[CMD] BlueZ still reports the link 10 s later")
            else:
                log(f"[CMD] still connected {hold:g} s later — disconnecting")
    if duration:
        await watch(duration, 0.2, 0.8)


async def main(a):
    if a.cmd:
        return await command(a.cmd, a.duration, a.hold, a.kill)
    if a.watch:
        return await watch(a.duration, a.on, a.off)
    log(f"[BLE] looking for the token: scan {a.scan:g} s, pause {a.pause:g} s (press its button)")
    while True:
        t0 = time.monotonic()
        dev = await find(a.scan)
        if dev is None:
            await asyncio.sleep(a.pause)
            continue
        log(f"[BLE] found {dev.name} after {time.monotonic() - t0:.1f} s of scanning")
        try:
            await session(dev, a)
        except Exception as e:
            log(f"[BLE] error: {type(e).__name__}: {e}")
        if a.once:
            return
        await asyncio.sleep(a.pause)


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--rate", type=int, default=26, choices=[26, 52, 104, 208, 416], help="IMU sample rate, Hz")
    p.add_argument("--scan", type=float, default=3.0, help="seconds per scan")
    p.add_argument("--pause", type=float, default=5.0, help="seconds between scans")
    p.add_argument("--idle", type=float, default=20.0, help="disconnect after this many seconds without motion")
    p.add_argument("--once", action="store_true", help="exit after the first session")
    p.add_argument("--watch", action="store_true", help="never connect: log adverts per second for --duration s")
    p.add_argument("--cmd", help="connect, write these hex commands (comma-separated) to RX, then --watch for --duration s")
    p.add_argument("--hold", type=float, default=5.0, help="cmd: stay connected this long after the last command")
    p.add_argument("--kill", help="cmd: then drop the link with this HCI disconnect reason (0x13, 0x15 …) or reset")
    p.add_argument("--duration", type=float, default=240.0, help="watch: seconds to listen")
    p.add_argument("--on", type=float, default=0.0, help="watch: scan for this many seconds, then restart (0 = one long scan)")
    p.add_argument("--off", type=float, default=0.0, help="watch: pause between scans, seconds")
    try:
        asyncio.run(main(p.parse_args()))
    except KeyboardInterrupt:
        sys.exit(0)
