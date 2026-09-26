"""
HTTP API for other agents: say a text out loud on the speaker.

    POST /say    Authorization: Bearer $RASPIDR_API_TOKEN
                 body: {"text": "..."} (Content-Type: application/json) or the plain text itself
    → 200 {"ok": true, "chunks": N}  synthesized and queued: it plays as soon as the speaker is free (a dialog in
                                     progress isn't interrupted); the encoder button cuts it off like an answer
      400 no text / too long · 401 wrong or missing token · 404 · 413 body too large · 502 TTS failed

    curl -H "Authorization: Bearer $TOKEN" --data-binary 'Привет от агента' http://<speaker>:8765/say

Enabled only when RASPIDR_API_TOKEN is set (environment or .env); port RASPIDR_API_PORT (default 8765),
all interfaces. Plain HTTP: the token is only as safe as the LAN it travels over.
"""
import hmac
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DEFAULT_PORT = 8765
MAX_BODY = 16 * 1024
MAX_TEXT = 2000


def start(say, log, token, port=DEFAULT_PORT, host="0.0.0.0"):
    """Serve in a daemon thread. say(text) → number of chunks; raises if nothing could be synthesized."""
    if not token:
        raise ValueError("empty token")
    expected = f"Bearer {token}".encode()

    class Handler(BaseHTTPRequestHandler):
        server_version = "raspidr"
        sys_version = ""

        def reply(self, code, obj, headers=()):
            body = json.dumps(obj, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            for k, v in headers:
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            if self.path.rstrip("/") != "/say":
                return self.reply(404, {"error": "not found"})
            if not hmac.compare_digest(self.headers.get("Authorization", "").encode(), expected):
                log(f"[API] 401 от {self.client_address[0]}")
                return self.reply(401, {"error": "unauthorized"}, [("WWW-Authenticate", "Bearer")])
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                return self.reply(400, {"error": "bad Content-Length"})
            if length > MAX_BODY:
                return self.reply(413, {"error": f"body over {MAX_BODY} bytes"})
            text = self.rfile.read(length).decode("utf-8", errors="replace")
            if "json" in self.headers.get("Content-Type", ""):
                try:
                    text = json.loads(text).get("text", "")
                except (ValueError, AttributeError):
                    return self.reply(400, {"error": 'expected {"text": "..."}'})
            text = str(text).strip()
            if not text:
                return self.reply(400, {"error": "no text"})
            if len(text) > MAX_TEXT:
                return self.reply(400, {"error": f"text over {MAX_TEXT} characters"})
            log(f"[API] /say от {self.client_address[0]}, {len(text)} знаков")
            try:
                chunks = say(text)
            except Exception as e:
                return self.reply(502, {"error": str(e)[:300]})
            self.reply(200, {"ok": True, "chunks": chunks})

        def do_GET(self):
            self.reply(405 if self.path.rstrip("/") == "/say" else 404, {"error": "POST /say"})

        def log_message(self, fmt, *args):  # the default writes every request to stderr
            pass

    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server
