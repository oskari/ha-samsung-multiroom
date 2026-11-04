"""Spotify Connect bridges: Soloist -> PulseAudio null sink -> WAV stream -> speaker.

Each bridge owns a null sink (wam_<id>), a Soloist instance playing into it, a
parec capture of its monitor, and the stream /stream/<id>.wav that its speaker
pulls via SetUrlPlayback. Soloist's WebSocket events drive the speaker:

  playing/buffering -> start the speaker stream (or unmute after a pause)
  paused            -> speaker volume 0 right away (the audio lags seconds)
  not playing for IDLE_CLOSE seconds -> end the speaker stream

Speaker facts (Shape M3 measured; M5 per the wambridge project): plain-HTTP
WAV, 44-byte header with 0xFFFFFFFF sizes, one connection at a time, no faked
Content-Length, and <pwron>on</pwron> on volume commands.
"""
from __future__ import annotations

import array
import collections
import os
import queue
import re
import shutil
import socket
import struct
import subprocess
import threading
import time
import uuid

from . import discovery, protocol, soloist
from .state import DATA

RATE, CHANNELS, WIDTH = 44100, 2, 2
BYTES_PER_SEC = RATE * CHANNELS * WIDTH      # of the 16-bit stream sent to the speaker
CAPTURE_FRAME = CHANNELS * 4                 # captured as 32-bit float
CHUNK = 4096                                 # bytes of 16-bit output per queue item
QUEUE_CHUNKS = 90              # ~2 s between capture and the speaker
IDLE_CLOSE = 10 * 60
WS_BASE_PORT = 9100

# Volume modes (cfg["volume_mode"]):
#   "stream"  - Spotify's slider scales the audio: Soloist sets its PulseAudio
#               stream volume and we pass the audio on as it is. The speaker's
#               own volume stays at cfg["speaker_volume"] (empty = leave it).
#   "speaker" - Spotify's slider sets the speaker's own volume (about a second).
#               Soloist still scales its stream - it treats any outside change
#               to that volume as the user moving the slider, so it must never
#               be touched - and we undo the scaling exactly. That's lossless
#               because the sink and the capture are 32-bit float.
#               cfg["speaker_volume"] is the speaker level at Spotify 100%.
VOLUME_MODES = ("stream", "speaker")
DEFAULT_MAX_LEVEL = 10
MAX_GAIN = 1000.0              # don't lift anything below -60 dB (Spotify ~10%)
GAIN_SETTLE = 0.3              # s around a volume change where the smaller gain is used


def pa_volume_gain(raw: int) -> float:
    """Gain that undoes a PulseAudio software volume (cubic curve, 65536 = 100%)."""
    factor = (raw / 65536.0) ** 3
    return min(MAX_GAIN, 1.0 / factor) if factor > 0 else 0.0


def float_to_s16(data: bytes, gains: tuple[float, float]) -> bytes:
    """Interleaved stereo float32 -> int16, applying per-channel gain, with clipping."""
    samples = array.array("f")
    samples.frombytes(data)
    gl, gr = gains[0] * 32767.0, gains[1] * 32767.0
    out = array.array("h", bytes(len(samples) * 2))
    for i in range(0, len(samples), 2):
        left, right = samples[i] * gl, samples[i + 1] * gr
        out[i] = 32767 if left > 32767 else -32768 if left < -32768 else int(left)
        out[i + 1] = 32767 if right > 32767 else -32768 if right < -32768 else int(right)
    return out.tobytes()


def wav_header() -> bytes:
    return (b"RIFF" + struct.pack("<I", 0xFFFFFFFF) + b"WAVE"
            + b"fmt " + struct.pack("<IHHIIHH", 16, 1, CHANNELS, RATE, BYTES_PER_SEC,
                                    CHANNELS * WIDTH, WIDTH * 8)
            + b"data" + struct.pack("<I", 0xFFFFFFFF))


def new_id() -> str:
    return uuid.uuid4().hex[:8]


class Bridge:
    def __init__(self, cfg: dict, ws_port: int, manager: "BridgeManager"):
        self.cfg = cfg
        self.id = cfg["id"]
        self.ws_port = ws_port
        self.m = manager
        self.dir = os.path.join(DATA, "bridges", self.id)
        self.sink = f"wam_{self.id}"
        self.module: str | None = None
        self.log_lines: collections.deque[str] = collections.deque(maxlen=200)
        self.stop_ev = threading.Event()
        self.threads: list[threading.Thread] = []
        self.procs: dict[str, subprocess.Popen] = {}
        self.pcm: queue.Queue[bytes] = queue.Queue(maxsize=QUEUE_CHUNKS)
        self.client_lock = threading.Lock()
        self.streaming = threading.Event()
        self.end_stream = threading.Event()
        self.state = "stopped"     # stopped | starting | running | pairing | error
        self.error = ""
        self.logged_in = False
        self.active = False
        self.status = "idle"
        self.track = ""
        self.spotify_volume: int | None = None
        self.not_playing_since = time.monotonic()
        self.level: int | None = None
        self.muted_for_pause = False
        self.sent = 0
        self.rate = 0.0
        self.pair_requested = False
        self.sent_level: int | None = None   # last level actually sent to the speaker
        self.vol_ev = threading.Event()      # wakes the volume sender (speaker mode)
        self.backoff = 0.0                   # seconds to wait before re-offering the stream
        self.next_start = 0.0
        self.gains = (1.0, 1.0)              # speaker mode: undoes Soloist's stream volume
        self.prev_gains = (1.0, 1.0)
        self.gains_settle_until = 0.0

    # -- helpers -----------------------------------------------------------
    def log(self, msg: str) -> None:
        line = f"{time.strftime('%H:%M:%S')} {msg}"
        self.log_lines.append(line)
        print(f"[{self.cfg['name']}] {msg}", flush=True)

    @property
    def speaker_ip(self) -> str:
        return self.m.speaker_ip(self.cfg)

    def url(self) -> str:
        host = self.m.advertise_ip or discovery.route_ip(self.speaker_ip)
        return f"http://{host}:{self.m.stream_port}/stream/{self.id}.wav"

    @property
    def speaker_mode(self) -> bool:
        return self.cfg.get("volume_mode") == "speaker"

    def max_level(self) -> int:
        v = self.cfg.get("speaker_volume")
        return int(v) if v else DEFAULT_MAX_LEVEL

    def mapped_level(self) -> int:
        """Speaker level for the current Spotify volume (speaker mode)."""
        v = self.spotify_volume if self.spotify_volume is not None else 50
        return max(0, min(30, round(v / 100 * self.max_level())))

    def info(self) -> dict:
        return {
            "id": self.id, "name": self.cfg["name"], "speaker_mac": self.cfg.get("speaker_mac", ""),
            "speaker_ip": self.speaker_ip, "speaker_volume": self.cfg.get("speaker_volume"),
            "volume_mode": "speaker" if self.speaker_mode else "stream",
            "speaker_level": self.sent_level if self.streaming.is_set() else None,
            "state": self.state, "error": self.error, "logged_in": self.logged_in,
            "active": self.active, "status": self.status, "track": self.track,
            "spotify_volume": self.spotify_volume, "streaming": self.streaming.is_set(),
            "rate": round(self.rate, 2), "log": list(self.log_lines)[-40:],
        }

    def _thread(self, target, *args) -> None:
        t = threading.Thread(target=target, args=args, daemon=True)
        t.start()
        self.threads.append(t)

    # -- lifecycle ---------------------------------------------------------
    def start(self, pair: bool = False) -> None:
        if self.state in ("starting", "running", "pairing"):
            return
        self.stop_ev.clear()
        self.error = ""
        self.state = "pairing" if pair else "starting"
        os.makedirs(os.path.join(self.dir, "data"), exist_ok=True)
        try:
            self._ensure_sink()
            self._start_soloist(pair)
            if not pair:
                self._start_capture()
                self._thread(self._events_loop)
                self._thread(self._control_loop)
                self._thread(self._stats_loop)
                self._thread(self._volume_loop)
        except Exception as exc:  # noqa: BLE001 - surfaced in the UI
            self.state, self.error = "error", str(exc)
            self.log(f"failed to start: {exc}")
            self.stop()

    def stop(self) -> None:
        self.stop_ev.set()
        self.end_stream.set()
        for name, p in list(self.procs.items()):
            if p.poll() is None:
                p.terminate()
                try:
                    p.wait(5)
                except subprocess.TimeoutExpired:
                    p.kill()
        self.procs.clear()
        if self.state != "error":
            self.state = "stopped"
        self.logged_in = self.active = False
        self.status = "idle"

    def remove(self) -> None:
        self.stop()
        if self.module:
            subprocess.run(["pactl", "unload-module", self.module], capture_output=True)
            self.module = None
        shutil.rmtree(self.dir, ignore_errors=True)

    def _ensure_sink(self) -> None:
        sinks = subprocess.run(["pactl", "list", "short", "sinks"], capture_output=True, text=True).stdout
        if f"\t{self.sink}\t" in sinks:
            return
        # Float sink: Soloist's volume scaling then loses no precision, so it can be undone.
        out = subprocess.run(["pactl", "load-module", "module-null-sink", f"sink_name={self.sink}",
                              f"rate={RATE}", f"channels={CHANNELS}", "format=float32le",
                              f"sink_properties=device.description={self.sink}"],
                             capture_output=True, text=True)
        if out.returncode:
            raise RuntimeError(f"could not create the audio output: {out.stderr.strip()}")
        self.module = out.stdout.strip()

    def _start_soloist(self, pair: bool) -> None:
        key = self.m.api_key
        if not key:
            raise RuntimeError("Set your Soloist API key in the app's Configuration tab.")
        cmd = [soloist.BIN, "-n", self.cfg["name"], "-k", key, "-D", os.path.join(self.dir, "data"),
               "-C", os.path.join(self.dir, "cache"), "-i", "70"]
        cmd += ["-p"] if pair else ["-w", f"127.0.0.1:{self.ws_port}"]
        env = dict(os.environ, PULSE_SINK=self.sink)
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env)
        self.procs["soloist"] = p
        self.log("pairing: open Spotify on the same network and pick this device" if pair
                 else "starting Soloist")
        self._thread(self._soloist_output, p, pair)

    def _soloist_output(self, p: subprocess.Popen, pair: bool) -> None:
        key = self.m.api_key
        for raw in iter(p.stdout.readline, b""):
            line = raw.decode("utf-8", "replace").rstrip()
            if key:
                line = line.replace(key, "<api-key>")
            line = re.sub(r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\.\d+: ", "", line)
            if line and not line.startswith("playing spotify:"):
                self.log(f"soloist: {line}")
            if "logged in as" in line:
                self.logged_in = True
            if "waiting for login" in line or "pairing mode" in line:
                self.logged_in = False
        code = p.wait()
        if self.stop_ev.is_set():
            return
        if pair:
            self.log("pairing finished" if code == 0 else f"pairing ended with code {code}")
            self.state = "stopped"
            self.m.after_pairing(self)
            return
        if code == 10:
            self.log("the Soloist build expired; fetching a new one")
            self.m.refresh_soloist_and_restart()
            return
        self.state, self.error = "error", f"Soloist exited with code {code}"
        self.log(self.error)
        self.stop_ev.set()
        self.end_stream.set()

    def _start_capture(self) -> None:
        p = subprocess.Popen(["parec", "-d", f"{self.sink}.monitor", "--format=float32le",
                              f"--rate={RATE}", f"--channels={CHANNELS}", "--latency-msec=40", "--raw"],
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0)
        self.procs["parec"] = p
        self._thread(self._read_pcm, p)
        self._thread(self._pa_watch)

    def _read_pcm(self, p: subprocess.Popen) -> None:
        buf = b""
        while not self.stop_ev.is_set():
            data = p.stdout.read(CHUNK * 2)  # float32 in, int16 out: half the size
            if not data:
                if not self.stop_ev.is_set():
                    self.state, self.error = "error", "audio capture stopped"
                    self.log(self.error)
                return
            buf += data
            usable = len(buf) - len(buf) % CAPTURE_FRAME
            raw, buf = buf[:usable], buf[usable:]
            if not raw or not self.streaming.is_set():
                continue
            chunk = float_to_s16(raw, self.current_gains())
            try:
                self.pcm.put_nowait(chunk)
            except queue.Full:
                try:
                    self.pcm.get_nowait()
                except queue.Empty:
                    pass
                self.pcm.put_nowait(chunk)

    # -- Soloist events ----------------------------------------------------
    def _events_loop(self) -> None:
        while not self.stop_ev.is_set():
            try:
                for ev in soloist.ws_events(self.ws_port, self.stop_ev):
                    self._on_event(ev)
            except OSError:
                pass
            self.stop_ev.wait(1)

    def _on_event(self, ev: dict) -> None:
        t = ev.get("type")
        if t == "auth_state":
            self.logged_in = bool(ev.get("logged_in"))
            self.active = bool(ev.get("is_active"))
            if self.state == "starting":
                self.state = "running"
        elif t == "device_changed":
            self.active = bool(ev.get("is_active"))
            if not self.active:
                self._set_status("idle")
        elif t in ("playback_state", "playback_changed"):
            if "status" in ev:
                self._set_status(ev["status"])
            if "volume" in ev:
                self._set_spotify_volume(ev["volume"])
            if ev.get("item"):
                self._set_track(ev["item"])
        elif t == "track_changed":
            self._set_track(ev.get("item") or {})
        elif t == "volume_changed":
            self._set_spotify_volume(ev.get("volume"))

    def _set_spotify_volume(self, volume) -> None:
        if volume is None:
            return
        self.spotify_volume = volume
        if self.speaker_mode:
            self.level = self.mapped_level()
            self.vol_ev.set()

    def _sink_inputs(self) -> list[dict]:
        """Soloist's PulseAudio streams on this bridge's sink: [{idx, corked, volumes}]."""
        try:
            sinks = subprocess.run(["pactl", "list", "short", "sinks"], capture_output=True,
                                   text=True, timeout=5).stdout
            sink_idx = next((ln.split("\t")[0] for ln in sinks.splitlines()
                             if ln.split("\t")[1:2] == [self.sink]), None)
            if sink_idx is None:
                return []
            out = subprocess.run(["pactl", "list", "sink-inputs"], capture_output=True,
                                 text=True, timeout=5).stdout
        except (OSError, subprocess.SubprocessError):
            return []
        streams = []
        for block in out.split("Sink Input #")[1:]:
            sink = re.search(r"^\s*Sink:\s*(\d+)", block, re.M)
            if not sink or sink.group(1) != sink_idx:
                continue
            volume_line = re.search(r"^\s*Volume:.*$", block, re.M)
            streams.append({
                "idx": block.split("\n", 1)[0].strip(),
                "corked": bool(re.search(r"^\s*Corked:\s*yes", block, re.M)),
                # raw PulseAudio volume per channel, e.g. "front-left: 33824 /  52% / ..."
                "raw": [int(v) for v in re.findall(r":\s*(\d+)\s*/", volume_line.group(0))]
                       if volume_line else [],
            })
        return streams

    def _audio_flowing(self) -> bool:
        """True when Soloist really plays into the sink (not just reports 'playing')."""
        return any(not s["corked"] for s in self._sink_inputs())

    # -- speaker mode: undo Soloist's stream volume ------------------------
    def current_gains(self) -> tuple[float, float]:
        if not self.speaker_mode:
            return (1.0, 1.0)
        if time.monotonic() < self.gains_settle_until:
            # Around a change the old and new scaling are both in flight: use the
            # smaller correction, so a change causes a brief dip, never a burst.
            return (min(self.prev_gains[0], self.gains[0]), min(self.prev_gains[1], self.gains[1]))
        return self.gains

    def _update_gains(self) -> None:
        """Read Soloist's stream volume (never change it: Soloist would report the
        change to Spotify as a slider move) and set the gain that undoes it."""
        streams = [s for s in self._sink_inputs() if s["raw"]]
        if not streams:
            return
        raw = streams[0]["raw"]
        new = (pa_volume_gain(raw[0]), pa_volume_gain(raw[1] if len(raw) > 1 else raw[0]))
        if new != self.gains:
            self.prev_gains, self.gains = self.gains, new
            self.gains_settle_until = time.monotonic() + GAIN_SETTLE

    def _pa_watch(self) -> None:
        """Follow PulseAudio stream changes as they happen (pactl subscribe)."""
        while not self.stop_ev.is_set():
            try:
                p = subprocess.Popen(["pactl", "subscribe"], stdout=subprocess.PIPE,
                                     stderr=subprocess.DEVNULL, text=True)
            except OSError:
                self.stop_ev.wait(5)
                continue
            self.procs["pactl-subscribe"] = p
            self._update_gains()
            for line in p.stdout:
                if self.stop_ev.is_set():
                    break
                if "sink-input" in line:
                    self._update_gains()
            if p.poll() is None:
                p.terminate()
            self.stop_ev.wait(1)

    def _volume_loop(self) -> None:
        """Speaker mode: send the latest wanted level, coalescing slider drags."""
        while not self.stop_ev.is_set():
            self.vol_ev.wait(1.0)
            self.vol_ev.clear()
            want = self.level
            if (not self.speaker_mode or want is None or want == self.sent_level
                    or self.muted_for_pause or not self.streaming.is_set()):
                continue
            try:
                if protocol.set_volume(self.speaker_ip, want):
                    self.sent_level = want
            except protocol.SpeakerError as exc:
                self.log(f"speaker volume failed: {exc}")
            self.stop_ev.wait(0.3)

    def _set_track(self, item: dict) -> None:
        identity = ((item.get("decorations") or {}).get("identity") or {})
        name = identity.get("name") or ""
        if name and name != self.track:
            self.track = name
            self.log(f"track: {name}")

    def _set_status(self, status: str) -> None:
        if status == self.status:
            return
        prev, self.status = self.status, status
        if status in ("playing", "buffering"):
            self.not_playing_since = 0.0
            if self.muted_for_pause and self.streaming.is_set() and self.level is not None:
                self._speaker(lambda: protocol.set_volume(self.speaker_ip, self.level), "unmute")
                self.sent_level = self.level
                self.muted_for_pause = False
        else:
            if prev in ("playing", "buffering"):
                self.not_playing_since = time.monotonic()
            if status == "paused" and self.streaming.is_set() and not self.muted_for_pause:
                self._speaker(lambda: protocol.set_volume(self.speaker_ip, 0), "mute for pause")
                self.muted_for_pause = True

    def _speaker(self, fn, what: str) -> None:
        try:
            fn()
        except protocol.SpeakerError as exc:
            self.log(f"speaker {what} failed: {exc}")

    # -- speaker control ---------------------------------------------------
    def _control_loop(self) -> None:
        silent_since = 0.0
        while not self.stop_ev.wait(1.0):
            self._update_gains()  # backstop for the pactl subscribe watcher
            playing = self.status in ("playing", "buffering")
            if playing and not self.streaming.is_set():
                # Spotify can report "playing" (e.g. right after a restart) while
                # Soloist produces nothing; starting the speaker on silence makes it
                # hang up and reconnect forever. Wait for real audio.
                if not self._audio_flowing():
                    if not silent_since:
                        silent_since = time.monotonic()
                    elif time.monotonic() - silent_since > 20 and silent_since > 0:
                        self.log("Spotify says it's playing, but no audio is coming from Soloist. "
                                 "Press play again in Spotify.")
                        silent_since = -1.0  # say it once
                    continue
                silent_since = 0.0
                if time.monotonic() >= self.next_start:
                    self._start_speaker()
            elif (self.streaming.is_set() and not playing and self.not_playing_since
                  and time.monotonic() - self.not_playing_since > IDLE_CLOSE):
                self.log("not playing for a while; ending the speaker stream")
                self.end_stream.set()
            if not playing:
                silent_since = 0.0

    def _start_speaker(self) -> None:
        ip = self.speaker_ip
        try:
            if self.speaker_mode:
                self._update_gains()
                level = self.mapped_level()
            else:
                level = self.cfg.get("speaker_volume")
                if level is None:
                    current = protocol.volume(ip) if self.level is None else self.level
                    level = current if current else 5   # 0 at startup is not a level to obey
            self.level = level
            url = self.url()
            self.log(f"asking the speaker at {ip} to play {url} (volume {level})")
            protocol.set_volume(ip, level)
            self.sent_level = level
            if not protocol.play_url(ip, url):
                self.log("the speaker refused the stream")
        except (protocol.SpeakerError, OSError) as exc:
            self.log(f"speaker unreachable: {exc}")
        if not self.streaming.wait(timeout=15) and not self.stop_ev.is_set():
            self.backoff = min(60.0, max(5.0, self.backoff * 2))
            self.next_start = time.monotonic() + self.backoff
            self.log(f"the speaker hasn't connected to the stream; retrying in {int(self.backoff)} s")

    def _stats_loop(self) -> None:
        last = self.sent
        while not self.stop_ev.wait(10):
            self.rate = (self.sent - last) / BYTES_PER_SEC / 10 if self.streaming.is_set() else 0.0
            last = self.sent

    # -- HTTP stream (called by the stream server) -------------------------
    def serve(self, handler) -> None:
        self.log(f"speaker {handler.client_address[0]} connected to the stream")
        if not self.client_lock.acquire(blocking=False):
            self.log("refusing a second stream request (one at a time)")
            handler.send_response(503)
            handler.end_headers()
            return
        try:
            handler.connection.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 65536)
            handler.send_response(200)
            handler.send_header("Content-Type", "audio/wav")
            handler.end_headers()
            handler.wfile.write(wav_header())
            while not self.pcm.empty():
                self.pcm.get_nowait()
            self.end_stream.clear()
            self.streaming.set()
            started = last_write = time.monotonic()
            while not self.stop_ev.is_set() and not self.end_stream.is_set():
                try:
                    data = self.pcm.get(timeout=0.05)
                except queue.Empty:
                    # No audio captured right now: fill the gap with silence at
                    # real-time speed, so the speaker's buffer doesn't run dry.
                    gap = min(time.monotonic() - last_write, 0.25)
                    n = int(gap * BYTES_PER_SEC)
                    n -= n % (CHANNELS * WIDTH)
                    if not n:
                        continue
                    data = bytes(n)
                handler.wfile.write(data)
                self.sent += len(data)
                last_write = time.monotonic()
        except OSError as exc:
            self.log(f"the speaker closed the stream ({exc.__class__.__name__})")
        finally:
            lasted = time.monotonic() - started if "started" in locals() else 0.0
            # A stream the speaker drops quickly is retried with growing pauses
            # (5 s up to a minute) instead of hammering it; a healthy one resets that.
            self.backoff = 0.0 if lasted > 30 else min(60.0, max(5.0, self.backoff * 2))
            self.next_start = time.monotonic() + self.backoff
            self.streaming.clear()
            self.client_lock.release()
            if self.muted_for_pause and self.level is not None:
                self._speaker(lambda: protocol.set_volume(self.speaker_ip, self.level), "restore volume")
                self.muted_for_pause = False
            self.log("stream ended")


class BridgeManager:
    def __init__(self, store, options: dict, log=print):
        self.store = store            # wam.state module
        self.api_key = (options.get("soloist_api_key") or "").strip()
        self.accepted = bool(options.get("accept_spotify_terms"))
        self.stream_port = int(options.get("stream_port") or 8099)
        self.advertise_ip = (options.get("advertise_ip") or "").strip()
        self.log = log
        self.bridges: dict[str, Bridge] = {}
        self.soloist_info: dict = {"present": False}
        self.soloist_error = ""
        self.lock = threading.Lock()
        self.known_speakers: dict[str, dict] = {}  # mac -> latest sweep entry

    # speaker IPs can change: follow the MAC address from the latest sweep
    def speaker_ip(self, cfg: dict) -> str:
        s = self.known_speakers.get(cfg.get("speaker_mac", ""))
        return s["ip"] if s else cfg.get("speaker_ip", "")

    def set_speakers(self, speakers: list[dict]) -> None:
        self.known_speakers = {s["mac"]: s for s in speakers if s.get("mac")}

    def ready(self) -> tuple[bool, str]:
        if not self.accepted:
            return False, "Accept Spotify's terms for Soloist in the app's Configuration tab."
        if not self.api_key:
            return False, "Add your Soloist API key in the app's Configuration tab."
        if not self.soloist_info.get("present"):
            return False, self.soloist_error or "Soloist isn't downloaded yet."
        return True, ""

    def prepare_soloist(self) -> None:
        if not self.accepted:
            return
        try:
            self.soloist_info = soloist.ensure(self.log)
            self.soloist_error = ""
        except Exception as exc:  # noqa: BLE001
            self.soloist_error = f"Could not get Soloist: {exc}"
            self.log(self.soloist_error)
            self.soloist_info = soloist.installed()

    def refresh_soloist_and_restart(self) -> None:
        def run():
            with self.lock:
                for b in self.bridges.values():
                    b.stop()
                try:
                    self.soloist_info = soloist.download(self.log)
                    self.soloist_error = ""
                except Exception as exc:  # noqa: BLE001
                    self.soloist_error = f"Could not update Soloist: {exc}"
                for b in self.bridges.values():
                    b.start()
        threading.Thread(target=run, daemon=True).start()

    def load(self) -> None:
        for i, cfg in enumerate(self.store.load()["bridges"]):
            self.bridges[cfg["id"]] = Bridge(cfg, WS_BASE_PORT + i, self)

    def start_all(self) -> None:
        ok, why = self.ready()
        if not ok:
            self.log(f"Spotify bridges not started: {why}")
            return
        for b in self.bridges.values():
            b.start()

    def stop_all(self) -> None:
        for b in self.bridges.values():
            b.stop()

    def _free_ws_port(self) -> int:
        used = {b.ws_port for b in self.bridges.values()}
        port = WS_BASE_PORT
        while port in used:
            port += 1
        return port

    def add(self, name: str, speaker: dict, speaker_volume: int | None,
            volume_mode: str = "stream") -> Bridge:
        if volume_mode not in VOLUME_MODES:
            raise ValueError(f"Unknown volume mode {volume_mode!r}.")
        name = name.strip() or f"{speaker['name']} (Spotify)"
        if any(b.cfg["name"] == name for b in self.bridges.values()):
            raise ValueError(f"A bridge called \"{name}\" already exists.")
        if any(b.cfg.get("speaker_mac") == speaker["mac"] for b in self.bridges.values()):
            raise ValueError(f"{speaker['name']} already has a Spotify bridge.")
        cfg = {"id": new_id(), "name": name, "speaker_mac": speaker["mac"],
               "speaker_ip": speaker["ip"], "speaker_volume": speaker_volume,
               "volume_mode": volume_mode}
        self.store.update(lambda st: st["bridges"].append(cfg))
        b = Bridge(cfg, self._free_ws_port(), self)
        self.bridges[cfg["id"]] = b
        ok, why = self.ready()
        if ok:
            b.start()
        else:
            b.state, b.error = "error", why
        return b

    def update(self, bridge_id: str, changes: dict) -> None:
        b = self.bridges[bridge_id]
        if changes.get("volume_mode", "stream") not in VOLUME_MODES:
            raise ValueError(f"Unknown volume mode {changes['volume_mode']!r}.")
        mode_changed = "volume_mode" in changes and changes["volume_mode"] != b.cfg.get("volume_mode", "stream")
        b.cfg.update(changes)

        def apply(st):
            for c in st["bridges"]:
                if c["id"] == bridge_id:
                    c.update(changes)
        self.store.update(apply)
        if mode_changed:
            b.log(f"volume mode: {'Spotify sets the speaker volume' if b.speaker_mode else 'Spotify scales the audio'}")
            b._update_gains()
            if b.speaker_mode:
                b.level = b.mapped_level()
                b.vol_ev.set()
            else:
                # Back to scaled audio, which is quieter than in speaker mode;
                # use the fixed level if one is set, else leave the speaker as it is.
                fixed = b.cfg.get("speaker_volume")
                if fixed and b.streaming.is_set() and not b.muted_for_pause:
                    b.level = fixed
                    b._speaker(lambda: protocol.set_volume(b.speaker_ip, b.level), "set volume")
                    b.sent_level = b.level
            return
        if "speaker_volume" in changes:
            level = b.mapped_level() if b.speaker_mode else changes.get("speaker_volume", b.level)
            if level is None:
                return
            b.level = level
            if b.speaker_mode:
                b.vol_ev.set()
            elif b.streaming.is_set() and not b.muted_for_pause:
                b._speaker(lambda: protocol.set_volume(b.speaker_ip, b.level), "set volume")
                b.sent_level = b.level

    def remove(self, bridge_id: str) -> None:
        b = self.bridges.pop(bridge_id)
        b.remove()
        self.store.update(lambda st: st.__setitem__(
            "bridges", [c for c in st["bridges"] if c["id"] != bridge_id]))

    def restart(self, bridge_id: str) -> None:
        b = self.bridges[bridge_id]
        b.stop()
        b.state = "stopped"
        ok, why = self.ready()
        if ok:
            b.start()
        else:
            b.state, b.error = "error", why

    def pair(self, bridge_id: str) -> None:
        b = self.bridges[bridge_id]
        ok, why = self.ready()
        if not ok:
            raise RuntimeError(why)
        b.stop()
        b.state = "stopped"
        b.start(pair=True)

    def after_pairing(self, b: Bridge) -> None:
        b.stop()
        b.state = "stopped"
        b.start()

    def stream(self, bridge_id: str) -> Bridge | None:
        return self.bridges.get(bridge_id)
