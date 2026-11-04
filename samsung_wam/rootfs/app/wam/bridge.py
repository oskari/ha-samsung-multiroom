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
BYTES_PER_SEC = RATE * CHANNELS * WIDTH
CHUNK = 4096
QUEUE_CHUNKS = 90              # ~2 s between capture and the speaker
IDLE_CLOSE = 10 * 60
WS_BASE_PORT = 9100


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

    def info(self) -> dict:
        return {
            "id": self.id, "name": self.cfg["name"], "speaker_mac": self.cfg.get("speaker_mac", ""),
            "speaker_ip": self.speaker_ip, "speaker_volume": self.cfg.get("speaker_volume"),
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
        out = subprocess.run(["pactl", "load-module", "module-null-sink", f"sink_name={self.sink}",
                              f"rate={RATE}", f"channels={CHANNELS}",
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
        p = subprocess.Popen(["parec", "-d", f"{self.sink}.monitor", "--format=s16le",
                              f"--rate={RATE}", f"--channels={CHANNELS}", "--latency-msec=40", "--raw"],
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0)
        self.procs["parec"] = p
        self._thread(self._read_pcm, p)

    def _read_pcm(self, p: subprocess.Popen) -> None:
        buf = b""
        while not self.stop_ev.is_set():
            data = p.stdout.read(CHUNK)
            if not data:
                if not self.stop_ev.is_set():
                    self.state, self.error = "error", "audio capture stopped"
                    self.log(self.error)
                return
            buf += data
            usable = len(buf) - len(buf) % 4
            chunk, buf = buf[:usable], buf[usable:]
            if not chunk or not self.streaming.is_set():
                continue
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
                self.spotify_volume = ev["volume"]
            if ev.get("item"):
                self._set_track(ev["item"])
        elif t == "track_changed":
            self._set_track(ev.get("item") or {})
        elif t == "volume_changed":
            self.spotify_volume = ev.get("volume")

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
        while not self.stop_ev.wait(1.0):
            playing = self.status in ("playing", "buffering")
            if playing and not self.streaming.is_set():
                self._start_speaker()
            elif (self.streaming.is_set() and not playing and self.not_playing_since
                  and time.monotonic() - self.not_playing_since > IDLE_CLOSE):
                self.log("not playing for a while; ending the speaker stream")
                self.end_stream.set()

    def _start_speaker(self) -> None:
        ip = self.speaker_ip
        level = self.cfg.get("speaker_volume")
        try:
            if level is None:
                current = protocol.volume(ip) if self.level is None else self.level
                level = current if current else 5   # 0 at startup is not a level to obey
            self.level = level
            url = self.url()
            self.log(f"asking the speaker at {ip} to play {url} (volume {level})")
            protocol.set_volume(ip, level)
            if not protocol.play_url(ip, url):
                self.log("the speaker refused the stream")
        except (protocol.SpeakerError, OSError) as exc:
            self.log(f"speaker unreachable: {exc}")
        if not self.streaming.wait(timeout=15) and not self.stop_ev.is_set():
            self.log("the speaker hasn't connected to the stream; retrying")

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
            silence = bytes(CHUNK)
            while not self.stop_ev.is_set() and not self.end_stream.is_set():
                try:
                    data = self.pcm.get(timeout=0.1)
                except queue.Empty:
                    data = silence
                handler.wfile.write(data)
                self.sent += len(data)
        except OSError as exc:
            self.log(f"the speaker closed the stream ({exc.__class__.__name__})")
        finally:
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

    def add(self, name: str, speaker: dict, speaker_volume: int | None) -> Bridge:
        name = name.strip() or f"{speaker['name']} (Spotify)"
        if any(b.cfg["name"] == name for b in self.bridges.values()):
            raise ValueError(f"A bridge called \"{name}\" already exists.")
        if any(b.cfg.get("speaker_mac") == speaker["mac"] for b in self.bridges.values()):
            raise ValueError(f"{speaker['name']} already has a Spotify bridge.")
        cfg = {"id": new_id(), "name": name, "speaker_mac": speaker["mac"],
               "speaker_ip": speaker["ip"], "speaker_volume": speaker_volume}
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
        b.cfg.update(changes)

        def apply(st):
            for c in st["bridges"]:
                if c["id"] == bridge_id:
                    c.update(changes)
        self.store.update(apply)
        if "speaker_volume" in changes and changes["speaker_volume"] is not None:
            b.level = changes["speaker_volume"]
            if b.streaming.is_set() and not b.muted_for_pause:
                b._speaker(lambda: protocol.set_volume(b.speaker_ip, b.level), "set volume")

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
