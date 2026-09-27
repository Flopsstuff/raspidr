#!/usr/bin/env python3
"""
Streaming evaluation of the model on whole files — the same way wakeword.py sees it on the speaker:
audio in 80 ms frames → openWakeWord features → model, taking the max score per file.

    ../.venv-train/bin/python eval_stream.py [--model ../models/hey_peedor.npz] [files/dirs ...]
Without files — test files from data/features/test_files.txt (positives and negatives).
"""
import argparse
import glob
import os
import sys

import numpy as np
from openwakeword.utils import AudioFeatures

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "src"))
from wakeword import FRAME, NpzModel  # noqa: E402
from build_dataset import load  # noqa: E402


def stream_max(model, feats, audio):
    """Max score over the file; 1 s of silence is prepended to fill the feature buffer."""
    pcm = np.concatenate([np.zeros(16000, np.float32), audio, np.zeros(8000, np.float32)])
    pcm = (np.clip(pcm, -1, 1) * 32767).astype(np.int16)
    feats.reset() if hasattr(feats, "reset") else None
    best = 0.0
    for i in range(0, len(pcm) - FRAME + 1, FRAME):
        feats(pcm[i:i + FRAME])
        if i >= 16000:
            best = max(best, model(feats.get_features(16)))
    return best


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--model", default=os.path.join(os.path.dirname(HERE), "models", "hey_peedor.npz"))
    p.add_argument("--threshold", type=float, default=0.5)
    p.add_argument("paths", nargs="*")
    args = p.parse_args()

    groups = {}
    if args.paths:
        files = []
        for x in args.paths:
            files += sorted(glob.glob(os.path.join(x, "*.*"))) if os.path.isdir(x) else [x]
        groups["files"] = files
    else:
        text = open(os.path.join(HERE, "data", "features", "test_files.txt")).read()
        pos, neg = text.split("# neg")
        pos = [ln for ln in pos.splitlines()[1:] if ln.strip()]
        groups["live (test)"] = [f for f in pos if "pi-rec" in f]
        groups["your TTS (test)"] = [f for f in pos if "/hey-peedor/tts-" in f]
        groups["Piper/say (test)"] = [f for f in pos if "/data/tts/pos/" in f]
        groups["xAI unseen voices (test)"] = [f for f in pos if "/data/xai/pos/" in f]
        neg = [ln for ln in neg.splitlines() if ln.strip()]
        groups["similar phrases Piper/say (test, must NOT)"] = [f for f in neg if "/data/tts/" in f]
        groups["speech by unseen xAI voices (test, must NOT)"] = [f for f in neg if "/data/xai/" in f]

    model = NpzModel(args.model)
    for name, files in groups.items():
        res = []
        for f in files:
            feats = AudioFeatures(inference_framework="onnx")
            res.append((stream_max(model, feats, load(f)), f))
        hit = sum(s >= args.threshold for s, _ in res)
        print(f"\n== {name}: {hit}/{len(res)} above the threshold {args.threshold}")
        if len(res) <= 20:
            for s, f in res:
                print(f"   {s:5.3f}  {os.path.basename(f)}")
        else:
            ss = np.array([s for s, _ in res])
            print(f"   min {ss.min():.3f}  median {np.median(ss):.3f}  max {ss.max():.3f}")


if __name__ == "__main__":
    main()
