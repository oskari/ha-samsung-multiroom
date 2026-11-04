"""Spotify Soloist: download/update the official build, and read its WebSocket API.

Soloist may not be redistributed, so each install downloads it straight from
Spotify's CDN, and only after the user has accepted Spotify's terms in the app
options. Builds expire 90 days after their build date (exit code 10); the app
fetches a fresh one before that happens.
"""
from __future__ import annotations

import base64
import datetime as dt
import json
import os
import platform
import re
import socket
import struct
import subprocess
import tarfile
import tempfile
import threading
import urllib.request

from .state import DATA

CDN = "https://soloist-builds.spotifycdn.com/"
ARCHIVES = {
    "x86_64": "soloist_release_x86_64.tar.gz", "amd64": "soloist_release_x86_64.tar.gz",
    "aarch64": "soloist_release_arm64.tar.gz", "arm64": "soloist_release_arm64.tar.gz",
    "armv7l": "soloist_release_arm32.tar.gz", "armv8l": "soloist_release_arm32.tar.gz",
}
BIN_DIR = os.path.join(DATA, "soloist")
BIN = os.path.join(BIN_DIR, "soloist")
LIFETIME_DAYS = 90
_lock = threading.Lock()


def archive_name() -> str:
    machine = platform.machine().lower()
    if machine not in ARCHIVES:
        raise RuntimeError(f"Soloist has no build for this CPU ({machine}).")
    return ARCHIVES[machine]


def installed() -> dict:
    """{'present', 'version', 'built', 'expires', 'days_left'} of the local build."""
    if not os.path.exists(BIN):
        return {"present": False}
    try:
        out = subprocess.run([BIN, "--version"], capture_output=True, text=True, timeout=15).stdout
    except (OSError, subprocess.SubprocessError) as exc:
        return {"present": True, "error": str(exc)}
    m = re.search(r"soloist (\S+) build \d+ \((\d{8})\)", out)
    if not m:
        return {"present": True, "version": out.strip()[:80]}
    built = dt.datetime.strptime(m.group(2), "%Y%m%d").date()
    expires = built + dt.timedelta(days=LIFETIME_DAYS)
    return {"present": True, "version": m.group(1), "built": built.isoformat(),
            "expires": expires.isoformat(), "days_left": (expires - dt.date.today()).days}


def download(log=print) -> dict:
    """Fetch the current build for this CPU from Spotify and install it."""
    with _lock:
        name = archive_name()
        os.makedirs(BIN_DIR, exist_ok=True)
        log(f"Downloading Spotify Soloist ({name}) from {CDN}")
        with tempfile.TemporaryDirectory(dir=BIN_DIR) as tmp:
            path = os.path.join(tmp, name)
            with urllib.request.urlopen(CDN + name, timeout=120) as resp, open(path, "wb") as f:
                while chunk := resp.read(1 << 16):
                    f.write(chunk)
            with tarfile.open(path) as tar:
                for member in tar.getmembers():
                    base = os.path.basename(member.name)
                    if member.isfile() and base in ("soloist", "CHANGELOG.md", "THIRD_PARTY_LICENSES.txt"):
                        member.name = base
                        tar.extract(member, tmp, filter="data")
            new = os.path.join(tmp, "soloist")
            if not os.path.exists(new):
                raise RuntimeError("The Soloist archive didn't contain the soloist program.")
            os.chmod(new, 0o755)
            for base in ("soloist", "CHANGELOG.md", "THIRD_PARTY_LICENSES.txt"):
                if os.path.exists(os.path.join(tmp, base)):
                    os.replace(os.path.join(tmp, base), os.path.join(BIN_DIR, base))
    info = installed()
    log(f"Installed Soloist {info.get('version')} (expires {info.get('expires')})")
    return info


def ensure(log=print, min_days: int = 7) -> dict:
    info = installed()
    if not info.get("present") or info.get("days_left", LIFETIME_DAYS) < min_days or "error" in info:
        info = download(log)
    return info


# --------------------------------------------------------------------------
# Minimal WebSocket client (stdlib only) for Soloist's local API
# --------------------------------------------------------------------------

def ws_events(port: int, stop: threading.Event):
    """Yield JSON events from ws://127.0.0.1:port until it closes or stop is set."""
    sock = socket.create_connection(("127.0.0.1", port), timeout=10)
    try:
        key = base64.b64encode(os.urandom(16)).decode()
        sock.sendall((f"GET / HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nUpgrade: websocket\r\n"
                      f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
                      "Sec-WebSocket-Version: 13\r\n\r\n").encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            part = sock.recv(4096)
            if not part:
                raise ConnectionError("handshake closed")
            buf += part
        head, buf = buf.split(b"\r\n\r\n", 1)
        if b" 101 " not in head.split(b"\r\n", 1)[0]:
            raise ConnectionError("handshake refused")
        sock.settimeout(1.0)

        def need(n: int) -> bytes:
            nonlocal buf
            while len(buf) < n:
                if stop.is_set():
                    raise ConnectionError("stopping")
                try:
                    part = sock.recv(65536)
                except socket.timeout:
                    continue
                if not part:
                    raise ConnectionError("closed")
                buf += part
            out, buf = buf[:n], buf[n:]
            return out

        def send(opcode: int, payload: bytes) -> None:
            mask = os.urandom(4)
            data = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
            sock.sendall(bytes([0x80 | opcode, 0x80 | len(payload)]) + mask + data)

        message = b""
        while not stop.is_set():
            b0, b1 = need(2)
            opcode, length = b0 & 0x0F, b1 & 0x7F
            if length == 126:
                length = struct.unpack(">H", need(2))[0]
            elif length == 127:
                length = struct.unpack(">Q", need(8))[0]
            mask = need(4) if b1 & 0x80 else b""
            payload = need(length)
            if mask:
                payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
            if opcode == 0x9:
                send(0xA, payload[:125])
            elif opcode == 0x8:
                return
            elif opcode in (0x0, 0x1):
                message += payload
                if b0 & 0x80:
                    try:
                        yield json.loads(message.decode("utf-8", "replace"))
                    except ValueError:
                        pass
                    message = b""
    finally:
        sock.close()
