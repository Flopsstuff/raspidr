"""
Hermes Agent client (OpenAI-compatible API on the LAN) with streaming.
URL/key/model — HERMES_API_URL / HERMES_API_KEY / HERMES_MODEL from the environment or .env (via voice.api_key).
"""
import json
import urllib.request

import voice


def config():
    url = voice.api_key("HERMES_API_URL").rstrip("/")
    try:
        model = voice.api_key("HERMES_MODEL")
    except RuntimeError:
        model = "hermes-agent"
    return url, voice.api_key("HERMES_API_KEY"), model


def stream_chat(messages, cancel=None, timeout=120):
    """Generator of answer text chunks. cancel — threading.Event: once set, stop reading."""
    url, key, model = config()
    body = json.dumps({"model": model, "stream": True, "messages": messages}).encode()
    req = urllib.request.Request(url + "/chat/completions", data=body, headers={
        "Authorization": "Bearer " + key, "Content-Type": "application/json", "User-Agent": "raspidr/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        for raw in r:
            if cancel is not None and cancel.is_set():
                return
            line = raw.decode(errors="replace").strip()
            if not line.startswith("data:") or line == "data: [DONE]":
                continue
            choice = (json.loads(line[5:]).get("choices") or [{}])[0]
            delta = choice.get("delta", {}).get("content")
            if delta:
                yield delta
