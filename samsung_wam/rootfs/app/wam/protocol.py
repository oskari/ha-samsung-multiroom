"""Speaker control protocol: XML commands as HTTP GETs on TCP 55001.

Each call opens a short connection; calls to one speaker are serialised because
the firmware copes badly with parallel control sockets.
"""
from __future__ import annotations

import re
import socket
import threading
import time
import urllib.parse
import uuid
import xml.etree.ElementTree as ET

PORT = 55001
_UID = str(uuid.uuid4())
_BLOCK = re.compile(rb"<(UIC|CPM)>.*?</\1>", re.S)
_locks: dict[str, threading.Lock] = {}
_guard = threading.Lock()

# Replies whose method name differs from the command (Get/Set prefix aside).
_REPLY = {
    "GetVolume": "VolumeLevel", "SetVolume": "VolumeLevel",
    "GetMute": "MuteStatus", "SetMute": "MuteStatus",
    "GetFunc": "CurrentFunc", "SetFunc": "CurrentFunc",
}

# Security labels as the speaker expects them in SetApManual.
SECURITY = {"wpa": "WPA/WPA2/FT PSK", "wep": "WEP", "none": "None", "eap": "802.1x EAP"}


class SpeakerError(Exception):
    pass


def _lock(ip: str) -> threading.Lock:
    with _guard:
        return _locks.setdefault(ip, threading.Lock())


def p_str(name: str, value) -> str:
    return f'<p type="str" name="{name}" val="{value}"/>'


def p_dec(name: str, value) -> str:
    return f'<p type="dec" name="{name}" val="{int(value)}"/>'


def p_cdata(name: str, value) -> str:
    value = str(value).replace("]]>", "]]]]><![CDATA[>")
    return f'<p type="cdata" name="{name}" val="empty"><![CDATA[{value}]]></p>'


def _parse(block: bytes) -> dict | None:
    try:
        root = ET.fromstring(block)
    except ET.ParseError:
        return None
    resp = root.find("response")
    fields = {}
    if resp is not None:
        fields = {c.tag: (c.text or "").strip() for c in resp if not len(c)}
    return {"method": root.findtext("method", ""), "result": resp.get("result", "") if resp is not None else "",
            "fields": fields, "xml": root}


def request(ip: str, method: str, params: str = "", pwron: bool = False,
            wait: float = 5.0, port: int = PORT) -> dict:
    """Send one command; return the matching reply {method, result, fields, xml}."""
    body = ("<pwron>on</pwron>" if pwron else "") + f"<name>{method}</name>" + params
    hints = {method, re.sub(r"^(Get|Set)", "", method), _REPLY.get(method, method)}
    hint_re = re.compile(rb"<method>(" + "|".join(sorted(hints)).encode() + rb")</method>")
    req = (f"GET /UIC?cmd={urllib.parse.quote(body)} HTTP/1.1\r\nHost: {ip}:{port}\r\n"
           f"mobileUUID: {_UID}\r\nmobileName: Wireless Audio\r\nmobileVersion: 1.0\r\n\r\n").encode()
    data = b""
    with _lock(ip):
        try:
            sock = socket.create_connection((ip, port), timeout=4)
        except OSError as exc:
            raise SpeakerError(f"{ip} is not reachable ({exc.__class__.__name__})") from exc
        with sock:
            sock.sendall(req)
            sock.settimeout(0.4)
            deadline = time.monotonic() + wait
            while time.monotonic() < deadline:
                try:
                    chunk = sock.recv(65536)
                except socket.timeout:
                    continue
                except OSError:
                    break
                if not chunk:
                    break
                data += chunk
                if any(hint_re.search(m.group(0)) for m in _BLOCK.finditer(data)):
                    break
    replies = [r for r in (_parse(m.group(0)) for m in _BLOCK.finditer(data)) if r]
    for r in replies:
        if r["method"] in hints:
            return r
    if replies:
        return replies[-1]
    return {"method": "", "result": "", "fields": {}, "xml": None}


def ok(reply: dict) -> bool:
    return reply.get("result") == "ok"


# -- read ------------------------------------------------------------------

def name(ip: str) -> str | None:
    r = request(ip, "GetSpkName", wait=3)
    return r["fields"].get("spkname") if ok(r) else None


def main_info(ip: str) -> dict:
    return request(ip, "GetMainInfo", wait=4)["fields"]


def ap_info(ip: str) -> dict:
    return request(ip, "GetApInfo", wait=4)["fields"]


def volume(ip: str) -> int | None:
    v = request(ip, "GetVolume", wait=3)["fields"].get("volume", "")
    return int(v) if v.isdigit() else None


def group_name(ip: str) -> str:
    return request(ip, "GetGroupName", wait=3)["fields"].get("groupname", "")


def software_version(ip: str) -> str:
    return request(ip, "GetSoftwareVersion", wait=3)["fields"].get("version", "")


def ap_list(ip: str) -> list[dict]:
    """Networks the speaker can see, strongest first."""
    r = request(ip, "GetApList", wait=15)
    rows = []
    if r.get("xml") is not None:
        for ap in r["xml"].iter("ap"):
            row = dict(ap.attrib)
            row.update({c.tag: (c.text or "").strip() for c in ap})
            if (row.get("ssid") or row.get("apname")) and str(row.get("index", "")).isdigit():
                row["ssid"] = row.get("ssid") or row.get("apname")
                rows.append(row)

    def strength(r: dict) -> int:
        return -int(r["rssi"]) if str(r.get("rssi", "")).lstrip("-").isdigit() else 0
    return sorted(rows, key=strength)


# -- write -----------------------------------------------------------------

def set_volume(ip: str, level: int) -> bool:
    return ok(request(ip, "SetVolume", p_dec("volume", max(0, min(30, level))), pwron=True))


def play_url(ip: str, url: str) -> bool:
    r = request(ip, "SetUrlPlayback", p_cdata("url", url) + p_dec("buffersize", 0)
                + p_dec("seektime", 0) + p_dec("resume", 0), wait=10)
    return ok(r) and r["method"] == "UrlPlayback"


def set_ap_manual(ip: str, ssid: str, password: str, security: str = "wpa") -> dict:
    return request(ip, "SetApManual", p_cdata("ssid", ssid) + p_str("password", password)
                   + p_str("security", SECURITY[security]), wait=10)


def set_ap_index(ip: str, index: int, password: str = "") -> dict:
    params = p_dec("apindex", index) + (p_str("password", password) if password else "")
    return request(ip, "SetAp", params, wait=10)


def ungroup(ip: str) -> bool:
    return ok(request(ip, "SetUngroup", wait=8))


def group_main(ip: str, name: str, mac: str, main_name: str, subs: list[tuple[str, str]]) -> bool:
    """For the main speaker: every member's (ip, mac)."""
    params = (p_cdata("name", name) + p_dec("index", 1) + p_str("type", "main")
              + p_dec("spknum", len(subs) + 1) + p_str("audiosourcemacaddr", mac)
              + p_cdata("audiosourcename", main_name) + p_str("audiosourcetype", "speaker"))
    for sub_ip, sub_mac in subs:
        params += p_str("subspkip", sub_ip) + p_str("subspkmacaddr", sub_mac)
    return ok(request(ip, "SetMultispkGroup", params, pwron=True, wait=10))


def group_sub(ip: str, name: str, total: int, main_ip: str, main_mac: str) -> bool:
    """For each other member: who the main speaker is."""
    params = (p_cdata("name", name) + p_dec("index", 1) + p_str("type", "sub")
              + p_dec("spknum", total) + p_str("mainspkip", main_ip) + p_str("mainspkmacaddr", main_mac))
    return ok(request(ip, "SetMultispkGroup", params, pwron=True, wait=10))
