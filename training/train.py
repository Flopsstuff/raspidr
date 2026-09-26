#!/usr/bin/env python3
"""
Trains a wake word classifier on top of openWakeWord features (16 × 96).

    ../.venv-train/bin/python train.py --steps 12000 --name hey_peedor

Data: data/features/*.npy (build_dataset.py), data/acav_*.npy (negatives, download_negatives.py),
data/validation_set_features.npy (~10.7 h of continuous audio — false activations per hour).
Output: models/<name>.npz (weights for numpy inference on the Pi), models/<name>.onnx, report to console.
"""
import argparse
import glob
import os
import time

import numpy as np
import torch
from torch import nn

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
FEAT = os.path.join(DATA, "features")
MODELS = os.path.join(os.path.dirname(HERE), "models")
FRAME_S = 0.08  # feature frame step


def load(name):
    return torch.from_numpy(np.load(os.path.join(FEAT, f"{name}.npy")).astype(np.float32))


def load_acav(path, rows, rng):
    """Negatives kept in memory as float16 (all 1.3M windows ≈ 4 GB); rows=0 — take all."""
    a = np.load(path, mmap_mode="r")
    if rows and rows < len(a):
        block = 5000
        starts = np.sort(rng.choice(len(a) - block, size=rows // block, replace=False))
        return torch.from_numpy(np.concatenate([a[s:s + block] for s in starts]))
    return torch.from_numpy(np.ascontiguousarray(a))


def make_model(hidden):
    return nn.Sequential(
        nn.Flatten(),
        nn.Linear(16 * 96, hidden), nn.LayerNorm(hidden), nn.ReLU(),
        nn.Linear(hidden, hidden), nn.LayerNorm(hidden), nn.ReLU(),
        nn.Linear(hidden, 1),
    )


@torch.no_grad()
def scores(model, x, batch=8192):
    model.eval()
    out = torch.cat([torch.sigmoid(model(x[i:i + batch])).squeeze(1) for i in range(0, len(x), batch)])
    model.train()
    return out.numpy()


def fp_per_hour(val_scores, thr, n_frames):
    """Activations on validation: consecutive frames above threshold = one event."""
    above = val_scores >= thr
    events = int(np.sum(above[1:] & ~above[:-1]) + above[0])
    return events / (n_frames * FRAME_S / 3600)


def evaluate(model, sets, val_windows, n_val, real_mask):
    r = {k: scores(model, v) for k, v in sets.items()}
    v = scores(model, val_windows)
    res = {}
    for thr in (0.3, 0.5, 0.7, 0.9):
        res[thr] = dict(
            real=float((r["test_pos"][real_mask] >= thr).mean()),
            pos=float((r["test_pos"] >= thr).mean()),
            noisy=float((r["test_pos_noisy"] >= thr).mean()),
            neg=float((r["test_neg"] >= thr).mean()),
            fph=fp_per_hour(v, thr, n_val),
        )
    return res


def export(model, name, meta):
    os.makedirs(MODELS, exist_ok=True)
    sd = {k: v.numpy() for k, v in model.state_dict().items()}
    # layer order in Sequential: 1 Linear, 2 LayerNorm, 4 Linear, 5 LayerNorm, 7 Linear
    np.savez(os.path.join(MODELS, f"{name}.npz"),
             w1=sd["1.weight"], b1=sd["1.bias"], g1=sd["2.weight"], be1=sd["2.bias"],
             w2=sd["4.weight"], b2=sd["4.bias"], g2=sd["5.weight"], be2=sd["5.bias"],
             w3=sd["7.weight"], b3=sd["7.bias"], **{f"meta_{k}": v for k, v in meta.items()})
    wrapped = nn.Sequential(model, nn.Sigmoid()).eval()
    torch.onnx.export(wrapped, torch.zeros(1, 16, 96), os.path.join(MODELS, f"{name}.onnx"),
                      input_names=["input"], output_names=[name], opset_version=13)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--name", default="hey_peedor")
    p.add_argument("--steps", type=int, default=12000)
    p.add_argument("--hidden", type=int, default=64)
    p.add_argument("--acav-rows", type=int, default=0, help="0 — all negative windows")
    p.add_argument("--neg-batch", type=int, default=1024)
    p.add_argument("--max-neg-weight", type=float, default=200.0,
                   help="negative weight ramps from 1 to this value — suppresses false activations")
    p.add_argument("--seed", type=int, default=0)
    args = p.parse_args()

    torch.manual_seed(args.seed)
    rng = np.random.default_rng(args.seed)
    t0 = time.time()

    pos, hard = load("train_pos"), load("train_neg")
    sets = {k: load(k) for k in ("test_pos", "test_pos_noisy", "test_neg")}
    acav = load_acav(sorted(glob.glob(os.path.join(DATA, "acav_*.npy")))[0], args.acav_rows, rng)
    # which test positives are live recordings from the speaker (same order as in build_dataset.py)
    with open(os.path.join(FEAT, "test_files.txt")) as fh:
        lines = fh.read().split("# neg")[0].splitlines()[1:]
    real_mask = np.array(["pi-rec" in ln for ln in lines if ln.strip()])
    val = np.load(os.path.join(DATA, "validation_set_features.npy"))
    val_windows = torch.from_numpy(np.lib.stride_tricks.sliding_window_view(val, (16, 96))[:, 0].copy())
    n_val = len(val)
    print(f"pos {tuple(pos.shape)}, hard neg {tuple(hard.shape)}, acav {tuple(acav.shape)}, "
          f"валидация {n_val * FRAME_S / 3600:.1f} ч ({time.time() - t0:.0f} с)", flush=True)

    model = make_model(args.hidden)
    opt = torch.optim.Adam(model.parameters(), lr=1e-3)
    sched = torch.optim.lr_scheduler.OneCycleLR(opt, max_lr=1e-3, total_steps=args.steps, pct_start=0.1)
    bce = nn.BCEWithLogitsLoss(reduction="none")

    best, best_state, best_step = -1e9, None, 0
    for step in range(1, args.steps + 1):
        ip = torch.randint(len(pos), (128,))
        ih = torch.randint(len(hard), (128,))
        ia = torch.randint(len(acav), (args.neg_batch,))
        x = torch.cat([pos[ip], hard[ih], acav[ia].float()])
        y = torch.cat([torch.ones(128), torch.zeros(128 + args.neg_batch)])
        w_neg = 1 + (args.max_neg_weight - 1) * step / args.steps
        w = torch.cat([torch.ones(128), torch.full((128 + args.neg_batch,), w_neg)])
        # small input noise — robustness to minor feature differences
        x = x + 0.05 * torch.randn_like(x)
        loss = (bce(model(x).squeeze(1), y) * w).mean()
        opt.zero_grad()
        loss.backward()
        opt.step()
        sched.step()

        if step % 1000 == 0 or step == args.steps:
            r = evaluate(model, sets, val_windows, n_val, real_mask)
            m = r[0.5]
            # goal: catch the phrase (live recordings matter most), barely fire on anything else
            score = 2 * m["real"] + m["pos"] + m["noisy"] - 2 * m["neg"] - min(m["fph"], 20)
            mark = ""
            if score > best:
                best, best_step = score, step
                best_state = {k: v.clone() for k, v in model.state_dict().items()}
                mark = "  ← лучшая"
            print(f"шаг {step:5d} loss {loss.item():.4f} | порог 0.5: живые {m['real']:.0%}, поймано {m['pos']:.0%}, "
                  f"в шуме {m['noisy']:.0%}, "
                  f"похожие {m['neg']:.0%}, ложных/ч {m['fph']:.2f}{mark} ({time.time() - t0:.0f} с)", flush=True)

    model.load_state_dict(best_state)
    r = evaluate(model, sets, val_windows, n_val, real_mask)
    print(f"\nлучшая модель — шаг {best_step}")
    print(f"порог   живые({real_mask.sum()})  поймано   в шуме   похожие фразы   ложных в час")
    for thr, m in r.items():
        print(f"{thr:4.1f}    {m['real']:8.0%}   {m['pos']:6.0%}   {m['noisy']:6.0%}   {m['neg']:10.0%}      {m['fph']:8.2f}")
    export(model, args.name, {"step": best_step, "hidden": args.hidden})
    print(f"сохранено: models/{args.name}.npz, models/{args.name}.onnx")


if __name__ == "__main__":
    main()
