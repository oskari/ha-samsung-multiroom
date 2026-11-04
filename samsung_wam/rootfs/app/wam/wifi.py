"""Putting speakers on Wi-Fi.

Two ways, both using the speaker's Wi-Fi setup commands:

* On the network: the speaker is already reachable (Ethernet, or another
  network) and is told to join a Wi-Fi network directly.
* Setup hotspot: a new speaker in setup mode (WPS/Wi-Fi SETUP held ~5 s) opens
  a network named "[Samsung] ..." with password 1234567890. The Home Assistant
  host's own Wi-Fi adapter joins it through the Supervisor network API, the
  setup command is sent, and the adapter is switched off again. For safety this
  is only offered when the host's Wi-Fi adapter is not otherwise in use.
"""
from __future__ import annotations

import http.client
import json
import os
import time

from . import protocol

HOTSPOT_PASSWORD = "1234567890"  # the speakers' setup hotspot password


def is_speaker_hotspot(ssid: str) -> bool:
    """Whether a Wi-Fi network name is a speaker's setup hotspot."""
    if ssid.startswith("DIRECT"):
        return "Samsung Audio Setup" in ssid
    return "[Samsung]" in ssid or "Samsung Wireless Audio" in ssid


def password_problems(password: str) -> list[str]:
    return [f"The password contains {ch!r}; the speaker may read it wrongly "
            "(passwords are sent unescaped)." for ch in '"<&' if ch in password]


def join(ip: str, password: str, ssid: str | None = None, index: int | None = None) -> str:
    """Tell a reachable speaker to join a network; returns the reply result."""
    if ssid is not None:
        if "]]>" in ssid:
            raise ValueError("That network name can't be used.")
        security = "wpa" if password else "none"
        r = protocol.set_ap_manual(ip, ssid, password, security)
    else:
        r = protocol.set_ap_index(ip, int(index), password)
    return r.get("result") or "no reply (normal: the speaker is switching networks)"


# --------------------------------------------------------------------------
# Supervisor network API (host Wi-Fi)
# --------------------------------------------------------------------------

class Supervisor:
    def __init__(self):
        self.token = os.environ.get("SUPERVISOR_TOKEN", "")

    @property
    def available(self) -> bool:
        return bool(self.token)

    def call(self, method: str, path: str, body: dict | None = None, timeout: float = 30) -> dict:
        conn = http.client.HTTPConnection("supervisor", 80, timeout=timeout)
        try:
            conn.request(method, path, body=json.dumps(body) if body is not None else None,
                         headers={"Authorization": f"Bearer {self.token}",
                                  "Content-Type": "application/json"})
            resp = conn.getresponse()
            raw = resp.read().decode("utf-8", "replace")
        finally:
            conn.close()
        try:
            data = json.loads(raw)
        except ValueError:
            data = {"result": "error", "message": raw[:200]}
        if resp.status >= 400 or data.get("result") == "error":
            raise RuntimeError(data.get("message") or f"Supervisor returned HTTP {resp.status}")
        return data.get("data", data)


def host_wifi(sup: Supervisor) -> dict:
    """Whether the host's Wi-Fi adapter can be borrowed for hotspot setup."""
    if not sup.available:
        return {"usable": False, "reason": "Not running under Home Assistant Supervisor."}
    try:
        info = sup.call("GET", "/network/info")
    except Exception as exc:  # noqa: BLE001 - shown to the user
        return {"usable": False, "reason": f"Supervisor network API: {exc}"}
    ifaces = info.get("interfaces", [])
    wifi = next((i for i in ifaces if i.get("type") == "wireless"), None)
    if wifi is None:
        return {"usable": False, "reason": "This Home Assistant host has no Wi-Fi adapter."}
    wired_up = any(i.get("type") == "ethernet" and i.get("connected") for i in ifaces)
    if not wired_up:
        return {"usable": False, "interface": wifi["interface"],
                "reason": "The host isn't connected by Ethernet, so its Wi-Fi can't be borrowed."}
    if wifi.get("connected") or wifi.get("primary"):
        return {"usable": False, "interface": wifi["interface"],
                "reason": "The host's Wi-Fi is in use. Disconnect it in Settings > System > Network first."}
    return {"usable": True, "interface": wifi["interface"], "reason": ""}


def find_hotspots(sup: Supervisor, iface: str) -> list[dict]:
    data = sup.call("GET", f"/network/interface/{iface}/accesspoints", timeout=60)
    aps = data.get("accesspoints", [])
    return [ap for ap in aps if is_speaker_hotspot(ap.get("ssid", ""))]


def hotspot_setup(sup: Supervisor, iface: str, hotspot: str, password: str,
                  ssid: str | None, index: int | None, log) -> str:
    """Join the speaker's hotspot with the host Wi-Fi, send the setup, restore."""
    status = host_wifi(sup)
    if not status.get("usable"):
        raise RuntimeError(status.get("reason"))
    log(f"Connecting the host's Wi-Fi ({iface}) to {hotspot}")
    sup.call("POST", f"/network/interface/{iface}/update", {
        "enabled": True,
        "ipv4": {"method": "auto"}, "ipv6": {"method": "disabled"},
        "wifi": {"mode": "infrastructure", "ssid": hotspot, "auth": "wpa-psk", "psk": HOTSPOT_PASSWORD},
    }, timeout=90)
    try:
        speaker = None
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline and speaker is None:
            info = sup.call("GET", f"/network/interface/{iface}/info")
            gateway = (info.get("ipv4") or {}).get("gateway")
            if gateway and protocol.name(gateway):
                speaker = gateway
                break
            time.sleep(3)
        if speaker is None:
            raise RuntimeError("Connected, but the speaker didn't answer on its hotspot.")
        log(f"Talking to {protocol.name(speaker) or 'the speaker'} at {speaker}")
        result = join(speaker, password, ssid, index)
        log(f"Speaker replied: {result}")
        return result
    finally:
        log("Switching the host's Wi-Fi off again")
        try:
            sup.call("POST", f"/network/interface/{iface}/update", {"enabled": False}, timeout=60)
        except Exception as exc:  # noqa: BLE001
            log(f"Could not switch the host's Wi-Fi off: {exc}. Check Settings > System > Network.")
