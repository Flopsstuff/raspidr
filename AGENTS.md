# AGENTS.md

Guidance for AI coding agents working in this repository.

## What this is

RaspiDR — a voice assistant running on a Raspberry Pi Zero 2 W (416 MB RAM, arm64, Debian 12). Development
happens on a Mac; the code is pushed to the Pi with `./deploy.sh`. Read [docs/index.md](docs/index.md) first:
architecture, hardware quirks and wake word training are documented there.

## Layout

- `src/` — runs on the Pi. Entry point `src/assistant.py` (state machine: wake word → greeting → listen →
  Groq STT → Hermes stream → Groq TTS). `src/wakeword.py` is also a standalone detector.
- `src/tools/` — hardware checks (`hwtest.py`), microphone probes, sample recorder, procedural sounds.
- `training/` — wake word training (Mac only, Python 3.11 venv `.venv-train` with torch 2.2.2 and `numpy<2`).
- `models/`, `sounds/` — binary assets tracked with Git LFS.
- `docs/` — VitePress site (`npm run docs:dev` / `docs:build`), published to GitHub Pages by `.github/workflows/docs.yml`.

## Commands

```bash
.venv/bin/python src/assistant.py --text "Привет"   # Mac: Hermes + TTS without a microphone
./deploy.sh [--install] [--restart] [--logs]        # sync to the Pi / deps / restart / follow log
npm run docs:dev                                     # docs preview
```

There is no test suite; `src/tools/hwtest.py` is a manual hardware check on the Pi.

## Conventions

- Configuration and all hosts/addresses live in `.env` (template: `.env.example`); never hardcode IPs, hostnames or keys.
- Comments, docstrings and docs are in English. Runtime log messages and Russian data (system prompt, phrase lists,
  the wake phrase «хэй пидор») stay Russian.
- Commit messages start with a gitmoji (`✨`, `🐛`, `♻️`, `📝`, …) followed by an English sentence.
- Voice recordings (`hey-peedor/`, `recordings/`) and datasets (`training/data/`) are never committed.

## Pi gotchas

- Memory is tight: keep the assistant's RSS around 200 MB; no heavy processes.
- PulseAudio grabs the sound card → `assistant.py` stops it on start; ALSA dmix/dsnoop share the device.
- `numpy<2` on the Pi (tflite-runtime 2.14), `OPENBLAS_NUM_THREADS=1` (set in code) or CPU goes to ~300%.
- Restarting by hand: `pkill -f` patterns must not match the ssh command line itself — use `[a]ssistant`.
