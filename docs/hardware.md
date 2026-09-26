# Hardware

Context about the device the project will run on. Collected on 2026-09-26 from the live system
and from the old project `~/sharm` (which also contains `client/pizero2w/`, the previous software for this same hardware).

## Access

- `ssh $PI_HOST` (host from `.env`), passwordless sudo, Wi-Fi only.
- Old project: `~/sharm` on the Pi itself (git, binary assets in git-lfs). Python venv: `~/venv`
  (created with `--system-site-packages`, needed for the system `python3-pigpio` / `python3-smbus`).
- `~/.env` exports `PV_ACCESS_KEY` (Picovoice); `source ~/.env` before running the wake word.
- **RaspiDR:** `~/$PI_DIR` (default `~/raspidr`, deployed with `./deploy.sh` from the Mac), venv `.venv`, keys in `.env` there.
  Architecture: [architecture.md](architecture.md); wake word training: [wakeword_training.md](wakeword_training.md).

## Board and OS

| | |
|---|---|
| Board | Raspberry Pi Zero 2 W Rev 1.0 |
| RAM | 416 MB + 512 MB swap (`/var/swap`) — **memory is tight**, can't handle heavy processes |
| OS | Debian 12 bookworm, arm64, kernel `6.12.47+rpt-rpi-v8` |
| Python | 3.11.2 |
| Wi-Fi | `wlan0` |

The system hasn't been updated since 2025-09-30 (last `apt`). The clock uses `fake-hwclock`; after a long
downtime the time at boot is "old" at first, then syncs via NTP.

### `/boot/firmware/config.txt` (relevant parts)

```
dtparam=i2c_arm=on
dtparam=i2s=on
dtparam=spi=on
enable_uart=1
dtparam=audio=on
dtoverlay=vc4-kms-v3d
arm_64bit=1
[all]
dtoverlay=i2s-mmap
dtoverlay=wm8960-soundcard
dtoverlay=w1-gpio          # 1-Wire on GPIO4 (default), no sensors at the moment
```

## Components

| Component | Interface | Pins (BCM) | Status as of 2026-09-26 |
|---|---|---|---|
| WM8960 Audio Codec HAT (SB Components) | I2S + I2C1 `0x1a` | 18, 19, 20, 21 (I2S), 2/3 (I2C), 17 | ✅ working, `card 0` |
| TPA3118 amplifier | analog, after the WM8960 | — | — |
| UPS-Lite V1.3 (XiaoJ), CW2015 fuel gauge | I2C1 `0x62` + power-good | 2/3 (I2C), 4 | ✅ working |
| NeoPixel ring, 7 × WS2812B, GRB | SPI MOSI | 10 | ✅ SPI enabled |
| Rotary encoder with button | GPIO via `pigpiod` | A=27, B=22, BTN=23 | — |
| 1-Wire | `w1-gpio` | 4 | overlay loaded, no devices |

### Pinout (40-pin header)

```
+-----+-----------+---------+     +-----+-----------+---------+
| Pin |   Name    |         |     | Pin |   Name    |         |
+-----+-----------+---------+     +-----+-----------+---------+
|  1  |  3.3V     |         |     |  2  |  5V       |         |
|  3  |  SDA1     | WM8960* |     |  4  |  5V       | Neopix  |
|  5  |  SCL1     | WM8960* |     |  6  |  GND      | Neopix  |
|  7  |  GPIO4    | UPS     |     |  8  |  TXD0     |         |
|  9  |  GND      |         |     | 10  |  RXD0     |         |
| 11  |  GPIO17   | WM8960_ |     | 12  |  GPIO18   | WM8960  |
| 13  |  GPIO27   | Enc_A   |     | 14  |  GND      | Enc_Gnd |
| 15  |  GPIO22   | Enc_B   |     | 16  |  GPIO23   | Enc_Btn |
| 17  |  3.3V     | Enc_Vcc |     | 18  |  GPIO24   | WM8960? |
| 19  |  MOSI     | Neopix  |     | 20  |  GND      | WM8960  |
| 21  |  MISO     |         |     | 22  |  GPIO25   |         |
| 23  |  SCLK     |         |     | 24  |  CE0      |         |
| 25  |  GND      | WM8960  |     | 26  |  CE1      |         |
| 27  |  SDA0     |         |     | 28  |  SCL0     |         |
| 29  |  GPIO5    |         |     | 30  |  GND      |         |
| 31  |  GPIO6    |         |     | 32  |  GPIO12   |         |
| 33  |  GPIO13   |         |     | 34  |  GND      | WM8960  |
| 35  |  GPIO19   | WM8960  |     | 36  |  GPIO16   |         |
| 37  |  GPIO26   |         |     | 38  |  GPIO20   | WM8960  |
| 39  |  GND      |         |     | 40  |  GPIO21   | WM8960  |
+-----+-----------+---------+     +-----+-----------+---------+
```

Free: GPIO 5, 6, 12, 13, 16, 24(?), 25, 26, UART 14/15 (taken by `serial-getty@ttyS0`),
MISO/SCLK/CE0/CE1 (SPI is enabled, but only MOSI is used).

I2C buses: `/dev/i2c-1` (GPIO2/3, all devices are here), `/dev/i2c-2` (HDMI DDC, empty).

## WM8960 Audio HAT

- Info: https://learn.sb-components.co.uk/Audio-Codec-HAT-for-Raspberry-Pi
- Drivers:
  - codec: the **stock** `snd-soc-wm8960.ko` from the RPi kernel (`kernel/sound/soc/codecs/`);
  - card glue: since 2026-09-26, the **stock** `snd_soc_simple_card` from the kernel. The custom
    `snd_soc_wm8960_soundcard` (fork https://github.com/Fl0p/WM8960-Audio-HAT, dkms; a copy of simple-card
    patched for 6.12) is blacklisted in `/etc/modprobe.d/blacklist-wm8960-soundcard.conf`: it
    registered under the same name `asoc-simple-card`, so the stock one didn't load. Rollback: delete the file,
    reboot.
  - the `wm8960-soundcard.dtbo` overlay and the `wm8960-soundcard.service` service come from the Waveshare repository.
  - After switching to the stock glue, the right microphone channel started responding weakly to voice
    (see below); before that it was a flat noise floor. The reason for the difference is unknown.
- At boot, the `wm8960-soundcard.service` service (`/usr/bin/wm8960-soundcard`) waits for `0x1a` on I2C1,
  applies the overlay, recreates the symlinks `/etc/asound.conf` → `/etc/wm8960-soundcard/asound.conf`
  and `/var/lib/alsa/asound.state`, then runs `alsactl restore`. Log: `/var/log/wm8960-soundcard.log`.
- ALSA: `pcm.!default` = asym, playback → `dmix` (`ipc_key 555555`) on `hw:wm8960soundcard`,
  capture → `dsnoop` 2ch (`ipc_key 666666`). Simultaneous playback and capture work.
- Card 0 mixer (`amixer -c 0`): `Speaker` (127 = +6 dB, see below), `Headphone` (0%), `Playback`,
  `Capture`. The card itself has no `Master` control, but `amixer set Master` (without `-c`, default
  device) works; the old `test_ai.py` used it to change the volume from the encoder. Hardware volume of the
  codec: `amixer -c 0 set Speaker 5%+`.
- The WM8960 is **write-only** over I2C: `i2cdetect -r` doesn't show it; with the driver loaded, the
  address shows up as `UU`.

Microphones (soldered on the HAT, inputs `LINPUT1`/`RINPUT1`, boost +29 dB, `Capture` +12 dB).
According to the schematic of the Waveshare reference board (https://files.waveshare.com/upload/f/fa/WM8960_Audio_HAT_Schematic.pdf;
the SB Components board appears to be a copy of it): two analog MEMS `AOS3729A`, powered directly from 3.3 V (not from
`MICBIAS`), each through its own chain: MIC2 → L5 → C21 → `LINPUT1` (L), MIC1 → L3 → C14 → `RINPUT1` (R).
`LINPUT2/3`, `RINPUT2/3` are not connected. The board is labeled LEFT / RIGHT. There is also a K1 button on GPIO17.
- How we investigated (2026-09-26): covering a mic with a finger barely works (sound gets around through the board); more reliable
  are scratching with a fingernail and disabling the input PGAs/`DATSEL` with a tone from the speakers as the source.
  Ruled out: the driver (the codec is stock, and the glue was also switched to stock), `MICB` (MEMS powered from 3.3 V),
  the `LINPUT2/3`/`RINPUT2/3` inputs (empty, −73 dBFS), a channel swap in I2S.
- **Diagnosis result (registers, 2026-09-26):** the whole signal goes through the **left** input PGA
  (`LINPUT1`): disabling the left PGA removes the sound, disabling the right one changes nothing;
  `DATSEL` moves the signal between channels as expected, i.e. I2S/LRCLK/driver are fine.
  On the board: **MIC1 (labeled RIGHT) → `LINPUT1`** (left channel); covering it mutes the sound;
  **MIC2 (labeled LEFT) doesn't reach the codec** on any input. The routing differs from the Waveshare
  schematic; MIC2 has an open circuit (the soldering looks intact). The −50 dBFS in the right channel is leakage inside
  the codec from the left one, not a microphone.
- **Set and saved:** `ADC Data Output Select` = `Left Data = Left ADC; Right Data = Left ADC`
  (`DATSEL=1`): the working microphone is recorded to both channels (mono duplicate), so recordings play from both speakers.
- Test scripts: `src/tools/micmeter.py` (L/R level on the ring + record/playback),
  `src/tools/micprobe.py` (step-by-step "which mic goes to which channel" test with color prompts).
  Checking all hardware at once: `src/tools/hwtest.py`.
- **PulseAudio** (`pulseaudio.socket`, a user service that starts on every ssh login) grabs the
  card → `aplay`/`arecord` via dmix/dsnoop get `Device or resource busy`, and on startup it
  resets `Speaker` to its own volume. Before working with audio:
  `systemctl --user stop pulseaudio.socket pulseaudio.service`.
- The volume is set to maximum and saved in `/etc/wm8960-soundcard/wm8960_asound.state`
  (the original is `.orig` next to it): `Speaker` 127 (+6 dB), `Speaker AC/DC` 5.
- The left channel has a strong noise floor in silence, −20…−35 dBFS with a ~100 Hz hum.
- The loop "1 kHz tone from the speakers → microphone" at `Speaker` 127 gives ~−7 dBFS in the working channel;
  it can serve as an automatic check of the capture path without a human.

Check:
```bash
aplay -l
aplay /usr/share/sounds/alsa/Front_Center.wav
arecord -d 3 -f cd -t wav /tmp/test.wav && aplay /tmp/test.wav
```

## UPS-Lite battery (CW2015)

UPS-Lite V1.3 board with a CW2015 fuel gauge.

- Instructions: https://github.com/linshuqin329/UPS-Lite/blob/master/UPS-Lite_V1.3_CW2015/Instructions%20for%20UPS-Lite%20V1.3.pdf
- The board sits under the Pi and makes contact via **pogo pins** to the header solder joints: 5V, GND, SDA (pin 3), SCL (pin 5),
  GPIO4 (pin 7). The battery is **soldered on**, so it can't be disconnected (to power-cycle the CW2015).
- I2C1, address `0x62`. Registers: `0x00` VERSION (ours is `0x70`), `0x02` VCELL, `0x04` SOC, `0x0A` MODE.
  - Words are read via `read_word_data` and need a byte swap (`struct.unpack("<H", struct.pack(">H", raw))`).
  - VCELL: `raw * 0.305 / 1000` → volts (≈4.04 V on battery, ≈4.15 V while charging).
  - SOC: `raw / 256` → percent.
  - Quick-start: `write_word_data(0x62, 0x0A, 0x30)` on initialization.
- **GPIO4 = power-good**: `HIGH` means external power (micro-USB on the UPS) is connected, `LOW` means running on
  battery. Verified both ways, including with `w1-gpio` loaded on the same pin.
- Battery on 2026-09-26: ~95–97% in the morning; 85% (3.96 V, on battery) in the evening, after a reboot the gauge
  answered again with the board untouched.
- MODE `0x0A`: bits `0xC0` = sleep (readings freeze). `knob.py` wakes the gauge with `0x00` if it finds it asleep;
  it doesn't quick-start (`0x30`), which would throw away the gauge's learned estimate. `hwtest.py ups` does quick-start.
- RaspiDR reads it in `src/knob.py` every 30 s and watches GPIO4 (pigpio callback, 50 ms glitch filter); see
  [architecture.md](architecture.md#encoder-knob-service).

⚠️ The main known issue is **the contact of the signal pogo pins**. Power still gets through (the Pi
runs on battery), but `0x62` isn't visible on the bus (`Errno 121 / EIO`, `i2cdetect` is empty). Software and
drivers have nothing to do with it; the fix is to reseat the board straight. If the UPS "disappears", first
reseat the board, and only then look into software. To check: jumper wires for SDA/SCL/GPIO4 to pins 3/5/7.

Check:
```bash
sudo /usr/sbin/i2cdetect -y 1          # should show UU at 0x1a and 62 at 0x62
sudo /usr/sbin/i2cget -y 1 0x62 0x04 w # SOC (bytes swapped)
pinctrl get 4                          # hi = charger connected
```

History: until 2025-09-30 there was a Waveshare UPS HAT (C) based on the INA219 (`0x43`); a deleted
`INA219.py` from it remains in git — it has nothing to do with the current hardware.

## NeoPixel ring (RGB LED)

- 7 WS2812B LEDs, color order **GRB**, DIN → GPIO10 (MOSI, pin 19), power 5V/GND (pins 4/6).
- The `adafruit-circuitpython-neopixel` library (+ `Adafruit-Blinka`) outputs via SPI,
  so `dtparam=spi=on` is required, and **sudo is not needed**.
- Working parameters: `neopixel.NeoPixel(board.D10, 7, brightness=0.2, auto_write=True, pixel_order=neopixel.GRB)`.
- Earlier attempts used GPIO14, GPIO12 and `pigpio`/`rpi_ws281x`; we settled on SPI MOSI.
- Turn it off when done: `pixels.fill((0, 0, 0)); pixels.deinit()`.

## Rotary encoder with button

- A = GPIO27 (pin 13), B = GPIO22 (pin 15), button = GPIO23 (pin 16), VCC = 3.3V (pin 17), GND (pin 14).
- All three inputs use the internal `PUD_UP` pull-up; the button is active-low.
- Driven via `pigpiod` (callback on `EITHER_EDGE`), glitch filter **100 µs** on all three pins.
- Decoding (from `rotary_encoder.py`): `(A<<1)|B` states accumulate in a buffer and are processed only
  on return to the stable `11`; direction is determined by the pair (previous unique state, current):
  `0001/0111/1110/1000` → CCW, `0010/1011/1101/0100` → CW. One click = one step.
  `pulses_per_rotation=80` is a nominal value for converting to degrees.
- The button watchdog is disabled by default (`watchdog_ms=0`).
- RaspiDR: `src/knob.py` decodes with a quadrature table (±1 per edge, 4 edges = one detent) and polls the button
  every 20 ms (two low reads = pressed); see [architecture.md](architecture.md#encoder-knob-service).

## pigpiod

- Service with the override `/etc/systemd/system/pigpiod.service.d/override.conf`:
  ```
  ExecStart=/usr/bin/pigpiod -t 0 -s 10 -x 0x08C00010
  ```
  - `-t 0`: PWM as the clock source, so it **doesn't conflict with I2S audio** (with `-t 1` the audio broke).
  - `-s 10`: sample rate 10 µs.
  - `-x 0x08C00010`: mask of the GPIOs pigpio is **allowed** to touch: 4, 22, 23, 27. This is a restriction
    for pigpio, not protection of the pin from the kernel: `w1-gpio` still holds GPIO4 in the kernel.
- The `RotaryEncoder` and `UPS` classes crash if `pigpiod` isn't running.

## Software from the old project

The previous code for this hardware lives on the Pi in `~/sharm/client/pizero2w/`.

| File | What it does |
|---|---|
| `rotary_encoder.py` | `RotaryEncoder` class, callbacks `set_rotation_callback(dir, pos, deg, rot)`, `set_button_callback(level, tick)` |
| `ups.py` | `UPS` class (context manager, optional background thread), events `on_battery_change`, `on_power_change`, `on_low_battery` |
| `wake_word_detector.py` | `WakeWordDetector`: Picovoice Porcupine + PyAudio, `.ppn` next to the script |
| `test_ai.py` | integration: wake word `hey-pee-dar` → sound `sounds/hello_*.wav` + blue flash; `hey-pipi` or the button → radio via `mpv`; encoder → volume |
| `test_ups.py`, `test_ups_events.py`, `test_encoder.py`, `test_neopixel.py` | manual hardware smoke tests |
| `firstboot.sh` | initial setup of a clean image (config.txt, packages, pigpiod override, WM8960 driver, reboot) |

Python packages in `~/venv`: `adafruit-circuitpython-neopixel`, `Adafruit-Blinka`, `pigpio`, `smbus2`
(+ system `smbus`), `pvporcupine 3.0.5`, `PyAudio`, `RPi.GPIO`/`rpi-lgpio`, `rpi-ws281x`.

The old code launched audio subprocesses (`aplay`, `mpv`) with `XDG_RUNTIME_DIR=/tmp/xdg_runtime`.

## Gotchas and observations

- `i2cdetect`/`i2cget` live in `/usr/sbin`; a regular user over ssh doesn't have them in `PATH`,
  so call `sudo /usr/sbin/i2cdetect`.
- A Python I2C scan via `read_byte` doesn't see devices claimed by a driver (`0x1a`) and can't tell
  them apart from an empty address; `i2cdetect` is more reliable.
- `dmesg` shows constant Wi-Fi errors (`brcmfmac ... timeout`, `mmc1: Controller never released inhibit
  bit(s)`, `scan error -110`): SDIO to the Wi-Fi chip is unstable. The connection stays up, but ssh
  may hang.
- Power is stable: `vcgencmd get_throttled` = `0x0` both on battery and on the charger.
- Idle temperature ~47 °C.
