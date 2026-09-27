# RaspiDR

Voice assistant on a Raspberry Pi Zero 2 W: wake word ("hey peedor"; custom openWakeWord model) →
greeting → utterance recording → Groq STT → Hermes LLM agent → Groq TTS, with each stage animated on a NeoPixel ring.

![RaspiDR architecture](docs/images/architecture.jpg)

**Documentation:** [flopsstuff.github.io/raspidr](https://flopsstuff.github.io/raspidr/) (sources in [docs/](docs/index.md))

| | |
|---|---|
| [docs/index.md](docs/index.md) | documentation contents — start here |
| [docs/architecture.md](docs/architecture.md) | how the system works: loop, modules, audio, configuration, deployment, measurements |
| [docs/hardware.md](docs/hardware.md) | the speaker hardware: pins, WM8960, UPS-Lite, ring, encoder, known issues |
| [docs/wakeword_training.md](docs/wakeword_training.md) | how the wake word model was trained and how to retrain it |

```
src/            speaker code (deployed to the Pi): assistant.py is the entry point
src/tools/      hardware checks and utilities (hwtest, micmeter, micprobe, record_samples, make_sounds)
training/       wake word training scripts (datasets in training/data are downloaded/generated, not in git)
models/         hey_peedor.npz — wake word model (Git LFS)
sounds/         greetings, «секунду…» ("one sec…"), waiting sound (Git LFS)
docs/           documentation, published with VitePress
deploy.sh       deploy to the Pi; requirements.txt — Pi dependencies; .env.example — configuration template
```

Voice recordings and phrase samples (`hey-peedor/`, `recordings/`) are personal and gitignored —
[docs/wakeword_training.md](docs/wakeword_training.md) explains how to generate and record your own.

## Quick start

```bash
git lfs install && git clone git@github.com:Flopsstuff/raspidr.git && cd raspidr
cp .env.example .env                                       # fill in the Pi host, Hermes URL and API keys
./deploy.sh --install --restart                            # sync to the Pi, install deps, start the assistant
./deploy.sh --logs                                         # follow the log; say «хэй пидор»

python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python src/assistant.py --text "Привет, кто ты?"  # on the Mac, no microphone
```

## License

Code and documentation: [MIT](LICENSE).

The wake word model `models/hey_peedor.npz` is trained on openWakeWord's negative feature set, and openWakeWord
distributes its pre-trained models under CC BY-NC-SA 4.0 because of its training data. Treat this model the same
way: **non-commercial use only**. Retrain it yourself (see [docs/wakeword_training.md](docs/wakeword_training.md))
if you need different terms.
