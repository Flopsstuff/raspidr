#!/usr/bin/env python3
"""
Smart speaker voice assistant: «хэй пидор» → greeting → phrase until a pause → Groq STT → Hermes (streaming)
→ Groq TTS in chunks → player queue. Each stage has its own ring animation (see leds.py).

    .venv/bin/python src/assistant.py                              # full loop (Pi: LED ring, Mac: ring in the terminal)
    .venv/bin/python src/assistant.py --text "Привет, кто ты?"     # no microphone: Hermes + TTS, then exit
    .venv/bin/python src/assistant.py --no-wake                    # no wake word: listen for a phrase right away

After an answer we immediately listen for the next phrase (no wake word); no phrase within --listen-timeout — go to sleep.
Interrupt an answer: encoder button (Pi) / Enter (Mac).
"""
import os

# For the model's tiny matrices OpenBLAS spawns threads that just spin idle (~300% CPU)
for _v in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS"):
    os.environ.setdefault(_v, "1")

import argparse
import collections
import json
import queue
import random
import re
import signal
import sys
import threading
import time
import urllib.request
from types import SimpleNamespace

import numpy as np
from openwakeword.utils import AudioFeatures
from openwakeword.vad import VAD

import hermes
import voice
from audio_io import FRAME, RATE, LoopPlayer, Mic, Player
from controls import InterruptButton
from leds import Ring
from wakeword import ROOT, Greeter, NpzModel, free_sound_card, resolve_model

FRAME_S = FRAME / RATE
HISTORY_PAIRS = 3  # how many question/answer pairs to remember
HISTORY_TTL = 300  # seconds of silence before the history is reset

SYSTEM_PROMPT = (
    "Ты — голосовой ассистент умной колонки, тебя зовут «пидор». Твой ответ озвучивается синтезатором речи. "
    "Отвечай по-русски, коротко: одно-три предложения. Никакого markdown, списков, эмодзи, ссылок и сокращений — "
    "только обычный разговорный текст, числа пиши словами, если их немного. "
    "Своё имя пиши «пидор» и никогда не начинай с него фразу."
)

# On silence/noise Whisper sometimes "hallucinates" subtitle credits
STT_JUNK = re.compile(r"субтитр|продолжение следует|спасибо за просмотр|редактор|подпишитесь", re.I)


def clean_stt(text):
    t = text.strip().strip("«»\"'").strip()
    t = re.sub(r"^[—–-]+\s*", "", t).strip("… ").strip()
    return "" if STT_JUNK.search(t) else t


def clean_tts(text):
    """Strip what the synthesizer would read as garbage: markdown, emoji."""
    t = re.sub(r"[*_#`>|\[\]]", "", text)
    t = "".join(c for c in t if ord(c) < 0x2600 or c in "«»—…")
    t = re.sub(r"\s+", " ", t)
    return re.sub(r"\s+([.,!?…»])", r"\1", t).strip()


class Chunker:
    """Text stream → chunks for TTS: the first sentence right away (faster first sound), then glue sentences
    up to ~soft chars, at most max_len (Orpheus limit, and saves the Groq quota of 10 requests/min)."""

    END = re.compile(r"[.!?…]+[»\")]*(\s+|$)|\n+")

    def __init__(self, soft=100, max_len=180):
        self.buf, self.acc, self.first = "", "", True
        self.soft, self.max_len = soft, max_len

    def _sentences(self, final=False):
        out = []
        while True:
            m = self.END.search(self.buf)
            if not m or (m.end() == len(self.buf) and not final and not self.buf.endswith(("\n", " "))):
                break
            out.append(self.buf[:m.end()])
            self.buf = self.buf[m.end():]
        while len(self.buf) > self.max_len:  # too long without a period — cut at a comma, otherwise at a space
            cut = self.buf.rfind(", ", 0, self.max_len)
            if cut < self.max_len // 2:
                cut = self.buf.rfind(" ", 0, self.max_len)
            cut = cut + 1 if cut > 0 else self.max_len
            out.append(self.buf[:cut])
            self.buf = self.buf[cut:]
        return out

    def _emit(self, sentences):
        chunks = []
        for s in sentences:
            if not s.strip():
                continue
            if self.first:
                chunks.append(s)
                self.first = False
                continue
            if self.acc and len(self.acc) + len(s) > self.max_len:
                chunks.append(self.acc)
                self.acc = ""
            self.acc += s
            if len(self.acc) >= self.soft:
                chunks.append(self.acc)
                self.acc = ""
        return [c for c in (clean_tts(c) for c in chunks) if c]

    def feed(self, delta):
        self.buf += delta
        return self._emit(self._sentences())

    def flush(self):
        chunks = self._emit(self._sentences(final=True) + [self.buf])
        self.buf = ""
        if self.acc.strip():
            chunks.append(clean_tts(self.acc))
            self.acc = ""
        return [c for c in chunks if c]


def tts_groq_once(text):
    """Groq Orpheus in a single attempt: voice.request's built-in wait on 429 would block the voice for up to 30 s."""
    cfg = voice.PROVIDERS["groq"]
    payload = {"model": cfg["tts_model"], "input": text, "voice": cfg["voice"], "response_format": "wav"}
    req = urllib.request.Request(cfg["api"] + "/audio/speech", data=json.dumps(payload).encode(), headers={
        "Authorization": "Bearer " + voice.api_key(cfg["key"]), "Content-Type": "application/json",
        "User-Agent": "raspidr/1.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read()


class Assistant:
    MODES = {"idle": "off", "greet": "greet", "listen": "listen", "think": "think", "speak": "speak",
             "error": "error"}

    def __init__(self, args):
        self.args = args
        self.ring = Ring(args.leds)
        self.player = Player()
        self.loop = LoopPlayer()  # "drops" while thinking
        self.button = InterruptButton()
        self.greetings = Greeter(args.sounds).files
        self.waits = Greeter(args.wait_sounds).files
        self.think_sound = args.think_sound if args.think_sound and os.path.exists(args.think_sound) else None
        self.last_greeting = None
        self.history, self.last_turn = [], 0.0
        self.state, self.state_t = "idle", time.time()
        self.job = None

    # ------------------------------------------------------------ helpers

    def log(self, msg):
        self.ring.log(time.strftime("%H:%M:%S ") + msg)

    def set_state(self, state):
        if state != self.state:
            self.log(f"[{state.upper()}]")
        self.state, self.state_t = state, time.time()
        self.ring.set_mode(self.MODES[state])

    def greet(self):
        f = random.choice([g for g in self.greetings if g != self.last_greeting] or self.greetings)
        self.last_greeting = f
        self.player.enqueue(f)
        self.set_state("greet")

    def interrupt(self):
        if self.job:
            self.job.cancel.set()
        self.loop.stop()
        self.player.stop()
        self.log("[STOP] перебили")
        self.set_state("idle")

    # ------------------------------------------------------------ answer: STT → Hermes → TTS

    def start_answer(self, pcm=None, text=None):
        self.job = SimpleNamespace(cancel=threading.Event(), done=threading.Event(), spoke=threading.Event(),
                                   error=None, long=False)
        self.set_state("think")
        threading.Thread(target=self._answer, args=(self.job, pcm, text), daemon=True).start()

    def _answer(self, job, pcm, text):
        try:
            if text is None:
                t0 = time.time()
                raw = voice.stt(voice.pcm_to_wav(pcm.tobytes()), "command.wav", lang="ru", prompt="хэй пидор")
                text = clean_stt(raw)
                self.log(f"[STT {time.time() - t0:.1f}s] «{text}»" + ("" if raw.strip() == text else f"  (сырой: «{raw}»)"))
                if not text:
                    return
            if job.cancel.is_set():
                return
            if time.time() - self.last_turn > HISTORY_TTL:
                self.history = []
            user = {"role": "user", "content": text}
            messages = [{"role": "system", "content": SYSTEM_PROMPT}] + self.history + [user]

            tts_q = queue.Queue()
            tts = threading.Thread(target=self._tts_worker, args=(job, tts_q), daemon=True)
            tts.start()
            chunker, answer, t1, first = Chunker(), "", time.time(), None
            try:
                for delta in hermes.stream_chat(messages, cancel=job.cancel):
                    if first is None:
                        first = time.time() - t1
                        self.log(f"[LLM первый токен {first:.1f}s]")
                    answer += delta
                    for chunk in chunker.feed(delta):
                        tts_q.put(chunk)
                for chunk in chunker.flush():
                    tts_q.put(chunk)
            finally:
                tts_q.put(None)
            answer = answer.strip()
            self.log(f"[LLM {time.time() - t1:.1f}s] {answer}")
            if answer and not job.cancel.is_set():
                self.history = (self.history + [user, {"role": "assistant", "content": answer}])[-2 * HISTORY_PAIRS:]
                self.last_turn = time.time()
            tts.join()
        except Exception as e:
            job.error = e
        finally:
            job.done.set()

    def _tts_worker(self, job, q):
        n = 0
        while True:
            chunk = q.get()
            if chunk is None or job.cancel.is_set():
                return
            n += 1
            t = time.time()
            try:
                audio, prov = tts_groq_once(chunk), "groq"
            except Exception as e:
                self.log(f"[TTS#{n}] groq: {str(e)[:80]} → xai")
                try:
                    audio, prov = voice.tts(chunk, provider="xai", voice=self.args.xai_voice), "xai"
                except Exception as e2:
                    job.error = e2
                    return
            if job.cancel.is_set():
                return
            self.log(f"[TTS#{n} {prov} {time.time() - t:.1f}s] {chunk}")
            self.player.enqueue(audio)
            job.spoke.set()

    def poll_answer(self):
        """THINK/SPEAK: switch animations and finish. True — the answer is done (or failed)."""
        job = self.job
        waited = time.time() - self.state_t
        if self.state == "think" and job.spoke.is_set():
            self.loop.stop()
            self.set_state("speak")
        elif self.state == "think" and not job.done.is_set():
            if not job.long and waited > self.args.long_think:
                # Hermes is going to memory/tools — say "one sec" once and switch the animation
                job.long = True
                self.log("[THINK] долго — заполнитель")
                self.loop.stop()
                self.player.enqueue(random.choice(self.waits))
                self.ring.set_mode("think_long")
            elif self.think_sound and waited > self.args.think_sound_delay and not self.player.busy():
                self.loop.start(self.think_sound)  # after the filler — drops again
        if job.done.is_set():
            self.loop.stop()
        if job.done.is_set() and not self.player.busy():
            if job.error:
                self.log(f"[ERROR] {job.error}")
                self.set_state("error")
            else:
                self.set_state("idle")
            return True
        return False

    # ------------------------------------------------------------ run modes

    def run_text(self, text):
        self.start_answer(text=text)
        while not self.poll_answer():
            time.sleep(0.05)
        time.sleep(1.2 if self.state == "error" else 0.5)  # let the animation finish

    def run(self):
        a = self.args
        model = NpzModel(resolve_model(a.model, a.framework))
        feats = AudioFeatures(inference_framework=a.framework)
        vad = VAD()
        mic = Mic(a.mic_device)
        self.log(f"[RUN] модель {model.name}, порог {a.threshold}; перебить — "
                 + ("Enter" if sys.platform == "darwin" else "кнопка энкодера"))
        n, streak, mute_until = 0, 0, 0.0
        followup_at = None  # when to start listening for a follow-up after the answer
        preroll, rec, speech, silence = collections.deque(maxlen=4), [], False, 0.0

        listen_timeout = a.listen_timeout

        def start_listen(timeout):
            nonlocal rec, speech, silence, listen_timeout
            vad.reset_states()
            preroll.clear()
            rec, speech, silence, listen_timeout = [], False, 0.0, timeout
            self.set_state("listen")

        try:
            for frame in mic.frames():
                n += 1
                feats(frame)
                now = time.time()
                if self.button.event.is_set():
                    self.button.event.clear()
                    if self.state != "idle" or followup_at is not None:
                        followup_at = None
                        self.interrupt()
                        mute_until = now + 1.0
                        continue

                if self.state == "idle":
                    if followup_at is not None and now >= followup_at:
                        followup_at = None
                        self.log(f"[LISTEN] продолжение разговора — жду {a.followup_timeout:g} с")
                        start_listen(a.followup_timeout)
                        continue
                    if now < mute_until:
                        streak = 0
                        continue
                    if a.no_wake:
                        start_listen(a.listen_timeout)
                        continue
                    score = model(feats.get_features(16)) if n > 5 else 0.0
                    streak = streak + 1 if score >= a.threshold else 0
                    if streak >= a.patience:
                        streak = 0
                        self.log(f"[WAKE] score={score:.3f}")
                        self.greet()

                elif self.state == "greet":
                    if not self.player.busy():
                        start_listen(a.listen_timeout)

                elif self.state == "listen":
                    p = vad.predict(frame)
                    if not speech:
                        preroll.append(frame)
                        if p >= 0.5:
                            speech, rec, silence = True, list(preroll), 0.0
                        elif now - self.state_t > listen_timeout:
                            self.log("[LISTEN] тишина — не дождались фразы")
                            self.set_state("idle")
                            mute_until = now + 0.5
                    else:
                        rec.append(frame)
                        silence = 0.0 if p >= 0.5 else silence + FRAME_S
                        if silence >= a.end_silence or len(rec) * FRAME_S >= a.max_listen:
                            self.log(f"[LISTEN] фраза {len(rec) * FRAME_S:.1f} с")
                            self.start_answer(pcm=np.concatenate(rec))

                elif self.state in ("think", "speak"):
                    if self.poll_answer() and self.state == "idle":
                        mute_until = now + a.mute_after
                        if not a.no_followup:
                            # pause so the tail of our own voice from the speaker doesn't get recorded
                            followup_at = now + a.followup_delay

                elif self.state == "error":
                    if now - self.state_t > 1.0:
                        self.set_state("idle")
                        mute_until = now + 0.5
        finally:
            mic.close()

    def close(self):
        self.loop.stop()
        self.player.stop()
        self.button.close()
        self.ring.close()


def main():
    linux = sys.platform.startswith("linux")
    p = argparse.ArgumentParser(description="Smart speaker voice assistant")
    p.add_argument("--model", default="hey_peedor")
    p.add_argument("--threshold", type=float, default=0.5)
    p.add_argument("--patience", type=int, default=2)
    p.add_argument("--framework", choices=("tflite", "onnx"), default="tflite" if linux else "onnx")
    p.add_argument("--sounds", default=os.path.join(ROOT, "sounds", "greetings"))
    p.add_argument("--leds", choices=("pi", "console", "off"), default="pi" if linux else "console")
    p.add_argument("--mic-device", help="ALSA device (Pi) or avfoundation microphone name (Mac)")
    p.add_argument("--text", help="no microphone: send this question straight to Hermes and speak the answer")
    p.add_argument("--no-wake", action="store_true", help="no wake word: listen for a phrase right away")
    p.add_argument("--listen-timeout", type=float, default=5.0, help="s to wait for a phrase to start after the greeting")
    p.add_argument("--followup-timeout", type=float, default=5.0,
                   help="s to wait for the next phrase after an answer, then sleep")
    p.add_argument("--end-silence", type=float, default=0.9, help="s of silence that end a phrase")
    p.add_argument("--max-listen", type=float, default=12.0, help="s — maximum phrase length")
    p.add_argument("--no-followup", action="store_true", help="don't listen for a follow-up after an answer, go straight to sleep")
    p.add_argument("--followup-delay", type=float, default=0.4, help="s after an answer before listening starts")
    p.add_argument("--mute-after", type=float, default=1.5, help="s after an answer during which the wake word is ignored")
    p.add_argument("--wait-sounds", default=os.path.join(ROOT, "sounds", "wait"))
    p.add_argument("--think-sound", default=os.path.join(ROOT, "sounds", "think", "drops.wav"),
                   help="sound looped while thinking (empty — no sound)")
    p.add_argument("--think-sound-delay", type=float, default=1.0, help="s of thinking before the sound starts")
    p.add_argument("--long-think", type=float, default=5.0, help="s without an answer before saying 'one sec' (sounds/wait)")
    p.add_argument("--xai-voice", default="leo", help="xAI voice to use when Groq TTS hits its rate limit")
    p.add_argument("--keep-pulseaudio", action="store_true")
    args = p.parse_args()

    if linux and not args.keep_pulseaudio:
        free_sound_card()
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    bot = Assistant(args)
    try:
        if args.text:
            bot.run_text(args.text)
        else:
            bot.run()
    except KeyboardInterrupt:
        pass
    finally:
        bot.close()


if __name__ == "__main__":
    main()
