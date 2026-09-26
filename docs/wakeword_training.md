# Training the wake word «хэй пидор» ("hey peedor")

A custom model on top of [openWakeWord](https://github.com/dscripka/openWakeWord): the pretrained feature models
(melspectrogram + embedding) turn audio into 16 × 96 windows, and a small network trained locally on a Mac
decides "phrase / not phrase". Picovoice was ruled out: custom models require approval of a commercial application.
openWakeWord's synthetic data generator doesn't support Russian, so we make the synthetic data ourselves (Piper, macOS, xAI).

All training lives in `training/`, large data in `training/data/` (~4.3 GB, not deployed to the Pi).
The final model is `models/hey_peedor.npz` (weights for numpy) and `models/hey_peedor.onnx`.

## Environment (Mac, Intel)

```bash
~/.pyenv/versions/3.11.13/bin/python -m venv .venv-train
.venv-train/bin/pip install "torch==2.2.2" "numpy<2" openwakeword scipy tqdm piper-tts onnx
.venv-train/bin/pip install "numpy<2"   # installing onnx pulls in numpy 2 — revert it
.venv-train/bin/python -c "import openwakeword.utils as u; u.download_models()"
```

Why: torch 2.2.2 is the last version for Intel Macs; it only supports up to Python 3.12 and is built against numpy 1.x.

## Data

| Source | Where | Count | How obtained |
|---|---|---|---|
| User's TTS | `hey-peedor/*.mp3` | 52 | xAI TTS by hand, 24 kHz |
| **Live recordings from the speaker** | `hey-peedor/pi-rec/*.wav` | 15 (trimmed by hand) | `src/tools/record_samples.py` on the Pi |
| Piper, 4 Russian voices | `training/data/tts/pos` | 720 | `generate_tts.py`: 4 voices × 4 punctuation variants × tempo × variability |
| macOS `say` Milena | `training/data/tts/pos` | 20 | same script, 5 speeds |
| xAI TTS, 28 voices | `training/data/xai/pos` | 560 | `generate_xai.py`: × 4 speeds × 5 delivery styles (loud, fast, question…) |
| Piper/say hard negatives | `training/data/tts/neg` | 420 | similar-sounding phrases («хэй пират», «пидорас», «хэй»…) and commands |
| xAI hard negatives | `training/data/xai/neg` | 1120 | same voices: 20 phrases × 2 speeds, so that "xAI voice" doesn't become a feature |
| General negatives | `training/data/acav_1302083.npy` | 1.3M windows ≈ 300 h | first 4 GB of `openwakeword_features_ACAV100M_2000_hrs_16bit.npy` (via a Range request) |
| Validation | `training/data/validation_set_features.npy` | 10.7 h | for "false activations per hour" |
| Room background | `training/data/noise/` | 2 × 30 s | silence from the speaker's microphone |

## Steps

```bash
cd training
../.venv-train/bin/python download_negatives.py --gb 4       # negatives + validation (~4.2 GB)
../.venv-train/bin/python generate_tts.py                    # Piper + Milena (a few minutes)
../.venv-train/bin/python generate_xai.py                    # xAI, ~1680 requests, ~$0.5; key: XAI_API_KEY from .env

# live recordings: on the Pi, ring red = wait, green = speak (PI_HOST/PI_DIR: set -a; . ../.env; set +a)
ssh "$PI_HOST" "cd $PI_DIR && .venv/bin/python src/tools/record_samples.py --label hey-peedor -n 15"
rsync -a "$PI_HOST:$PI_DIR/recordings/hey-peedor/" ../hey-peedor/pi-rec/

../.venv-train/bin/python build_dataset.py                   # augmentations + features (~10 min)
../.venv-train/bin/python train.py --steps 30000             # ~13 min on CPU
../.venv-train/bin/python eval_stream.py                     # streaming check of the test files
cd .. && ./deploy.sh                                         # models/hey_peedor.npz → Pi
```

Run logs: `training/logs/`.

### build_dataset.py

- The phrase is cut out by energy (20 ms frames) and placed in a 2 s window (= exactly 16 feature frames) so that it
  **ends** 0–0.3 s before the end of the window; the model learns to trigger right after the phrase.
- Augmentations: tempo ±12 %, room reverb (synthetic RIR, RT60 0.15–0.7 s) in half the cases, peak
  level −28…−3 dBFS, background (room noise from the speaker / white / pink / brown noise) at SNR 3–30 dB.
- Variants per clip: live recordings ×150 (the main domain), user's TTS ×40, Piper/say/xAI ×6.
- Hard negatives: TTS negatives ×6 (xAI ×3), phrase "fragments" («хэй пи…», «…пидор») ×3 per positive,
  800 windows of pure background.
- Test set (never used for training): live recordings #11–15, 15 % of the user's TTS, 10 % of Piper/say,
  **the xAI voices iris, leo, liora entirely**, as a check on unfamiliar voices.
- Features: `openwakeword.utils.AudioFeatures(onnx).embed_clips` → `data/features/*.npy` (float16).

### train.py

- Network: `Flatten(16×96) → Linear 64 → LayerNorm → ReLU → Linear 64 → LayerNorm → ReLU → Linear 1`.
- Batch: 128 positives + 128 hard negatives + 1024 ACAV windows; the negative weight grows 1 → 200 over training
  (suppresses false activations); input noise 0.05; Adam, OneCycle, max lr 1e-3.
- Every 1000 steps: live recordings, the full test set, test in noise, similar phrases, false activations per hour on validation;
  the best checkpoint is saved by `2·live + test + noise − 2·similar − min(false/h, 20)`.
- Export: `models/<name>.npz` (weights, inference in `src/wakeword.py::NpzModel`) and `.onnx`.

### eval_stream.py

Same as on the speaker: the file in 80 ms frames → features → model, taking the maximum over the file. More honest than
the "single-position" evaluation in `train.py` (there the phrase sits strictly at the end of the window, and the numbers are understated).
`eval_stream.py <files or folders>` checks any recordings.

## Run history

| Run | Data | Best step | False/h @0.5 | Streaming: live / TTS / Piper / unfamiliar xAI | Streaming: similar phrases |
|---|---|---|---|---|---|
| 1 | user TTS + 15 live + Piper/say; 400K ACAV windows, negative weight up to 15 | 2000 | 53 | — | — |
| 2 | same; all 1.3M windows, weight up to 200, batch 1024 | 10000 | 0.65 | 5/5 · 6/6 · 70/74 · — | 0/32 |
| **3 (current)** | + xAI (560 pos., 1120 neg.), live recordings trimmed | 4000 | **0.65** (0.28 @0.9) | **5/5 · 6/6 · 73/74 · 60/60** | 2/32 Piper, 7/120 xAI |

Run 3, single-position evaluation: live 100 %, full test 99 %, in noise 97 %, similar 0 %.
Live on the speaker: 17 activations in 2 minutes from different distances, all on his phrase according to the user
(the total number of times he said the phrase wasn't counted, so live recall hasn't been measured).

## Weaknesses of the current model

It triggers on phonetically similar phrases from unfamiliar voices: «хэй **пи**рат» (up to 0.996), «хэй **при**вет» (0.96),
«хэй, **до**рогой» (0.91), and borderline on «эй сири», «эй подожди». Normal speech and commands don't trigger it.

What to do in the next version:
1. Minimal pairs as negatives via xAI with all voices: «хэй пират / пирог / пилот / привет / педро / подвинься /
   дорогой / пидорас…», several speeds (~$0.1).
2. More live recordings from the speaker, including "non-phrase" audio: normal conversation in the room as negatives.
3. Tune the threshold on a live test (0.7–0.9 cuts some of the false activations and catches the phrase just as well).

## Gotchas

- Pi: `tflite-runtime 2.14` is built against numpy 1.x → `numpy<2` for aarch64 in `requirements.txt`.
- `pip install onnx` silently installs numpy 2 → torch 2.2 crashes ("Numpy is not available").
- Synthetic file names: punctuation was lost in the name → variants overwrote each other; the name now
  includes the text number.
- WAVs from Groq/xAI have an "infinite" length in the header; compute the duration from the file size.
- `numpy` on the Pi: OpenBLAS spawns threads for tiny matrices (~300 % CPU) → `OPENBLAS_NUM_THREADS=1`.
- Single-position evaluation understates quality; look at `eval_stream.py`.
