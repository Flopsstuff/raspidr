# Power efficiency experiments

This document is a backlog of ideas to measure before changing the architecture. The goal is to reduce idle power draw
and extend battery life without making wake-word detection less reliable or increasing response latency noticeably.

No estimate below should be treated as a guaranteed battery-life improvement. CPU usage is useful for locating work, but
power in watts is the metric that decides whether an experiment is worthwhile.

## Current baseline

Measurements from 2026-09-26 show the main continuous costs while waiting for the wake word:

- openWakeWord takes about 27 ms of CPU per 80 ms audio frame when the quiet gate is open: approximately 21 ms for the
  embedding model, 4 ms for the mel spectrogram and only 2 ms for the custom MLP;
- `arecord` uses about 8% of one core, apparently for ALSA conversion from 48 kHz to 16 kHz (12.8% measured in
  isolation; reduced to about 1% on 2026-09-27, see below);
- `pigpiod` uses about 4.5% of one core;
- `knob.py` uses about 2% of one core;
- the assistant uses 164–204 MB RSS, while the whole board has 416 MB RAM.

The existing measurements need to be repeated. The wake-word section reports 34% of one core after enabling `QuietGate`,
while the summary table reports 59% during wake-word waiting. Ambient sound and gate-open time may explain the difference.

## How to measure

Measure these states separately to identify the fixed platform cost and the cost of each service:

1. Both RaspiDR services stopped.
2. Only `raspidr-knob` running.
3. Both services running with the wake word switched off, so the microphone is closed.
4. Normal IDLE in a quiet room, with the gate mostly closed.
5. IDLE with speech or other sound keeping the gate open.
6. LISTEN with Silero VAD running but no speech.
7. Silent playback path versus an explicitly shut-down amplifier, if the hardware permits it.

For every experiment record:

- average watts and battery discharge rate;
- whole-system and per-process CPU;
- assistant RSS/PSS and swap activity;
- wake-word recall, false triggers per hour and trigger latency;
- response latency and any dropped audio frames.

`src/tools/powerlog.py` is suitable for long comparisons, but the CW2015 has no current sensor and its state-of-charge
estimate has roughly 10–15% uncertainty. Small improvements require alternating, multi-hour A/B runs under similar battery,
temperature and network conditions. An inline USB power meter or an INA219/INA226 would produce much better data.

## Priority 1: low-risk experiments

### Remove capture resampling

The assistant asks `arecord` for 16 kHz mono through the ALSA `default` device. The default capture path is a two-channel
`dsnoop` device, and the codec path appears to run at 48 kHz. Test these alternatives on the Pi:

- direct 16 kHz capture from `hw:wm8960soundcard`, if the driver and codec accept it;
- a dedicated 16 kHz capture PCM instead of the general-purpose `default` PCM;
- direct 48 kHz capture followed by an efficient 3:1 decimator;
- removing `dsnoop` from capture if only one process ever owns the microphone.

The upper bound is the approximately 8% of one core currently attributed to `arecord`. Detection accuracy must be checked
because a different resampler or decimator changes the wake-word input.

**Result (2026-09-27, applied).** The cost came from `defaults.pcm.rate_converter "samplerate"` (libsamplerate) in
`/etc/wm8960-soundcard/asound.conf`. CPU of `arecord` alone, 30 s per path, wake word muted:

| Capture path | CPU of one core |
|---|---|
| `default` → `dsnoop` 48k → `samplerate` → 16k (before) | 12.8% |
| same, `samplerate_linear` / `lavrate_faster` | 1.6% / 3.2% |
| same, `speexrate` | 2.0% |
| same, `linear` | 1.1% |
| `dsnoop` 48k stereo, no conversion | 0.3% |
| `hw` at 16k (codec decimates) | 0.1–0.8% |

Capturing at 16 kHz directly from the codec is not usable: the WM8960 runs capture and playback at the same rate. With the
microphone opened first, `dmix` plays at 16 kHz as well (TTS loses everything above 8 kHz); with playback opened first,
a 16 kHz capture fails with `Slave PCM not usable`. Switching the card to 16 kHz in IDLE and back to 48 kHz for playback
would save less than 1% of a core over `linear` at the cost of closing and reopening the microphone around every sound.

Accuracy was compared with a loopback run: the speaker played 52 TTS wake phrases and 32 similar-phrase negatives while
four `arecord` processes captured the same sound through different paths; the model scored each slot offline (onnx).

| Resampler | Positives ≥ 0.5 | Positive median | Negatives ≥ 0.5 (max) |
|---|---|---|---|
| `samplerate` (before) | 38/52 | 0.994 | 0/32 (0.07) |
| `speexrate` | 36/52 | 0.998 | 0/32 (0.20) |
| `linear` | 38/52 | 0.996 | 0/32 (0.15) |
| scipy `resample_poly` offline reference | 38/52 | 0.996 | 0/32 (0.05) |

Borderline files flip across the threshold even between two high-quality resamplers (`samplerate` vs scipy differ on
3 files), so differences of a couple of files are noise of this test. `linear` shows no measurable loss. Both capture
channels are identical (correlation 1.00, `DATSEL=1`), so the plug downmix doesn't matter.

Applied: `rate_converter "linear"` in `pcm.capture` only; playback keeps `samplerate`. The original file is
`/etc/wm8960-soundcard/asound.conf.bak-samplerate`. Still to confirm on live speech: recall and false triggers per hour
over normal use.

### Tune `QuietGate`

Test the gate against recorded positives, recorded negatives and live speech at different distances:

- raise the margin from 8 dB to 10 or 12 dB;
- shorten the 1.5 s hold time;
- reduce the 2 s pre-roll toward the minimum context needed by the model;
- calculate energy in a speech band so that the known approximately 100 Hz microphone hum does not open the gate;
- add hysteresis or a very cheap speech/non-speech stage before openWakeWord.

The pre-roll deserves special attention: opening the gate can release about 25 queued frames and run them through the models
in one burst. A shorter pre-roll may save work on incidental sounds, but it may also remove useful context from quiet wake
phrases.

### Gate Silero before speech starts

Silero VAD currently runs for every frame in LISTEN, including up to five seconds of silent follow-up waiting. Use a cheap
energy or spectral gate to decide when to start Silero. Keep Silero active after speech starts so that quiet syllables and
end-of-utterance detection remain robust.

### Add a battery-aware mode

Possible policies while running on battery, especially below a configurable charge threshold:

- disable automatic follow-up listening;
- reduce ring brightness or animations;
- use a stricter wake-word gate;
- omit waiting sounds;
- at a critical level, switch to push-to-talk and close the microphone between button presses.

Push-to-talk is the largest software-controlled saving because it can stop capture and wake-word inference completely, but
it changes the product behavior and should remain an explicit mode.

## Priority 2: remove continuous background work

### Replace `pigpiod`

The current daemon samples GPIO continuously, and `knob.py` additionally polls the button every 20 ms. Candidate designs:

- the kernel `rotary-encoder` driver for rotation and `gpio-key` for the button;
- `libgpiod` edge events for the encoder, button and UPS power-good signal;
- a small event-driven native knob service if the kernel overlays do not handle the required behavior.

Remove the unused `w1-gpio` overlay first or account for its claim on GPIO4. Long press, double click, quadrature decoding and
the charger debounce can all be implemented from timestamped edge events without periodic polling.

### Make the LED loop event-driven

The ring render thread wakes at 25 FPS even when the ring is off, although identical frames are not written to SPI. It could
sleep indefinitely for static states and wake on a mode change, overlay, badge change or animation deadline. This is a small
optimization compared with wake-word inference and GPIO sampling.

### Review headless OS configuration

Measure changes individually and keep a rollback path. Candidates in the current boot configuration include:

- remove the unused `w1-gpio` overlay;
- disable the built-in analog audio driver if only the WM8960 is used;
- disable the unused UART;
- avoid loading the display/KMS stack on a permanently headless device;
- disable Bluetooth if it is not used;
- test Wi-Fi power saving, with special attention to the existing `brcmfmac`/SDIO errors.

Also inspect enabled system services before disabling anything. CPU frequency limits, governors or offlining cores should be
accepted only after a watt-level measurement: doing the same work more slowly does not necessarily save energy.

## Priority 3: audio and model architecture

### Replace Silero VAD

Silero loads ONNX Runtime and remains resident even though it is used only during LISTEN. Compare it with:

- WebRTC VAD;
- a native energy/spectral VAD with an adaptive noise floor;
- a small purpose-trained quantized VAD.

The main expected benefit is lower RSS and less risk of swap pressure. Idle power may change little because Silero inference
does not run in IDLE. The replacement must tolerate the microphone's high and variable noise floor.

### Build a native always-on audio service

A full C++ rewrite of the assistant is unlikely to be worthwhile: STT, Hermes and TTS are primarily network-bound, while
the Python state machine spends most of its time blocked on audio or network I/O. A narrower native boundary is more useful:

```text
ALSA capture
  -> energy/speech gate
  -> wake-word features and inference
  -> wake event and PCM ring buffer
  -> Python assistant
```

Such a service could remove the `arecord` subprocess, avoid PCM copies, combine gating and inference, use the TFLite C API
and keep the high-level dialog logic in Python.

Keeping the existing openWakeWord models limits the CPU improvement: about 25 of the measured 27 ms per active frame is
spent in the mel and embedding models, not the custom Python MLP. A native port with the same models is mainly a memory,
integration and predictability improvement.

### Train a smaller end-to-end wake-word model

The largest long-term software opportunity is to replace the generic openWakeWord embedding frontend with a small int8
keyword model operating directly on log-mel features, for example a DS-CNN or TinyConv model. This could remove the most
expensive 21 ms embedding step.

This is also the highest-risk option. It requires retraining, quantization, representative calibration data and a full
evaluation of recall and false positives. It should be attempted only after the cheaper alternatives have been measured.

## Hardware power opportunities

### Shut down the external amplifier when silent

The TPA3118 family has a low-current shutdown input, while its normal no-load quiescent current can be significant. Determine
the exact amplifier board, supply voltage and whether its `SD` pin is accessible. If it is:

- enter shutdown in IDLE, LISTEN and THINK;
- enable it shortly before playback;
- shut it down after the audio tail;
- verify startup latency and eliminate audible pops.

This may save more power than Python micro-optimizations, but the actual result depends on the amplifier board and wiring.

### Power down unused WM8960 paths

The microphone ADC must remain active for wake-word detection, but the codec supports independent power control for ADCs,
DACs, output buffers and mixers. Verify what ALSA DAPM already powers down when no playback stream is open. Potentially keep
only the working left microphone path active during IDLE and enable the DAC/output path for playback.

### Consider an external always-on controller only if battery life demands it

An MCU could handle the wake word and keep the Pi powered down, but this adds hardware and a long cold-start delay. It is a
different product architecture, not a normal optimization. Consider it only if measured Pi idle power cannot meet the
required battery life.

## Smaller implementation cleanups

These are useful for reliability and storage wear, but they are unlikely to change idle watts materially:

- pipe generated WAV bytes to `aplay` instead of writing every TTS chunk to a temporary file;
- reuse a long-lived ALSA playback handle instead of starting one process per chunk;
- store recorded PCM in a `bytearray` or bounded ring buffer instead of keeping many NumPy arrays and concatenating them;
- bound concurrent piecewise STT work and account for the extra network requests introduced by segmented phrases;
- investigate why assistant RSS rises from about 164 MB after restart to about 204 MB after use.

## Suggested experiment order

1. Establish a repeatable power and accuracy baseline.
2. Measure amplifier shutdown and direct/no-resample ALSA capture.
3. Tune `QuietGate` and add a cheap pre-gate for Silero.
4. Replace `pigpiod` with edge-driven GPIO handling.
5. Replace Silero if memory remains a problem.
6. Prototype a native audio/wake service only if the earlier measurements justify it.
7. Train a new quantized wake-word model only if the current frontend remains the dominant cost.

Each accepted change should include before/after power, CPU, memory and wake-word accuracy results in this document.
