#!/usr/bin/env python3
"""
STT and TTS via cloud APIs: Groq (Whisper / Orpheus) and xAI (Grok Voice).
Stdlib only — nothing to install. Keys GROQ_API_KEY / XAI_API_KEY from the environment or from .env in the project root.

    python3 src/voice.py stt sample.wav [--lang ru] [--provider xai]
    python3 src/voice.py stt --record 5 [--device default]          # record 5 s from the microphone (arecord)
    python3 src/voice.py stt sample.wav --provider both              # both providers — cross-validation
    python3 src/voice.py tts "Привет!" -o out.wav [--voice eve] [--play]

groq: STT — whisper-large-v3-turbo / whisper-large-v3; TTS — canopylabs/orpheus-v1-english (officially English
      only, but it reads Russian in Cyrillic) or canopylabs/orpheus-arabic-saudi.
xai:  STT — grok-voice-transcribe-2.0; TTS — /v1/tts, multilingual voices (eve, ara, leo, ...), Russian included.
"""
import argparse
import io
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
import wave

RATE = 16000

PROVIDERS = {
    "groq": {
        "api": "https://api.groq.com/openai/v1",
        "key": "GROQ_API_KEY",
        "stt_path": "/audio/transcriptions",
        "stt_model": "whisper-large-v3-turbo",
        "tts_model": "canopylabs/orpheus-v1-english",
        "voice": "troy",
    },
    "xai": {
        "api": "https://api.x.ai/v1",
        "key": "XAI_API_KEY",
        "stt_path": "/stt",
        "stt_model": "grok-voice-transcribe-2.0",
        "tts_model": None,  # /v1/tts has no model parameter
        "voice": "eve",
    },
}
DEFAULT_TTS = "groq"  # orpheus-v1-english is officially English only, but reads Cyrillic decently


def api_key(name):
    key = os.environ.get(name)
    if key:
        return key
    env = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")  # .env lives in the project root (src/..)
    if os.path.exists(env):
        for line in open(env):
            k, _, v = line.strip().partition("=")
            if k == name:
                return v.strip().strip("'\"")
    raise RuntimeError(f"no {name} (neither in the environment nor in .env)")


def request(provider, path, body, content_type):
    cfg = PROVIDERS[provider]
    req = urllib.request.Request(
        cfg["api"] + path,
        data=body,
        headers={
            "Authorization": "Bearer " + api_key(cfg["key"]),
            "Content-Type": content_type,
            # without a UA, the Cloudflare in front of the API sometimes returns 403
            "User-Agent": "raspidr/1.0",
        },
    )
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            err = e.read().decode(errors="replace")
            # rate limit (Groq TTS free tier — 10 requests per minute): wait as long as asked and retry
            if e.code == 429 and attempt < 3:
                wait = float(e.headers.get("retry-after") or 10)
                print(f"{provider}: 429, waiting {wait:.0f} s", file=sys.stderr)
                time.sleep(wait + 0.5)
                continue
            raise RuntimeError(f"{provider} {path}: HTTP {e.code}: {err}") from None


def multipart(fields, audio, filename):
    boundary = uuid.uuid4().hex
    parts = [
        f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode()
        for name, value in fields.items()
    ]
    parts.append(
        f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{filename}"\r\n'
        f"Content-Type: application/octet-stream\r\n\r\n".encode() + audio + b"\r\n"
    )
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), "multipart/form-data; boundary=" + boundary


def stt(audio, filename="audio.wav", lang=None, provider="groq", model=None, prompt=None):
    """audio: bytes (wav/mp3/ogg/flac/...) -> text.
    prompt: hint with words that should be recognized (names, etc.)."""
    cfg = PROVIDERS[provider]
    fields = {"model": model or cfg["stt_model"]}
    if lang:
        fields["language"] = lang
    if provider == "groq":
        fields.update(response_format="json", temperature="0")
        if prompt:
            fields["prompt"] = prompt
    else:
        if lang:
            fields["format"] = "true"  # punctuation/normalization; xAI rejects it without language
        if prompt:
            fields["keyterm"] = prompt
    body, ctype = multipart(fields, audio, filename)
    return json.loads(request(provider, cfg["stt_path"], body, ctype))["text"].strip()


def tts(text, provider=DEFAULT_TTS, voice=None, lang="ru", model=None):
    """text -> wav bytes."""
    cfg = PROVIDERS[provider]
    if provider == "groq":
        payload = {"model": model or cfg["tts_model"], "input": text,
                   "voice": voice or cfg["voice"], "response_format": "wav"}
        return request(provider, "/audio/speech", json.dumps(payload).encode(), "application/json")
    payload = {"text": text, "voice_id": voice or cfg["voice"], "language": lang,
               "output_format": {"codec": "wav", "sample_rate": 24000}}
    return request(provider, "/tts", json.dumps(payload).encode(), "application/json")


def pcm_to_wav(pcm, rate=RATE):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    return buf.getvalue()


def record(seconds, device="default"):
    """Record from the microphone via arecord (Pi) -> wav bytes."""
    pcm = subprocess.run(
        ["arecord", "-q", "-D", device, "-f", "S16_LE", "-r", str(RATE), "-c", "1", "-t", "raw", "-d", str(seconds)],
        check=True, capture_output=True,
    ).stdout
    return pcm_to_wav(pcm)


def play(path):
    player = next((p for p in ("aplay", "afplay") if shutil.which(p)), None)
    if not player:
        sys.exit("nothing to play with: neither aplay nor afplay")
    subprocess.run([player, path], check=True)


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("stt", help="transcribe speech")
    s.add_argument("file", nargs="?", help="audio file (wav/mp3/ogg/flac/m4a/webm)")
    s.add_argument("--record", type=float, metavar="SEC", help="record SEC seconds from the microphone instead of a file")
    s.add_argument("--device", default="default", help="ALSA capture device")
    s.add_argument("--lang", help="ISO-639-1 language (ru, en, ...); auto-detect if omitted")
    s.add_argument("--provider", choices=["groq", "xai", "both"], default="groq")
    s.add_argument("--model", help="STT model (defaults to the provider's own)")
    s.add_argument("--prompt", help="hint: names/terms that should be recognized")

    t = sub.add_parser("tts", help="synthesize speech")
    t.add_argument("text")
    t.add_argument("-o", "--out", default="tts.wav")
    t.add_argument("--provider", choices=["groq", "xai"], default=DEFAULT_TTS)
    t.add_argument("--voice", help="voice (default: groq — troy, xai — eve)")
    t.add_argument("--lang", default="ru", help="language for xai (ru, en, ...)")
    t.add_argument("--model", help="TTS model (groq only)")
    t.add_argument("--play", action="store_true", help="play right away")

    args = p.parse_args()
    try:
        if args.cmd == "stt":
            if args.record:
                audio, name = record(args.record, args.device), "mic.wav"
            elif args.file:
                audio, name = open(args.file, "rb").read(), os.path.basename(args.file)
            else:
                p.error("need a file or --record SEC")
            providers = ["groq", "xai"] if args.provider == "both" else [args.provider]
            for prov in providers:
                t0 = time.monotonic()
                text = stt(audio, name, lang=args.lang, provider=prov, model=args.model, prompt=args.prompt)
                print(f"[{prov} {time.monotonic() - t0:.2f}s] {text}" if len(providers) > 1 else text)
        else:
            audio = tts(args.text, provider=args.provider, voice=args.voice, lang=args.lang, model=args.model)
            with open(args.out, "wb") as f:
                f.write(audio)
            print(args.out)
            if args.play:
                play(args.out)
    except RuntimeError as e:
        sys.exit(str(e))


if __name__ == "__main__":
    main()
