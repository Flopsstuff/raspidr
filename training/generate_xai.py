#!/usr/bin/env python3
"""
Synthetic data via xAI TTS (https://docs.x.ai/developers/model-capabilities/audio/text-to-speech).

Key comes from the XAI_API_KEY env var or the project .env (gitignored).
  data/xai/pos/ — «хэй пидор»: 28 voices × speed × delivery
  data/xai/neg/ — same voices: similar phrases and ordinary speech (so "xAI voice" doesn't become a phrase cue)
A rerun downloads only what is missing.
"""
import itertools
import json
import os
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "data", "xai")
API = "https://api.x.ai/v1"

POS = [  # (tag, text)
    ("plain", "хэй пидор"),
    ("excl", "Хэй, пидор!"),
    ("quest", "Хей, пидор?"),
    ("loud", "<loud>Хэй, пидор!</loud>"),
    ("fast", "<fast>хэй пидор</fast>"),
]
POS_SPEEDS = (0.8, 0.95, 1.1, 1.3)
NEG = [
    "хэй", "пидор", "пидорас", "хэй пират", "хэй повар", "хэй пилот", "хэй подарок", "хэй привет",
    "эй подожди", "хэй, дорогой", "пидор хэй", "окей гугл",
    "включи музыку погромче", "какая завтра погода", "поставь будильник на семь утра",
    "слушай, а ты сегодня что делаешь вечером", "я не знаю, давай потом решим",
    "выключи свет в комнате", "пойдём пить чай", "это очень интересная идея",
]
NEG_SPEEDS = (0.9, 1.15)


def key():
    k = os.environ.get("XAI_API_KEY")
    if not k:
        with open(os.path.join(os.path.dirname(HERE), ".env")) as f:
            for line in f:
                if line.startswith("XAI_API_KEY="):
                    k = line.strip().split("=", 1)[1]
    return k


def tts(k, text, voice, speed, path):
    body = json.dumps({"text": text, "voice_id": voice, "language": "ru", "speed": speed}).encode()
    for attempt in range(5):
        req = urllib.request.Request(API + "/tts", data=body, method="POST", headers={
            "Authorization": f"Bearer {k}", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                data = r.read()
            with open(path + ".part", "wb") as f:
                f.write(data)
            os.replace(path + ".part", path)
            return True
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503) and attempt < 4:
                time.sleep(2 ** attempt)
                continue
            print(f"  ошибка {e.code} {voice} «{text}»: {e.read()[:200]!r}")
            return False
        except Exception as e:  # network
            if attempt < 4:
                time.sleep(2 ** attempt)
                continue
            print(f"  ошибка {voice} «{text}»: {e}")
            return False


def main():
    k = key()
    req = urllib.request.Request(API + "/tts/voices", headers={"Authorization": f"Bearer {k}"})
    voices = [v["voice_id"] for v in json.load(urllib.request.urlopen(req))["voices"]]
    jobs = []
    for d in ("pos", "neg"):
        os.makedirs(os.path.join(OUT, d), exist_ok=True)
    for v, (tag, text), sp in itertools.product(voices, POS, POS_SPEEDS):
        jobs.append((text, v, sp, os.path.join(OUT, "pos", f"xai_{v}_{tag}_s{sp}.mp3")))
    for v, (i, text), sp in itertools.product(voices, enumerate(NEG), NEG_SPEEDS):
        jobs.append((text, v, sp, os.path.join(OUT, "neg", f"xai_{v}_n{i:02d}_s{sp}.mp3")))
    todo = [j for j in jobs if not os.path.exists(j[3])]
    chars = sum(len(j[0]) for j in todo)
    print(f"{len(voices)} голосов; заданий {len(jobs)}, осталось {len(todo)} (~{chars} символов, "
          f"~${chars * 15 / 1e6:.2f})", flush=True)

    done = fail = 0
    t0 = time.time()
    with ThreadPoolExecutor(6) as ex:
        for ok in ex.map(lambda j: tts(k, *j), todo):
            done += ok
            fail += not ok
            if (done + fail) % 50 == 0:
                print(f"  {done + fail}/{len(todo)} ({time.time() - t0:.0f} с)", flush=True)
    print(f"готово: {done}, ошибок {fail}; pos={len(os.listdir(os.path.join(OUT, 'pos')))}, "
          f"neg={len(os.listdir(os.path.join(OUT, 'neg')))}")


if __name__ == "__main__":
    main()
