"""Finding speakers: sweep this host's networks (plus configured ones) for TCP 55001."""
from __future__ import annotations

import ipaddress
import json
import socket
import subprocess
from concurrent.futures import ThreadPoolExecutor

from . import protocol

_SKIP_IFACES = ("lo", "docker", "hassio", "veth", "br-", "tun", "wg")


def local_subnets() -> list[ipaddress.IPv4Network]:
    """The host's LAN networks, each capped at /24 so a sweep stays quick."""
    nets: list[ipaddress.IPv4Network] = []
    try:
        out = subprocess.run(["ip", "-j", "-4", "addr", "show"], capture_output=True,
                             text=True, timeout=5).stdout
        entries = json.loads(out or "[]")
    except (OSError, ValueError, subprocess.SubprocessError):
        entries = []
    for e in entries:
        if e.get("ifname", "").startswith(_SKIP_IFACES):
            continue
        for a in e.get("addr_info", []):
            iface = ipaddress.ip_interface(f"{a['local']}/{a['prefixlen']}")
            if iface.ip.is_link_local or iface.ip.is_loopback:
                continue
            net = iface.network if iface.network.prefixlen >= 24 else ipaddress.ip_interface(f"{iface.ip}/24").network
            if net not in nets:
                nets.append(net)
    return nets


def parse_subnet(text: str) -> ipaddress.IPv4Network:
    net = ipaddress.ip_network(text.strip(), strict=False)
    if net.version != 4:
        raise ValueError("only IPv4 networks are supported")
    if net.prefixlen < 24:
        net = ipaddress.ip_network(f"{net.network_address}/24")
    return net


def probe(hosts: list[str], port: int = protocol.PORT, timeout: float = 0.6) -> list[str]:
    def is_open(host: str) -> str | None:
        with socket.socket() as s:
            s.settimeout(timeout)
            return host if s.connect_ex((host, port)) == 0 else None
    with ThreadPoolExecutor(max_workers=128) as pool:
        return [h for h in pool.map(is_open, hosts) if h]


def speaker_state(ip: str) -> dict | None:
    """Everything the UI shows about one speaker, or None if it isn't one."""
    try:
        name = protocol.name(ip)
        if name is None:
            return None  # e.g. a Samsung TV also listening on 55001
        info = protocol.main_info(ip)
        ap = protocol.ap_info(ip)
        vol = protocol.volume(ip)
        role = {"M": "main", "S": "sub"}.get(info.get("grouptype", ""), "")
        group = protocol.group_name(ip) if role == "main" else ""
    except protocol.SpeakerError:
        return None
    return {
        "ip": ip, "name": name, "mac": (info.get("spkmacaddr") or ap.get("mac", "")).lower(),
        "model": info.get("spkmodelname", ""), "role": role,
        "main_ip": info.get("groupmainip", "") if role == "sub" else "", "group": group,
        "volume": vol, "ssid": ap.get("ssid", ""), "rssi": ap.get("rssi", ""),
        "channel": ap.get("ch", ""), "connection": ap.get("connectiontype", ""),
    }


def sweep(nets: list[ipaddress.IPv4Network], extra_hosts: list[str] = ()) -> list[dict]:
    hosts = list(dict.fromkeys([*(str(h) for n in nets for h in n.hosts()), *extra_hosts]))
    found = probe(hosts)
    with ThreadPoolExecutor(max_workers=16) as pool:
        states = [s for s in pool.map(speaker_state, found) if s]
    return sorted(states, key=lambda s: (s["name"].lower(), s["ip"]))


def route_ip(target: str) -> str:
    """This host's address on the route to `target` (no packet is sent)."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.connect((target, 9))
        return s.getsockname()[0]
