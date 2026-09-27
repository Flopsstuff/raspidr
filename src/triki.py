"""
Żabka Triki BLE token (nRF52810 + LSM6DSL, a bottle cap) as a wireless knob. Runs inside knob.py in its own thread
with an asyncio loop (bleak → BlueZ over D-Bus) and hands gestures to the knob through a queue.

    button press (token asleep)  — it wakes up and advertises; caught → "summon" (greeting + listening), connect
    button press (connected)     — "summon" again: interrupts when the assistant is busy, greeting + listening when idle
    flip cap up / PCB up         — microphone off / on (held still on the new face for FLIP_HOLD_S)
    turn while lying flat        — volume, one step per STEP_DEG, clockwise (seen from above) = louder
    IDLE_S without motion        — disconnect ("link down"): the token sleeps ~180 s later (it can't while connected)

Life cycle — the token has no sleep command, it sleeps on its own ~180 s after it's disconnected or its button was
last pressed, and a press during those 180 s (it's still advertising, nothing on air tells a press apart) only
restarts the timer:
    asleep   — scan; a sighting is a button press → "summon" + connect
    active   — streaming; IDLE_S without motion or presses → disconnect → cooling
    cooling  — scan; gone for ASLEEP_S → asleep. Still on air AWAKE_TOO_LONG_S after the disconnect → its button was
               pressed meanwhile → reconnect quietly ("link up": no greeting, it would come minutes late). The encoder
               touched (poke()) while it's still on air → someone is at the speaker → reconnect quietly right away
    resume   — the link dropped by itself (out of range) → reconnect quietly on the next sighting
    retry    — connecting failed (it happens: "failed to discover services") → try again on the next sighting, no cue:
               the press was already answered with the greeting

Protocol (github.com/Flopsstuff/triki, docs/guide/ble-protocol.md, plus what we found on the Pi):
- Nordic UART Service. Start the IMU stream by writing `20 10 00 D0 07 <rate LE16> 03` to RX; it answers `21 00 00 00 00`
  and then notifies 14-byte frames on TX: `22 <button>` + gyro XYZ + accel XYZ, int16 LE. gyro / 14.286 = deg/s,
  accel / 2048 = g. Byte 1 is the button: 00 released, 01 pressed (upstream only knows `22 00` and drops the rest;
  some other firmware versions put a 0..15 frame counter there — ours is 3.2.1-A). The first ~20 frames are noise.
  `20 00 00 00 00 00 00` stops the stream (answer `21 00 00 00 00`), but then presses aren't reported either — so we
  never idle connected without the stream. Other RX opcodes (0x09, 0x0a …) are the Żappka app's session
  authentication: 0x0a/0x42/0x44 without its payload make the token reset (0x42 also leaves the LED on).
- Axes: +Z out of the PCB face, −Z out of the metal cap; at rest the sky-facing axis reads +1 g.
- A button press wakes the token for ~180 s of advertising (ADV_IND ~16/s, the payload never changes), and so does a
  disconnect; connected, it stays awake — hence the idle disconnect. The LED doesn't blink on presses while connected.

Scanning: 0.2 s every second. The Pi's Broadcom controller stops delivering advertising reports a few seconds into a
continuous scan (all devices go quiet until the scan restarts — BlueZ does that only every 10.24 s), short scans never
live long enough to hit it. The token is caught within ≤1 s in 95% of cases, radio busy 20% of the time.
"""
import asyncio
import queue
import struct
import subprocess
import threading
import time

NUS_RX = "6e400002-b5a3-f393-e0a9-e50e24dcca9e"
NUS_TX = "6e400003-b5a3-f393-e0a9-e50e24dcca9e"
BATTERY_LEVEL = "00002a19-0000-1000-8000-00805f9b34fb"
RATE_HZ = 26
FRAME_LEN = 14
GYRO_SCALE = 14.286  # LSB per deg/s (±2000 dps)
ACCEL_SCALE = 2048.0  # LSB per g (±16 g)
SKIP_FRAMES = 20  # the first frames after the start are noise

SCAN_ON_S, SCAN_OFF_S = 0.2, 0.8
ASLEEP_S = 5.0  # not seen this long → it's asleep: the next sighting is a button press
IDLE_S = 60.0  # connected without motion or presses → disconnect
POKE_S = 3.0  # an encoder touch this recent takes a cooling token back (spans a scan window that missed it)
AWAKE_TOO_LONG_S = 190.0  # still advertising this long after a disconnect → its button was pressed (measured 172–180 s)
MOVING_DPS = 15.0  # gyro magnitude above this (after the bias) counts as being handled
FACE_G = 0.8  # |accel Z| above this → lying on a face
FLIP_HOLD_S = 0.4  # a new face counts once it's held this long, still
FLIP_STILL_DPS = 40.0
STEP_DEG = 15.0  # a volume step per this much turning: 24 steps ≈ one full turn
TURN_DEADBAND_DPS = 5.0  # slower than this isn't turning (the gyro bias alone is ~5 deg/s on some axes)
TURN_RESET_S = 1.0  # a partial step is forgotten after this long without turning
BIAS_ALPHA = 0.02  # gyro bias follows the readings while the token lies still
PRESS_GAP_S = 0.3


def ble_connected(prefix):
    """Addresses of devices named prefix… that BlueZ keeps connected (e.g. left over by a killed process)."""
    out = subprocess.run(["bluetoothctl", "devices", "Connected"], capture_output=True, text=True).stdout
    return [line.split()[1] for line in out.splitlines()
            if len(line.split()) > 2 and " ".join(line.split()[2:]).lower().startswith(prefix)]


class Motion:
    """Frames → gestures. feed() is called for every frame; events go to emit(name, arg)."""

    def __init__(self, emit):
        self.emit = emit
        self.bias = None  # gyro zero offset, deg/s: the first still frame, then an EMA while still
        self.face = None  # "pcb" / "cap": the last face held still
        self.cand, self.cand_t = None, 0.0
        self.turn, self.turn_t = 0.0, 0.0
        self.button = False
        self.press_t = 0.0
        self.active_t = time.monotonic()

    def feed(self, button, gyro, accel):
        now = time.monotonic()
        ax, ay, az = accel
        level = abs((ax * ax + ay * ay + az * az) ** 0.5 - 1.0) < 0.05  # no linear acceleration
        if self.bias is None:
            if not (level and sum(r * r for r in gyro) ** 0.5 < MOVING_DPS):
                return  # handled right from the connect: wait for a still frame to learn the offset
            self.bias = list(gyro)
        g = [r - b for r, b in zip(gyro, self.bias)]
        speed = (g[0] ** 2 + g[1] ** 2 + g[2] ** 2) ** 0.5
        if speed < MOVING_DPS and level:
            self.bias = [b + BIAS_ALPHA * (r - b) for b, r in zip(self.bias, gyro)]
        else:
            self.active_t = now

        if button and not self.button and now - self.press_t > PRESS_GAP_S:
            self.press_t = self.active_t = now
            self.emit("summon", None)
        self.button = button

        face = "pcb" if az > FACE_G else "cap" if az < -FACE_G else None
        if face != self.cand:
            self.cand, self.cand_t = face, now
        elif face and face != self.face and speed < FLIP_STILL_DPS and now - self.cand_t >= FLIP_HOLD_S:
            if self.face is not None:
                self.emit("face", face)
            self.face = face
            self.turn = 0.0

        # turning around the vertical axis: only while lying on a face; seen from above, clockwise is −Z rotation
        # when the PCB is up, +Z when the cap is up
        if face and abs(g[2]) > TURN_DEADBAND_DPS:
            self.turn += (-g[2] if face == "pcb" else g[2]) / RATE_HZ
            self.turn_t = now
            while abs(self.turn) >= STEP_DEG:
                d = 1 if self.turn > 0 else -1
                self.turn -= d * STEP_DEG
                self.emit("turn", d)
        elif now - self.turn_t > TURN_RESET_S:
            self.turn = 0.0

    def idle_for(self):
        return time.monotonic() - self.active_t


class Triki:
    """Background BLE client. Events for the knob land in self.events as (name, arg):
    ("summon", None), ("face", "cap"|"pcb"), ("turn", ±1), ("link", "up"|"down"), ("log", text).
    "link up" comes only for a quiet reconnect after cooling or a lost link (a summon is its own feedback, a retry
    after a failed connect follows one), "link down" when we let it go or lose it."""

    def __init__(self, name_prefix="triki"):
        self.prefix = name_prefix.lower()
        self.events = queue.Queue()
        self.loop = None
        self.stop_ev = None
        self.poke_t = 0.0  # the encoder was last touched: take the token back while cooling, keep it while connected
        self.thread = threading.Thread(target=self._thread, daemon=True)
        self.thread.start()

    def log(self, msg):
        self.events.put(("log", "[TRIKI] " + msg))

    # ------------------------------------------------------------ thread / loop

    def _thread(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self.stop_ev = asyncio.Event()
        try:
            self.loop.run_until_complete(self._main())
        except Exception as e:
            self.log(f"stopped: {type(e).__name__}: {e}")

    def poke(self):
        """Any thread: the encoder was turned or pressed."""
        self.poke_t = time.monotonic()

    def close(self, timeout=4.0):
        """Disconnect cleanly (a connection left behind keeps the token awake and invisible)."""
        if self.loop and self.stop_ev:
            self.loop.call_soon_threadsafe(self.stop_ev.set)
            self.thread.join(timeout)

    # ------------------------------------------------------------ scan → connect → stream

    async def _main(self):
        from bleak import BleakScanner  # heavy (dbus-fast): only in this thread

        for addr in ble_connected(self.prefix):
            subprocess.run(["bluetoothctl", "disconnect", addr], capture_output=True)
            self.log("dropped a connection left over from before")
        self.log(f"looking for \"{self.prefix}…\": scanning {SCAN_ON_S:g} s every {SCAN_ON_S + SCAN_OFF_S:g} s")
        found = {"dev": None}
        last_seen = None
        state, off_t = "asleep", 0.0  # off_t: when we disconnected (cooling)

        def on_adv(d, adv):
            nonlocal last_seen
            if (adv.local_name or d.name or "").lower().startswith(self.prefix):
                last_seen = time.monotonic()
                found["dev"] = d

        scanner = BleakScanner(on_adv)
        while not self.stop_ev.is_set():
            found["dev"] = None
            await scanner.start()
            await self._sleep(SCAN_ON_S)
            await scanner.stop()
            dev, now = found["dev"], time.monotonic()
            if state == "cooling":
                if last_seen is None or now - last_seen > ASLEEP_S:
                    state = "asleep"
                    self.log(f"the token fell asleep {now - off_t:.0f} s after the disconnect — waiting for a press")
                elif dev and now - off_t > AWAKE_TOO_LONG_S:
                    state = "resume"
                    self.log(f"still on air {now - off_t:.0f} s after the disconnect — its button was pressed meanwhile")
                elif dev and now - self.poke_t < POKE_S:
                    state, self.poke_t = "resume", 0.0  # one touch takes it back once
                    self.log("the knob was touched while the token is still awake — taking it back")
            if dev is None or state not in ("asleep", "resume", "retry"):
                await self._sleep(SCAN_OFF_S)
                continue
            if state == "asleep":
                self.log(f"press: caught {dev.name}")
                self.events.put(("summon", None))
            else:
                self.log("reconnecting quietly")
            reason = await self._session(dev, cue=state == "resume")
            last_seen = off_t = time.monotonic()
            state = {"lost": "resume", "error": "retry"}.get(reason, "cooling")

    async def _sleep(self, s):
        try:
            await asyncio.wait_for(self.stop_ev.wait(), s)
        except asyncio.TimeoutError:
            pass

    async def _session(self, dev, cue=False):
        """Connected until idle / lost / stop. → "idle" | "lost" | "stop" | "error"."""
        from bleak import BleakClient

        t0 = time.monotonic()
        gone = asyncio.Event()
        motion = Motion(lambda name, arg: self.events.put((name, arg)))
        buf = bytearray()
        frames = 0

        def on_tx(_, data):
            nonlocal frames
            buf.extend(data)
            while len(buf) >= 2:
                j = next((i for i in range(len(buf) - 1) if buf[i] == 0x22 and buf[i + 1] in (0, 1)), -1)
                if j < 0:
                    del buf[:-1]
                    return
                del buf[:j]
                if len(buf) < FRAME_LEN:
                    return
                button = buf[1] == 1
                v = struct.unpack_from("<6h", buf, 2)
                del buf[:FRAME_LEN]
                frames += 1
                if frames > SKIP_FRAMES:
                    motion.feed(button, [x / GYRO_SCALE for x in v[:3]], [x / ACCEL_SCALE for x in v[3:]])

        try:
            async with BleakClient(dev, disconnected_callback=lambda _: gone.set(), timeout=10.0) as client:
                try:
                    battery = f", battery {(await client.read_gatt_char(BATTERY_LEVEL))[0]}%"
                except Exception:
                    battery = ""
                await client.start_notify(NUS_TX, on_tx)
                start = bytes([0x20, 0x10, 0x00, 0xD0, 0x07, RATE_HZ & 0xFF, RATE_HZ >> 8, 0x03])
                await client.write_gatt_char(NUS_RX, start, response=False)
                self.log(f"connected in {time.monotonic() - t0:.1f} s{battery}")
                if cue:
                    self.events.put(("link", "up"))
                while True:
                    if self.stop_ev.is_set():
                        reason = "stop"
                        break
                    if gone.is_set():
                        reason = "lost"
                        break
                    motion.active_t = max(motion.active_t, self.poke_t)  # the encoder in use: keep the remote
                    if motion.idle_for() > IDLE_S:
                        reason = "idle"
                        break
                    await self._sleep(0.5)
        except Exception as e:
            self.log(f"link error: {type(e).__name__}: {e}")
            return "error"
        what = {"stop": "disconnecting (stop)", "lost": "link lost",
                "idle": f"{IDLE_S:.0f} s without motion — disconnecting so it can sleep"}[reason]
        self.log(f"{what}; session {time.monotonic() - t0:.0f} s")
        if reason in ("idle", "lost"):
            self.events.put(("link", "down"))
        return reason
