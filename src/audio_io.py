"""
Speaker audio: microphone in 80 ms frames and a queued player.

Mic    — Linux: arecord (ALSA default → dsnoop), macOS: ffmpeg avfoundation. Always 16 kHz, mono, int16.
Player — queue of WAVs (path or bytes), plays them one at a time: aplay -D default (Pi) / afplay (Mac).
"""
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading

import numpy as np

RATE = 16000
FRAME = 1280  # 80 ms — openWakeWord step
MAC_MIC = "MacBook Pro Microphone"


class Mic:
    def __init__(self, device=None):
        if sys.platform == "darwin":
            cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "avfoundation",
                   "-i", f":{device or MAC_MIC}", "-ac", "1", "-ar", str(RATE), "-f", "s16le", "-"]
        else:
            cmd = ["arecord", "-q", "-D", device or "default", "-f", "S16_LE", "-r", str(RATE), "-c", "1",
                   "-t", "raw"]
        self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL)

    def frames(self):
        while True:
            data = self.proc.stdout.read(FRAME * 2)
            if len(data) < FRAME * 2:
                raise RuntimeError("микрофон: " + self.proc.stderr.read().decode(errors="replace").strip())
            yield np.frombuffer(data, dtype=np.int16)

    def close(self):
        self.proc.terminate()
        self.proc.wait()


class Player:
    """Playback queue in its own thread. busy() — something is queued or playing."""

    def __init__(self):
        self.cmd = ["aplay", "-q", "-D", "default"] if shutil.which("aplay") else ["afplay"]
        self.q = queue.Queue()
        self.proc = None
        self.pending = 0  # enqueued but not finished yet — no "gap" between the queue and playback
        self.generation = 0  # stop() bumps it — jobs from an old "epoch" are dropped
        self.lock = threading.Lock()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def enqueue(self, audio, label=""):
        """audio — a file path or WAV bytes."""
        with self.lock:
            self.pending += 1
            self.q.put((self.generation, audio, label))

    def busy(self):
        with self.lock:
            return self.pending > 0

    def stop(self):
        with self.lock:
            self.generation += 1
            if self.proc and self.proc.poll() is None:
                self.proc.terminate()

    def _run(self):
        while True:
            gen, audio, label = self.q.get()
            tmp = None
            try:
                with self.lock:
                    if gen != self.generation:
                        continue
                path = audio
                if isinstance(audio, (bytes, bytearray)):
                    fd, tmp = tempfile.mkstemp(suffix=".wav", prefix="raspidr-")
                    with os.fdopen(fd, "wb") as f:
                        f.write(audio)
                    path = tmp
                with self.lock:
                    if gen != self.generation:
                        continue
                    self.proc = subprocess.Popen(self.cmd + [path], stdout=subprocess.DEVNULL,
                                                 stderr=subprocess.DEVNULL)
                self.proc.wait()
            finally:
                if tmp:
                    os.remove(tmp)
                with self.lock:
                    self.pending -= 1


class LoopPlayer:
    """Background sound on a loop (e.g. the waiting "drops") — separate from the Player queue, mixed in parallel via dmix."""

    def __init__(self):
        self.cmd = ["aplay", "-q", "-D", "default"] if shutil.which("aplay") else ["afplay"]
        self.proc = None
        self.running = False
        self.generation = 0  # each start gets its own number — an old thread won't keep going after stop()+start()
        self.lock = threading.Lock()

    def start(self, path):
        with self.lock:
            if self.running:
                return
            self.running = True
            self.generation += 1
            gen = self.generation
        threading.Thread(target=self._run, args=(path, gen), daemon=True).start()

    def _run(self, path, gen):
        while True:
            with self.lock:
                if not self.running or gen != self.generation:
                    return
                self.proc = subprocess.Popen(self.cmd + [path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                proc = self.proc
            proc.wait()

    def stop(self):
        with self.lock:
            self.running = False
            if self.proc and self.proc.poll() is None:
                self.proc.terminate()
