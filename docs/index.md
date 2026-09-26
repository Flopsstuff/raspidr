---
layout: home
hero:
  name: RaspiDR
  text: Voice assistant on a Raspberry Pi Zero 2 W
  tagline: Custom wake word, Groq speech, Hermes agent, NeoPixel stage animations
  actions:
    - theme: brand
      text: Architecture
      link: /architecture
    - theme: alt
      text: Hardware
      link: /hardware
    - theme: alt
      text: GitHub
      link: https://github.com/Flopsstuff/raspidr
features:
  - title: Wake word «хэй пидор»
    details: Custom openWakeWord model trained on a Mac from synthetic and live samples; ~0.65 false triggers per hour.
    link: /wakeword_training
  - title: Full voice loop
    details: Greeting, listening until a pause, Groq STT, streamed Hermes answer spoken chunk by chunk, follow-up without the wake word.
    link: /architecture
  - title: Pi Zero 2 W hardware
    details: WM8960 audio HAT, UPS-Lite battery, NeoPixel ring and rotary encoder — pins, quirks and fixes.
    link: /hardware
---

# RaspiDR documentation

Voice assistant on a Raspberry Pi Zero 2 W: wake word «хэй пидор» ("hey peedor") → greeting → listen →
Groq STT → Hermes agent → Groq TTS, with every stage shown on the NeoPixel ring.

## [Architecture](architecture.md)

How the system works end to end.

- [Components](architecture.md#components) — Pi, Hermes server, Groq / xAI, Mac for development
- [Voice loop](architecture.md#voice-loop) — states, transitions, ring animations and sounds per stage
  - [Response pipeline](architecture.md#response-pipeline) — STT → Hermes stream → chunking → TTS → player
- [Modules (`src/`)](architecture.md#modules-src)
- [Audio on the Pi](architecture.md#audio-on-the-pi) — dmix/dsnoop, PulseAudio, saved mixer state
- [Configuration](architecture.md#configuration) — `.env` keys and `assistant.py` flags
- [Development and deployment](architecture.md#development-and-deployment)
- [Measurements (2026-09-26)](architecture.md#measurements-2026-09-26) — CPU, RAM, latencies
- [Known issues and next steps](architecture.md#known-issues-and-next-steps)

## [Hardware](hardware.md)

The speaker device: board, pins, peripherals and their quirks.

- [Access](hardware.md#access) · [Board and OS](hardware.md#board-and-os) · [Components](hardware.md#components) · [Pinout](hardware.md#pinout-40-pin-header)
- [WM8960 Audio HAT](hardware.md#wm8960-audio-hat) — codec, mixer, microphones (only one works)
- [UPS-Lite battery (CW2015)](hardware.md#ups-lite-battery-cw2015) — battery gauge, power-good GPIO, pogo-pin contact issue
- [NeoPixel ring](hardware.md#neopixel-ring-rgb-led) · [Rotary encoder](hardware.md#rotary-encoder-with-button) · [pigpiod](hardware.md#pigpiod)
- [Software from the old project](hardware.md#software-from-the-old-project)
- [Gotchas and observations](hardware.md#gotchas-and-observations)

## [Wake word training](wakeword_training.md)

How the custom «хэй пидор» model was built and how to retrain it.

- [Environment](wakeword_training.md#environment-mac-intel) · [Data](wakeword_training.md#data) · [Steps](wakeword_training.md#steps)
- [Run history](wakeword_training.md#run-history) — metrics of all training runs
- [Weaknesses of the current model](wakeword_training.md#weaknesses-of-the-current-model) — and what to do next
- [Gotchas](wakeword_training.md#gotchas)
