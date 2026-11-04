"""Options from Home Assistant and the app's own persisted state (/data)."""
from __future__ import annotations

import json
import os
import threading

DATA = os.environ.get("WAM_DATA", "/data")
_lock = threading.Lock()

DEFAULTS = {
    "subnets": [],      # extra networks to search, e.g. an IoT VLAN
    "speakers": {},     # mac -> {ip, name, model}; remembered between scans
    "bridges": [],      # [{id, name, speaker_mac, speaker_ip, speaker_volume}]
}


def options() -> dict:
    try:
        with open(os.path.join(DATA, "options.json"), encoding="utf-8") as f:
            opts = json.load(f)
    except (OSError, ValueError):
        opts = {}
    opts.setdefault("accept_spotify_terms", False)
    opts.setdefault("soloist_api_key", "")
    opts.setdefault("stream_port", 8099)
    opts.setdefault("advertise_ip", "")
    return opts


def load() -> dict:
    with _lock:
        try:
            with open(os.path.join(DATA, "state.json"), encoding="utf-8") as f:
                state = json.load(f)
        except (OSError, ValueError):
            state = {}
    for k, v in DEFAULTS.items():
        state.setdefault(k, json.loads(json.dumps(v)))
    return state


def save(state: dict) -> None:
    with _lock:
        path = os.path.join(DATA, "state.json")
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
        os.replace(tmp, path)


def update(fn) -> dict:
    """Load, change with fn(state), save; returns the new state."""
    state = load()
    fn(state)
    save(state)
    return state
