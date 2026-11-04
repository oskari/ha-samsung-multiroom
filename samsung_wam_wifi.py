#!/usr/bin/env python3
"""Set up and group Samsung Wireless Audio Multiroom speakers (e.g. Shape M3 /
WAM351) without the Samsung Multiroom app.

Easy mode
---------
Just run it with no arguments (or double-click it on Windows):

    python samsung_wam_wifi.py

A menu lets you connect a speaker to Wi-Fi, show speakers and groups, group
speakers or ungroup them. If your speakers live on another network than this
PC (e.g. an IoT VLAN), it asks for that network once and remembers it in
samsung_wam_config.json next to this script.

Wi-Fi setup tells you what to do, finds the speaker's setup Wi-Fi, connects this PC to
it, asks which home network the speaker should join, sends the setup command,
disconnects again and then looks for the speaker on your home network.
Automatic hotspot connection needs Windows with a Wi-Fi adapter. Keep the PC's
Ethernet cable plugged in so the PC stays online meanwhile.

How it works
------------
Wi-Fi setup uses three speaker commands:

  GetApList                        list the networks the speaker can see
  SetApManual ssid/password/sec    join a network by name
  SetAp apindex/password           join a network from GetApList by its index

Commands are plain HTTP GETs on TCP port 55001, the same transport pywam uses.
In setup mode the speaker opens a Wi-Fi network whose name contains
"[Samsung]" (or "Samsung Wireless Audio"), with the password 1234567890.
Only the Python standard library is needed.

Grouping uses the speakers' multiroom commands: the main speaker (the one playing the
source) gets SetMultispkGroup type=main listing every member's IP and MAC, each
other member gets type=sub pointing at the main speaker, and SetUngroup leaves
or dissolves a group.

Expert commands
---------------
  python samsung_wam_wifi.py find                  speakers on this PC's networks
  python samsung_wam_wifi.py groups                speakers and their groups
  python samsung_wam_wifi.py group <main-ip> <ip> [<ip> ...] [--name "Downstairs"]
  python samsung_wam_wifi.py ungroup <ip>          main ip: dissolve; other: leave
  python samsung_wam_wifi.py info  <speaker-ip>    name, firmware, current network
  python samsung_wam_wifi.py scan  <speaker-ip>    networks the speaker can see
  python samsung_wam_wifi.py join  <speaker-ip> --ssid "My Wi-Fi"
  python samsung_wam_wifi.py join  <speaker-ip> --index 3      (index from scan)
  python samsung_wam_wifi.py join  <speaker-ip> --ssid "My Wi-Fi" --dry-run

find, groups, group and ungroup accept --subnet 192.168.10.0/24 (repeatable)
to search networks this PC isn't directly on.

The Wi-Fi password is always asked interactively, so it never lands in your
shell history.
"""
from __future__ import annotations

import argparse
import ctypes
import getpass
import ipaddress
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
import urllib.parse
import uuid
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

PORT = 55001
IS_WINDOWS = os.name == "nt"
HOTSPOT_PASSWORD = "1234567890"  # the speakers' setup hotspot password
TEMP_PROFILE = "WAM-setup-temp"   # Windows Wi-Fi profile we add and remove again

# Security labels as the speaker expects them in SetApManual.
SECURITY = {
    "wpa": "WPA/WPA2/FT PSK",  # normal home networks (WPA2-Personal)
    "wep": "WEP",
    "none": "None",
    "eap": "802.1x EAP",
}

# Speaker command templates.
CMD_GET_AP_INFO = "<name>GetApInfo</name>"
CMD_GET_AP_LIST = "<name>GetApList</name>"
CMD_GET_SPK_NAME = "<name>GetSpkName</name>"
CMD_GET_SW_VERSION = "<name>GetSoftwareVersion</name>"
CMD_SET_AP_MANUAL = (
    '<name>SetApManual</name>'
    '<p type="cdata" name="ssid" val="empty"><![CDATA[{ssid}]]></p>'
    '<p type="str" name="password" val="{password}"/>'
    '<p type="str" name="security" val="{security}"/>'
)
CMD_SET_AP_INDEX_PW = (
    '<name>SetAp</name>'
    '<p type="dec" name="apindex" val="{index}"/>'
    '<p type="str" name="password" val="{password}"/>'
)
CMD_SET_AP_INDEX = '<name>SetAp</name><p type="dec" name="apindex" val="{index}"/>'

MOBILE_UUID = str(uuid.uuid4())
BLOCK_RE = re.compile(rb"<(UIC|CPM)>.*?</\1>", re.S)


# --------------------------------------------------------------------------
# Speaker protocol
# --------------------------------------------------------------------------

def build_request(ip: str, cmd: str, port: int = PORT) -> bytes:
    path = f"/UIC?cmd={urllib.parse.quote(cmd)}"
    return (
        f"GET {path} HTTP/1.1\r\n"
        f"Host: {ip}:{port}\r\n"
        f"mobileUUID: {MOBILE_UUID}\r\n"
        "mobileName: Wireless Audio\r\n"
        "mobileVersion: 1.0\r\n"
        "\r\n"
    ).encode()


def send(ip: str, cmd: str, port: int = PORT, wait: float = 5.0) -> list[bytes]:
    """Send one command and return the <UIC>/<CPM> blocks received.

    The speaker keeps the socket open and may push unrelated events, so this
    reads until the reply to our command (or `wait` seconds) instead of EOF.
    """
    name = re.search(r"<name>(\w+)</name>", cmd).group(1)
    # Replies usually name the method without its Get/Set prefix.
    hints = {name, re.sub(r"^(Get|Set)", "", name)}
    reply_re = re.compile(rb"<method>(" + "|".join(hints).encode() + rb")</method>")
    data = b""
    with socket.create_connection((ip, port), timeout=5) as sock:
        sock.sendall(build_request(ip, cmd, port))
        sock.settimeout(0.5)
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:
            try:
                chunk = sock.recv(65536)
            except socket.timeout:
                continue
            if not chunk:
                break
            data += chunk
            if any(reply_re.search(m.group(0)) for m in BLOCK_RE.finditer(data)):
                break
    blocks = [m.group(0) for m in BLOCK_RE.finditer(data)]
    replies = [b for b in blocks if reply_re.search(b)]
    return replies or blocks or ([data] if data else [])


def parse(blocks: list[bytes]) -> list[ET.Element]:
    roots = []
    for raw in blocks:
        try:
            roots.append(ET.fromstring(raw))
        except ET.ParseError:
            pass
    return roots


def response_fields(blocks: list[bytes]) -> dict[str, str]:
    """Flat {tag: text} of the first reply's <response> children."""
    for root in parse(blocks):
        resp = root.find("response")
        if resp is not None:
            fields = {"result": resp.get("result", "")}
            fields.update({c.tag: (c.text or "").strip() for c in resp if not len(c)})
            return fields
    return {}


def ap_list(blocks: list[bytes]) -> list[dict[str, str]]:
    rows = []
    for root in parse(blocks):
        for ap in root.iter("ap"):
            row = dict(ap.attrib)
            row.update({c.tag: (c.text or "").strip() for c in ap})
            rows.append(row)

    def strength(r: dict[str, str]) -> int:
        try:
            return -int(r.get("rssi", ""))
        except ValueError:
            return 0
    return sorted(rows, key=strength)


def show(blocks: list[bytes]) -> None:
    if not blocks:
        print("  (no reply)")
        return
    for raw in blocks:
        try:
            root = ET.fromstring(raw)
        except ET.ParseError:
            print("  " + raw.decode("utf-8", "replace"))
            continue
        method = root.findtext("method", "?")
        resp = root.find("response")
        result = resp.get("result", "?") if resp is not None else "?"
        print(f"  {method}: result={result}")
        if resp is not None:
            for child in resp:
                if not len(child):
                    print(f"    {child.tag} = {(child.text or '').strip()}")


def speaker_name(ip: str, port: int = PORT, wait: float = 3.0) -> str | None:
    """Speaker name if `ip` answers like a Samsung speaker, else None."""
    try:
        fields = response_fields(send(ip, CMD_GET_SPK_NAME, port, wait=wait))
    except OSError:
        return None
    return fields.get("spkname") if fields.get("result") == "ok" else None


def join_command(password: str, ssid: str | None = None, index: int | None = None,
                 security: str = "wpa") -> str:
    if ssid is not None:
        return CMD_SET_AP_MANUAL.format(ssid=ssid, password=password,
                                        security=SECURITY[security])
    if password:
        return CMD_SET_AP_INDEX_PW.format(index=index, password=password)
    return CMD_SET_AP_INDEX.format(index=index)


def password_warnings(password: str) -> list[str]:
    return [f"The password contains {ch!r}. Passwords are sent unescaped, so the "
            "speaker may read it wrongly." for ch in '"<&' if ch in password]


# --------------------------------------------------------------------------
# Finding speakers on this PC's networks
# --------------------------------------------------------------------------

def local_subnets() -> list[ipaddress.IPv4Network]:
    """This PC's IPv4 networks, each capped at /24 so a sweep stays quick."""
    addrs: list[str] = []
    if IS_WINDOWS:
        out = powershell(
            "Get-NetIPAddress -AddressFamily IPv4 | Where-Object { "
            "$_.PrefixOrigin -ne 'WellKnown' -and $_.IPAddress -notlike '169.254.*' } | "
            "ForEach-Object { \"$($_.IPAddress)/$($_.PrefixLength)\" }")
        addrs = [a.strip() for a in out.splitlines() if a.strip()]
    if not addrs:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.connect(("192.0.2.1", 9))  # no packet is sent for UDP connect
                addrs = [s.getsockname()[0] + "/24"]
        except OSError:
            return []
    nets = []
    for a in addrs:
        iface = ipaddress.ip_interface(a)
        if iface.network.prefixlen < 24:
            iface = ipaddress.ip_interface(f"{iface.ip}/24")
        if iface.network not in nets and not iface.ip.is_loopback:
            nets.append(iface.network)
    return nets


def probe(hosts: list[str], port: int = PORT, timeout: float = 0.6) -> list[str]:
    """Hosts that accept a TCP connection on `port`."""
    def is_open(host: str) -> str | None:
        with socket.socket() as s:
            s.settimeout(timeout)
            return host if s.connect_ex((host, port)) == 0 else None
    with ThreadPoolExecutor(max_workers=128) as pool:
        return [h for h in pool.map(is_open, hosts) if h]


def find_speakers(nets: list[ipaddress.IPv4Network] | None = None,
                  port: int = PORT) -> list[tuple[str, str]]:
    """(ip, name) of every Samsung speaker answering on the given networks."""
    hosts = list(dict.fromkeys(
        str(h) for n in (nets if nets is not None else local_subnets()) for h in n.hosts()))
    found = []
    for host in probe(hosts, port):
        name = speaker_name(host, port)
        if name is not None:  # filters out e.g. Samsung TVs that also use 55001
            found.append((host, name))
    return found


# --------------------------------------------------------------------------
# Windows Wi-Fi helpers (netsh + PowerShell)
# --------------------------------------------------------------------------

def powershell(command: str) -> str:
    full = "[Console]::OutputEncoding=[Text.Encoding]::UTF8; " + command
    r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", full],
                       capture_output=True, encoding="utf-8", errors="replace")
    return r.stdout.strip()


def netsh(args: str) -> subprocess.CompletedProcess:
    # A plain string keeps netsh's own quoting rules (interface="Wi-Fi 2").
    return subprocess.run("netsh wlan " + args, capture_output=True,
                          encoding="oem" if IS_WINDOWS else "utf-8", errors="replace")


def wifi_interface() -> str:
    return powershell(
        "Get-NetAdapter -Physical | Where-Object PhysicalMediaType -eq 'Native 802.11' | "
        "Select-Object -First 1 -ExpandProperty Name")


def wifi_address(iface: str) -> tuple[str, str]:
    """(gateway, ip/prefix) of the Wi-Fi adapter; empty strings if none yet."""
    out = powershell(
        f"$c = Get-NetIPConfiguration -InterfaceAlias '{iface}'; "
        "\"$($c.IPv4DefaultGateway.NextHop)|$($c.IPv4Address.IPAddress)/$($c.IPv4Address.PrefixLength)\"")
    gateway, _, address = out.partition("|")
    if address.startswith("/") or address.startswith("169.254."):
        address = ""
    return gateway.strip(), address.strip()


def trigger_scan() -> None:
    """Ask Windows to rescan for Wi-Fi networks right away (best effort)."""
    if not IS_WINDOWS:
        return
    try:
        from ctypes import wintypes
        wlan = ctypes.WinDLL("wlanapi.dll")
    except (OSError, AttributeError, ImportError):
        return
    handle, negotiated = wintypes.HANDLE(), wintypes.DWORD()
    if wlan.WlanOpenHandle(2, None, ctypes.byref(negotiated), ctypes.byref(handle)):
        return
    try:
        info = ctypes.c_void_p()
        if wlan.WlanEnumInterfaces(handle, None, ctypes.byref(info)):
            return
        try:
            count = ctypes.c_uint32.from_address(info.value).value
            entry = 16 + 256 * 2 + 4  # WLAN_INTERFACE_INFO: GUID, WCHAR[256], state
            for i in range(count):
                guid = (ctypes.c_ubyte * 16).from_address(info.value + 8 + i * entry)
                wlan.WlanScan(handle, ctypes.byref(guid), None, None, None)
        finally:
            wlan.WlanFreeMemory(info)
    finally:
        wlan.WlanCloseHandle(handle, None)


def visible_networks(iface: str) -> tuple[list[dict[str, str]], str]:
    """Networks Windows currently sees: ([{ssid, auth}], raw netsh output)."""
    out = netsh(f'show networks interface="{iface}"').stdout
    nets: list[dict[str, str]] = []
    for line in out.splitlines():
        m = re.match(r"^SSID \d+ : (.*)$", line)
        if m:
            nets.append({"ssid": m.group(1).rstrip(), "auth": ""})
            continue
        m = re.match(r"^\s+Authentication\s*: (.*)$", line)
        if m and nets:
            nets[-1]["auth"] = m.group(1).strip()
    return nets, out


def is_speaker_hotspot(ssid: str) -> bool:
    """Whether a Wi-Fi network name is a speaker's setup hotspot."""
    if ssid.startswith("DIRECT"):
        return "Samsung Audio Setup" in ssid
    return "[Samsung]" in ssid or "Samsung Wireless Audio" in ssid


def profile_xml(ssid: str, auth: str) -> str:
    a = auth.upper()
    if "OPEN" in a:
        security = ("open", "none")
    elif "WPA2" in a or "WPA3" in a or not a:
        security = ("WPA2PSK", "AES")
    elif "WPA" in a:
        security = ("WPAPSK", "TKIP")
    else:
        security = ("WPA2PSK", "AES")
    key = "" if security[0] == "open" else (
        "<sharedKey><keyType>passPhrase</keyType><protected>false</protected>"
        f"<keyMaterial>{HOTSPOT_PASSWORD}</keyMaterial></sharedKey>")
    return (
        '<?xml version="1.0"?>\n'
        '<WLANProfile xmlns="http://www.microsoft.com/networking/WLAN/profile/v1">'
        f"<name>{TEMP_PROFILE}</name>"
        f"<SSIDConfig><SSID><hex>{ssid.encode('utf-8').hex().upper()}</hex></SSID></SSIDConfig>"
        "<connectionType>ESS</connectionType><connectionMode>manual</connectionMode>"
        "<MSM><security><authEncryption>"
        f"<authentication>{security[0]}</authentication><encryption>{security[1]}</encryption>"
        f"<useOneX>false</useOneX></authEncryption>{key}</security></MSM>"
        "</WLANProfile>")


def current_profile(iface: str) -> str:
    """Name of the Wi-Fi profile the adapter is connected to, if any."""
    out = netsh("show interfaces").stdout
    block = ""
    for chunk in re.split(r"\n\s*\n", out):
        if re.search(rf"^\s+Name\s*: {re.escape(iface)}\s*$", chunk, re.M):
            block = chunk
    if not re.search(r"^\s+State\s*: connected", block, re.M):
        return ""
    m = re.search(r"^\s+Profile\s*: (.*)$", block, re.M)
    return m.group(1).strip() if m else ""


def connect_hotspot(iface: str, ssid: str, auth: str) -> None:
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "wam-profile.xml")
        with open(path, "w", encoding="utf-8") as f:
            f.write(profile_xml(ssid, auth))
        r = netsh(f'add profile filename="{path}" interface="{iface}" user=current')
    if r.returncode:
        raise RuntimeError((r.stdout + r.stderr).strip())
    r = netsh(f'connect name="{TEMP_PROFILE}" interface="{iface}"')
    if r.returncode:
        raise RuntimeError((r.stdout + r.stderr).strip())


def disconnect_hotspot(iface: str, previous: str) -> None:
    netsh(f'disconnect interface="{iface}"')
    netsh(f'delete profile name="{TEMP_PROFILE}" interface="{iface}"')
    if previous and previous != TEMP_PROFILE:
        netsh(f'connect name="{previous}" interface="{iface}"')


# --------------------------------------------------------------------------
# Easy mode
# --------------------------------------------------------------------------

def step(n: int, title: str) -> None:
    print(f"\n[{n}] {title}")


def ask(prompt: str) -> str:
    return input("    " + prompt).strip()


def wait_for_hotspot(iface: str, timeout: float = 90) -> list[dict[str, str]]:
    deadline, next_scan = time.monotonic() + timeout, 0.0
    while time.monotonic() < deadline:
        if time.monotonic() >= next_scan:
            trigger_scan()
            next_scan = time.monotonic() + 10
        nets, raw = visible_networks(iface)
        hits = [n for n in nets if is_speaker_hotspot(n["ssid"])]
        if hits:
            print()
            return hits
        if not nets and "location" in raw.lower():
            print("\n    Windows won't list Wi-Fi networks:\n    " + raw.strip())
            return []
        print(".", end="", flush=True)
        time.sleep(3)
    print()
    return []


def wait_for_speaker_ip(iface: str, port: int, timeout: float = 45) -> str | None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        gateway, address = wifi_address(iface)
        if gateway and speaker_name(gateway, port) is not None:
            return gateway
        if address:  # no gateway handed out: look around the hotspot's subnet
            net = ipaddress.ip_interface(address).network
            if net.prefixlen < 24:
                net = ipaddress.ip_interface(f"{address.split('/')[0]}/24").network
            found = find_speakers([net], port)
            if found:
                return found[0][0]
        print(".", end="", flush=True)
        time.sleep(3)
    return None


def choose_network(ip: str, port: int) -> tuple[str | None, int | None, str]:
    """Ask which network to join. Returns (ssid, index, label)."""
    print("    Asking the speaker which networks it can see (takes a few seconds)...")
    try:
        rows = ap_list(send(ip, CMD_GET_AP_LIST, port, wait=15.0))
    except OSError:
        rows = []
    usable = [r for r in rows if r.get("index", "").isdigit() and (r.get("ssid") or r.get("apname"))]
    if usable:
        print("    Networks the speaker can see:")
        for n, r in enumerate(usable, 1):
            extra = f"  (signal {r['rssi']})" if r.get("rssi") else ""
            print(f"      {n:>2}. {r.get('ssid') or r.get('apname')}{extra}")
        print("    Pick the 2.4 GHz one if your router has separate names.")
        choice = ask("Type its number, or type the network name: ")
    else:
        print("    The speaker didn't send a list, so type the name yourself (case-sensitive).")
        choice = ask("Network name: ")
    if choice.isdigit() and usable and 1 <= int(choice) <= len(usable):
        r = usable[int(choice) - 1]
        return None, int(r["index"]), r.get("ssid") or r.get("apname", "")
    return choice, None, choice


def easy_mode(port: int = PORT) -> int:
    print("Samsung multiroom speaker Wi-Fi setup")
    print("=" * 37)
    print("This connects your speaker to your home Wi-Fi without the Samsung app.\n"
          "It will briefly connect this PC's Wi-Fi to the speaker, so keep the\n"
          "Ethernet cable plugged in if you want the PC to stay online meanwhile.\n"
          "Press Ctrl+C at any time to stop.")

    iface = wifi_interface() if IS_WINDOWS else ""
    if not iface:
        print("\nI can't switch Wi-Fi networks automatically here"
              + (" (no Wi-Fi adapter found)." if IS_WINDOWS else " (only on Windows)."))
        print("Do this yourself instead:\n"
              "  1. Hold the speaker's WPS/Wi-Fi SETUP button for about 5 seconds.\n"
              f"  2. Connect to the Wi-Fi called \"[Samsung] ...\" (password {HOTSPOT_PASSWORD}).\n"
              "  3. Run:  python samsung_wam_wifi.py join <default-gateway-ip> --ssid \"YourWiFi\"")
        return 1

    step(1, "Put the speaker in setup mode")
    print("    Make sure the speaker is plugged in and switched on. Then hold the\n"
          "    WPS/Wi-Fi SETUP button on its back for about 5 seconds, until its\n"
          "    light changes.")
    ask("Press Enter when you've done that... ")

    step(2, "Looking for the speaker's setup Wi-Fi (up to 90 seconds)")
    print("    ", end="", flush=True)
    hits = wait_for_hotspot(iface)
    if not hits:
        nets, _ = visible_networks(iface)
        if nets:
            print("    I didn't recognise the speaker's Wi-Fi by name. Networks I can see:")
            for n, net in enumerate(nets, 1):
                print(f"      {n:>2}. {net['ssid']}  ({net['auth'] or '?'})")
            pick = ask("If one of these is the speaker, type its number (Enter = none): ")
            if pick.isdigit() and 1 <= int(pick) <= len(nets):
                hits = [nets[int(pick) - 1]]
    if not hits:
        print("    I couldn't find the speaker. Try holding the button a bit longer (or\n"
              "    unplug the speaker for 10 seconds first), then run this again. If it\n"
              "    never appears, use WPS or an Ethernet cable instead.")
        return 1
    hotspot = hits[0]
    if len(hits) > 1:
        for n, h in enumerate(hits, 1):
            print(f"      {n}. {h['ssid']}")
        pick = ask("Several speakers found. Which one (number)? ")
        if pick.isdigit() and 1 <= int(pick) <= len(hits):
            hotspot = hits[int(pick) - 1]
    print(f"    Found: {hotspot['ssid']}")

    previous = current_profile(iface)
    name, target = None, ""
    try:
        step(3, f"Connecting this PC's Wi-Fi ({iface}) to the speaker")
        connect_hotspot(iface, hotspot["ssid"], hotspot["auth"])
        print("    ", end="", flush=True)
        ip = wait_for_speaker_ip(iface, port)
        print()
        if not ip:
            print("    Connected, but the speaker didn't answer. Run this again, or\n"
                  "    check Windows' Wi-Fi status for the speaker network.")
            return 1
        name = speaker_name(ip, port) or "the speaker"
        print(f"    Talking to \"{name}\" at {ip}.")

        step(4, "Choose your home Wi-Fi")
        ssid, index, target = choose_network(ip, port)
        if not target:
            print("    No network chosen, stopping.")
            return 1
        password = getpass.getpass("    Wi-Fi password (not shown, leave empty if none): ")
        for w in password_warnings(password):
            print("    Warning: " + w)
        if ssid is not None and "]]>" in ssid:
            print("    That network name can't be used.")
            return 1
        security = "wpa" if password else "none"
        if ask(f"Send \"{target}\" to {name}? [Y/n] ").lower() in ("n", "no"):
            print("    Cancelled, nothing was sent.")
            return 1

        step(5, "Sending your Wi-Fi details to the speaker")
        try:
            fields = response_fields(send(ip, join_command(password, ssid, index, security),
                                          port, wait=10.0))
            print(f"    Speaker replied: {fields.get('result') or 'nothing (normal, it is switching networks)'}")
        except OSError:
            print("    The speaker dropped the connection (normal, it is switching networks).")
    finally:
        print("\n    Disconnecting this PC from the speaker's Wi-Fi and tidying up...")
        disconnect_hotspot(iface, previous)

    step(6, f"Waiting for {name} to show up on your home network (up to 3 minutes)")
    print("    ", end="", flush=True)
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        time.sleep(10)
        found = find_speakers(port=port)
        mine = [f for f in found if f[1] == name] or found
        if mine:
            ip, spk = mine[0]
            info = response_fields(send(ip, CMD_GET_AP_INFO, port))
            print(f"\n\n    Done! \"{spk}\" is at {ip}"
                  + (f", on Wi-Fi \"{info['ssid']}\"." if info.get("ssid") else "."))
            print("    Now open Spotify, tap the Devices icon and pick the speaker.")
            return 0
        print(".", end="", flush=True)
    print(f"\n\n    I can't see the speaker on this PC's network. If its light has stopped\n"
          f"    blinking it probably worked: \"{target}\" may be a separate network\n"
          f"    (an IoT or guest network/VLAN) that this PC doesn't search. Find the\n"
          f"    speaker in your router's device list and check it with\n"
          f"        python samsung_wam_wifi.py info <its-ip>\n"
          f"    or search that network with\n"
          f"        python samsung_wam_wifi.py find --subnet 192.168.10.0/24\n"
          f"    If the light still blinks, the name or password was probably wrong:\n"
          f"    run this again and pick the network from the list.")
    return 1


# --------------------------------------------------------------------------
# Grouping
# --------------------------------------------------------------------------

CMD_GET_MAIN_INFO = "<name>GetMainInfo</name>"
CMD_GET_GROUP_NAME = "<name>GetGroupName</name>"
CMD_UNGROUP = "<name>SetUngroup</name>"
CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "samsung_wam_config.json")


class GroupError(Exception):
    pass


@dataclass
class Spk:
    ip: str
    name: str
    mac: str
    role: str     # "main", "sub" or "" when not grouped
    main_ip: str  # for a sub: its main speaker's IP
    group: str    # group name, known for main speakers
    model: str


def group_main_command(name: str, mac: str, main_name: str, subs: list[tuple[str, str]]) -> str:
    """For the main speaker: every member's (ip, mac)."""
    cmd = ('<pwron>on</pwron><name>SetMultispkGroup</name>'
           f'<p type="cdata" name="name" val="empty"><![CDATA[{name}]]></p>'
           '<p type="dec" name="index" val="1"/><p type="str" name="type" val="main"/>'
           f'<p type="dec" name="spknum" val="{len(subs) + 1}"/>'
           f'<p type="str" name="audiosourcemacaddr" val="{mac}"/>'
           f'<p type="cdata" name="audiosourcename" val="empty"><![CDATA[{main_name}]]></p>'
           '<p type="str" name="audiosourcetype" val="speaker"/>')
    for ip, sub_mac in subs:
        cmd += (f'<p type="str" name="subspkip" val="{ip}"/>'
                f'<p type="str" name="subspkmacaddr" val="{sub_mac}"/>')
    return cmd


def group_sub_command(name: str, total: int, main_ip: str, main_mac: str) -> str:
    """For each other member: who the main speaker is."""
    return ('<pwron>on</pwron><name>SetMultispkGroup</name>'
            f'<p type="cdata" name="name" val="empty"><![CDATA[{name}]]></p>'
            '<p type="dec" name="index" val="1"/><p type="str" name="type" val="sub"/>'
            f'<p type="dec" name="spknum" val="{total}"/>'
            f'<p type="str" name="mainspkip" val="{main_ip}"/>'
            f'<p type="str" name="mainspkmacaddr" val="{main_mac}"/>')


def speaker_state(ip: str, port: int = PORT) -> Spk | None:
    try:
        info = response_fields(send(ip, CMD_GET_MAIN_INFO, port))
        if info.get("result") != "ok":
            return None
        role = {"M": "main", "S": "sub"}.get(info.get("grouptype", ""), "")
        group = ""
        if role == "main":
            group = response_fields(send(ip, CMD_GET_GROUP_NAME, port)).get("groupname", "")
    except OSError:
        return None
    return Spk(ip=ip, name=speaker_name(ip, port) or ip, mac=info.get("spkmacaddr", ""),
               role=role, main_ip=info.get("groupmainip", "") if role == "sub" else "",
               group=group, model=info.get("spkmodelname", ""))


def all_speakers(nets: list[ipaddress.IPv4Network], port: int = PORT) -> list[Spk]:
    hosts = list(dict.fromkeys(str(h) for n in nets for h in n.hosts()))
    with ThreadPoolExecutor(max_workers=16) as pool:
        states = pool.map(lambda h: speaker_state(h, port), probe(hosts, port))
    return sorted((s for s in states if s), key=lambda s: (s.name.lower(), s.ip))


def networks(extra: list[str], around: list[str] = ()) -> list[ipaddress.IPv4Network]:
    """--subnet values (or saved ones and this PC's), plus the /24 around given IPs."""
    wanted = list(extra) or [*load_subnets(), *map(str, local_subnets())]
    nets: list[ipaddress.IPv4Network] = []
    for s in [*wanted, *(f"{ip}/24" for ip in around)]:
        n = ipaddress.ip_network(s, strict=False)
        if n.prefixlen < 24:
            n = ipaddress.ip_network(f"{n.network_address}/24")
        if n not in nets:
            nets.append(n)
    return nets


def load_config() -> dict:
    """{"subnets": [...], "speakers": [{"ip", "name", "mac", "model"}, ...]}"""
    try:
        with open(CONFIG_FILE, encoding="utf-8") as f:
            cfg = json.load(f)
    except (OSError, ValueError):
        cfg = {}
    if not isinstance(cfg, dict):
        cfg = {}
    cfg.setdefault("subnets", [])
    cfg.setdefault("speakers", [])
    return cfg


def save_config(cfg: dict) -> None:
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)


def load_subnets() -> list[str]:
    return list(load_config()["subnets"])


def save_subnet(subnet: str) -> None:
    cfg = load_config()
    if subnet not in cfg["subnets"]:
        cfg["subnets"].append(subnet)
        save_config(cfg)


def known_speakers() -> list[dict]:
    return [s for s in load_config()["speakers"] if isinstance(s, dict) and s.get("ip")]


def remember_speakers(spks: list[Spk]) -> None:
    """Add or update speakers in the config, keyed by MAC (falls back to IP)."""
    cfg = load_config()
    saved = cfg["speakers"]
    for s in spks:
        entry = {"ip": s.ip, "name": s.name, "mac": s.mac, "model": s.model}
        for i, old in enumerate(saved):
            if (s.mac and old.get("mac") == s.mac) or old.get("ip") == s.ip:
                saved[i] = entry
                break
        else:
            saved.append(entry)
    # If an IP now belongs to a different speaker, keep only the newest entry.
    by_ip: dict[str, dict] = {}
    for entry in saved:
        by_ip[entry["ip"]] = entry
    cfg["speakers"] = list(by_ip.values())
    save_config(cfg)


def forget_speaker(ip: str) -> None:
    cfg = load_config()
    cfg["speakers"] = [s for s in cfg["speakers"] if s.get("ip") != ip]
    save_config(cfg)


def choose_speakers(prompt: str, many: bool = False, skip: str = "",
                    port: int = PORT) -> list[str]:
    """Show remembered speakers (finding them first if needed) and let the user pick."""
    speakers = known_speakers()
    if not speakers:
        print("No remembered speakers yet, looking for them...")
        remember_speakers(all_speakers(networks([]), port))
        speakers = known_speakers()
    skip_ip = resolve(skip) if skip else ""
    speakers = [s for s in speakers if s["ip"] != skip_ip]
    if not speakers:
        raise GroupError("No speakers to choose from. If they're on another network, run "
                         "`find --subnet 192.168.10.0/24` first.")
    for n, s in enumerate(speakers, 1):
        print(f"  {n:>2}. {s.get('name', '?')}  {s['ip']}")
    chosen = pick(prompt, len(speakers), many)
    if not chosen:
        raise GroupError("Nothing chosen.")
    return [speakers[i]["ip"] for i in chosen]


def resolve(target: str) -> str:
    """An IP as-is, or the IP of a remembered speaker whose name matches."""
    try:
        ipaddress.ip_address(target)
        return target
    except ValueError:
        pass
    speakers = known_speakers()
    exact = [s for s in speakers if s.get("name", "").lower() == target.lower()]
    partial = [s for s in speakers if target.lower() in s.get("name", "").lower()]
    matches = exact or partial
    if len(matches) == 1:
        return matches[0]["ip"]
    if not matches:
        raise GroupError(f'No remembered speaker called "{target}". Run `find` or use its IP.')
    names = ", ".join(f'{m["name"]} ({m["ip"]})' for m in matches)
    raise GroupError(f'"{target}" matches several speakers: {names}')


def describe(spks: list[Spk]) -> list[str]:
    lines, ips = [], {s.ip for s in spks}
    for m in (s for s in spks if s.role == "main"):
        subs = [s for s in spks if s.role == "sub" and s.main_ip == m.ip]
        lines.append(f'Group "{m.group or "?"}" ({len(subs) + 1} speakers)')
        lines.append(f"    {m.name}  {m.ip}  (main: plays the music)")
        lines += [f"    {s.name}  {s.ip}" for s in subs]
    for s in spks:
        if s.role == "sub" and s.main_ip not in ips:
            lines.append(f"{s.name}  {s.ip}  (grouped under {s.main_ip}, which wasn't found)")
    lines += [f"{s.name}  {s.ip}  (not grouped)" for s in spks if not s.role]
    return lines


def reply(ip: str, cmd: str, port: int) -> str:
    try:
        return response_fields(send(ip, cmd, port, wait=10.0)).get("result") or "no reply"
    except OSError as exc:
        return f"error: {exc}"


def make_group(main_ip: str, sub_ips: list[str], name: str | None, port: int,
               nets: list[ipaddress.IPv4Network]) -> list[str]:
    """Create the group, or replace the members of the group main_ip already leads."""
    sub_ips = [ip for ip in dict.fromkeys(sub_ips) if ip != main_ip]
    if not sub_ips:
        raise GroupError("A group needs at least two speakers.")
    states: dict[str, Spk] = {}
    for ip in [main_ip, *sub_ips]:
        s = speaker_state(ip, port)
        if s is None:
            raise GroupError(f"No speaker answers at {ip}.")
        if not s.mac:
            raise GroupError(f"{s.name} ({ip}) didn't report its MAC address.")
        states[ip] = s
    main = states[main_ip]
    if main.role == "sub":
        raise GroupError(f"{main.name} ({main_ip}) is in another group. Ungroup it first.")
    for ip in sub_ips:
        s = states[ip]
        if s.role == "main" or (s.role == "sub" and s.main_ip != main_ip):
            raise GroupError(f"{s.name} ({ip}) is in another group. Ungroup it first.")

    log = []
    if main.role == "main":  # members leaving this group
        for old in all_speakers(nets, port):
            if old.role == "sub" and old.main_ip == main_ip and old.ip not in sub_ips:
                log.append(f"{old.name} leaves the group: {reply(old.ip, CMD_UNGROUP, port)}")
    name = name or main.group or f"{main.name} group"
    subs = [(ip, states[ip].mac) for ip in sub_ips]
    log.append(f"{main.name} becomes main of \"{name}\": "
               + reply(main_ip, group_main_command(name, main.mac, main.name, subs), port))
    for ip in sub_ips:
        log.append(f"{states[ip].name} joins: "
                   + reply(ip, group_sub_command(name, len(subs) + 1, main_ip, main.mac), port))
    return log


def ungroup(ip: str, port: int, nets: list[ipaddress.IPv4Network]) -> list[str]:
    """Main speaker: dissolve its group. Any other member: take it out of its group."""
    s = speaker_state(ip, port)
    if s is None:
        raise GroupError(f"No speaker answers at {ip}.")
    if not s.role:
        return [f"{s.name} isn't grouped."]
    if s.role == "main":
        log = [f"{x.name} leaves: {reply(x.ip, CMD_UNGROUP, port)}"
               for x in all_speakers(nets, port) if x.role == "sub" and x.main_ip == ip]
        return log + [f"{s.name} ends the group: {reply(ip, CMD_UNGROUP, port)}"]
    log = [f"{s.name} leaves: {reply(ip, CMD_UNGROUP, port)}"]
    main = speaker_state(s.main_ip, port)
    if main is None or main.role != "main":
        return log
    main_net = ipaddress.ip_network(f"{main.ip}/24", strict=False)
    nets = nets if main_net in nets else [*nets, main_net]
    rest = [x.ip for x in all_speakers(nets, port)
            if x.role == "sub" and x.main_ip == main.ip and x.ip != ip]
    if rest:
        return log + make_group(main.ip, rest, main.group, port, nets)
    return log + [f"{main.name} has no members left, ending the group: "
                  f"{reply(main.ip, CMD_UNGROUP, port)}"]


# --------------------------------------------------------------------------
# Menu (no arguments)
# --------------------------------------------------------------------------

def pick(prompt: str, count: int, many: bool = False) -> list[int]:
    """Ask for one or more 1-based numbers; returns 0-based indexes ([] = none)."""
    while True:
        text = ask(prompt).replace(",", " ")
        if not text:
            return []
        parts = text.split()
        if all(p.isdigit() and 1 <= int(p) <= count for p in parts) and (many or len(parts) == 1):
            return [int(p) - 1 for p in dict.fromkeys(parts)]
        print(f"    Type {'numbers' if many else 'a number'} between 1 and {count}.")


def discover(port: int) -> tuple[list[Spk], list[ipaddress.IPv4Network]]:
    nets = networks([])
    spks = []
    if nets:
        print("    Looking for speakers on " + ", ".join(map(str, nets)) + " ...")
        spks = all_speakers(nets, port)
    while not spks:
        print("    No speakers found here. If they're on another network (e.g. an IoT")
        subnet = ask("network), type it like 192.168.10.0/24, or press Enter to stop: ")
        if not subnet:
            return [], nets
        try:
            net = ipaddress.ip_network(subnet, strict=False)
        except ValueError:
            print("    That isn't a network address.")
            continue
        spks = all_speakers([net], port)
        if spks:
            save_subnet(str(net))
            nets.append(net)
            print(f"    Found them. I'll remember {net} for next time.")
    remember_speakers(spks)
    return spks, nets


def list_speakers(spks: list[Spk]) -> None:
    for n, s in enumerate(spks, 1):
        role = {"main": f'main of "{s.group}"', "sub": f"in a group (main {s.main_ip})"}.get(s.role, "")
        print(f"      {n:>2}. {s.name}  {s.ip}" + (f"  [{role}]" if role else ""))


def group_wizard(port: int) -> int:
    spks, nets = discover(port)
    if len(spks) < 2:
        if spks:
            print(f"    Only one speaker found ({spks[0].name}). Grouping needs at least two.")
        return 1
    print("    Speakers:")
    list_speakers(spks)
    main_pick = pick("Which one should be the main speaker (it plays the music)? ", len(spks))
    if not main_pick:
        return 1
    main = spks[main_pick[0]]
    others = pick("Which speakers join it? Numbers separated by spaces: ", len(spks), many=True)
    sub_ips = [spks[i].ip for i in others if spks[i].ip != main.ip]
    if not sub_ips:
        print("    No other speakers chosen, nothing to do.")
        return 1
    default = main.group if main.role == "main" else f"{main.name} group"
    name = ask(f'Group name (Enter = "{default}"): ') or default
    members = ", ".join(s.name for s in spks if s.ip in sub_ips)
    if ask(f'Group {members} with {main.name} as "{name}"? [Y/n] ').lower() in ("n", "no"):
        print("    Cancelled, nothing was changed.")
        return 1
    try:
        for line in make_group(main.ip, sub_ips, name, port, nets):
            print("    " + line)
    except GroupError as exc:
        print(f"    {exc}")
        return 1
    time.sleep(2)
    print("\n    Now:")
    for line in describe(all_speakers(nets, port)):
        print("      " + line)
    return 0


def ungroup_wizard(port: int) -> int:
    spks, nets = discover(port)
    grouped = [s for s in spks if s.role]
    if not grouped:
        if spks:
            print("    None of your speakers are grouped.")
        return 1
    print("    Grouped speakers (pick a main speaker to end its whole group, or another")
    print("    speaker to take just that one out):")
    list_speakers(grouped)
    chosen = pick("Which one? ", len(grouped))
    if not chosen:
        return 1
    try:
        for line in ungroup(grouped[chosen[0]].ip, port, nets):
            print("    " + line)
    except GroupError as exc:
        print(f"    {exc}")
        return 1
    return 0


def menu(port: int) -> int:
    print("Samsung multiroom speaker tool")
    print("=" * 30)
    print("  1. Connect a speaker to Wi-Fi\n"
          "  2. Show speakers and groups\n"
          "  3. Group speakers\n"
          "  4. Ungroup speakers\n"
          "  q. Quit")
    choice = input("Choose: ").strip().lower()
    print()
    if choice == "1":
        return easy_mode(port)
    if choice == "2":
        spks, _ = discover(port)
        for line in describe(spks):
            print("      " + line)
        return 0 if spks else 1
    if choice == "3":
        return group_wizard(port)
    if choice == "4":
        return ungroup_wizard(port)
    return 0


# --------------------------------------------------------------------------
# Expert commands
# --------------------------------------------------------------------------

def cmd_find(args: argparse.Namespace) -> int:
    nets = networks(args.subnet)
    print("Looking on " + ", ".join(map(str, nets)) + " ...")
    spks = all_speakers(nets, args.port)
    for s in spks:
        print(f"  {s.ip}  {s.name}  ({s.model})")
    if not spks:
        print("  No speakers found.")
    remember_speakers(spks)
    return 0


def cmd_speakers(args: argparse.Namespace) -> int:
    speakers = known_speakers()
    for s in speakers:
        print(f"  {s['ip']:<15}  {s.get('name', '')}  ({s.get('model', '')})")
    if not speakers:
        print("  No remembered speakers yet. Run `find` first.")
    return 0


def cmd_groups(args: argparse.Namespace) -> int:
    nets = networks(args.subnet)
    print("Looking on " + ", ".join(map(str, nets)) + " ...")
    spks = all_speakers(nets, args.port)
    for line in describe(spks) or ["No speakers found."]:
        print("  " + line)
    remember_speakers(spks)
    return 0


def cmd_group(args: argparse.Namespace) -> int:
    main_ip, members = resolve(args.main), [resolve(m) for m in args.members]
    nets = networks(args.subnet, around=[main_ip, *members])
    for line in make_group(main_ip, members, args.name, args.port, nets):
        print("  " + line)
    return 0


def cmd_ungroup(args: argparse.Namespace) -> int:
    ip = resolve(args.ip)
    for line in ungroup(ip, args.port, networks(args.subnet, around=[ip])):
        print("  " + line)
    return 0


def cmd_info(args: argparse.Namespace) -> int:
    for cmd in (CMD_GET_SPK_NAME, CMD_GET_SW_VERSION, CMD_GET_AP_INFO):
        show(send(args.ip, cmd, args.port))
    return 0


def cmd_scan(args: argparse.Namespace) -> int:
    blocks = send(args.ip, CMD_GET_AP_LIST, args.port, wait=15.0)
    show(blocks)
    rows = ap_list(blocks)
    if rows:
        print(f"\n  {len(rows)} networks seen by the speaker:")
        for r in rows:
            rest = {k: v for k, v in r.items() if k not in ("index", "ssid", "apname")}
            print(f"    [{r.get('index', '?')}] {(r.get('ssid') or r.get('apname', ''))!r}  {rest}")
    return 0


def cmd_join(args: argparse.Namespace) -> int:
    if (args.ssid is None) == (args.index is None):
        print("Give either --ssid NAME or --index N (from `scan`).", file=sys.stderr)
        return 2
    password = ""
    if args.security != "none":
        password = getpass.getpass("Wi-Fi password (not shown): ")
        if not password:
            print("Empty password; use --security none for an open network.", file=sys.stderr)
            return 2
    for w in password_warnings(password):
        print("Warning: " + w, file=sys.stderr)
    if args.ssid and "]]>" in args.ssid:
        print("The network name can't contain ']]>'.", file=sys.stderr)
        return 2

    cmd = join_command(password, args.ssid, args.index, args.security)
    target = (f"network {args.ssid!r} ({SECURITY[args.security]})" if args.ssid is not None
              else f"network #{args.index} from the speaker's scan list")
    if args.dry_run:
        shown = cmd.replace(f'val="{password}"', 'val="********"') if password else cmd
        print(f"Would send to {args.ip}:{args.port}:\n  {shown}")
        return 0
    if not args.yes:
        answer = input(f"Tell the speaker at {args.ip} to join {target}? [y/N] ")
        if answer.strip().lower() not in ("y", "yes"):
            print("Cancelled.")
            return 1
    try:
        show(send(args.ip, cmd, args.port, wait=10.0))
    except OSError as exc:
        print(f"  Connection ended: {exc}")  # expected if the hotspot drops at once
    print("\nSent. The speaker now tries to join the network; its light should stop\n"
          "blinking within a minute or two. Then run `find` to locate it.")
    return 0


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    argv = sys.argv[1:] if argv is None else argv

    parser = argparse.ArgumentParser(
        description="Wi-Fi setup and grouping for Samsung multiroom speakers without the "
                    "Samsung app. Run with no arguments for the menu.")
    parser.add_argument("--port", type=int, default=PORT, help="speaker port (default 55001)")
    sub = parser.add_subparsers(dest="command")
    nets = argparse.ArgumentParser(add_help=False)
    nets.add_argument("--subnet", action="append", default=[],
                      help="search this network, e.g. 192.168.10.0/24 (repeatable); default: "
                           "remembered networks plus this PC's own")

    sub.add_parser("setup", help="guided Wi-Fi setup")
    sub.add_parser("menu", help="plain text menu (instead of the full-screen UI)")
    sub.add_parser("find", parents=[nets],
                   help="find speakers and remember them").set_defaults(func=cmd_find)
    sub.add_parser("speakers", help="list remembered speakers").set_defaults(func=cmd_speakers)
    sub.add_parser("groups", parents=[nets],
                   help="show speakers and their groups").set_defaults(func=cmd_groups)
    p = sub.add_parser("group", parents=[nets], help="group speakers")
    p.add_argument("main", nargs="?",
                   help="main speaker (plays the music): IP or name; leave out to pick from a list")
    p.add_argument("members", nargs="*", help="speakers that join it: IPs or names")
    p.add_argument("--name", help="group name (default: keep it, or '<main> group')")
    p.set_defaults(func=cmd_group)
    p = sub.add_parser("ungroup", parents=[nets],
                       help="main speaker: end its group; other: take it out of its group")
    p.add_argument("ip", nargs="?", metavar="speaker",
                   help="IP or remembered name; leave out to pick from a list")
    p.set_defaults(func=cmd_ungroup)
    p = sub.add_parser("info", help="show name, firmware and current network")
    p.add_argument("ip", nargs="?", metavar="speaker",
                   help="IP or remembered name; leave out to pick from a list")
    p.set_defaults(func=cmd_info)
    p = sub.add_parser("scan", help="list networks the speaker can see")
    p.add_argument("ip", nargs="?", metavar="speaker",
                   help="IP or remembered name; leave out to pick from a list")
    p.set_defaults(func=cmd_scan)
    p = sub.add_parser("join", help="tell the speaker to join a Wi-Fi network")
    p.add_argument("ip", nargs="?", metavar="speaker",
                   help="IP or remembered name; leave out to pick from a list")
    p.add_argument("--ssid", help="network name (case-sensitive)")
    p.add_argument("--index", type=int, help="network index from `scan`")
    p.add_argument("--security", choices=sorted(SECURITY), default="wpa",
                   help="wpa = WPA/WPA2 Personal (default)")
    p.add_argument("--dry-run", action="store_true", help="print the command, send nothing")
    p.add_argument("-y", "--yes", action="store_true", help="don't ask for confirmation")
    p.set_defaults(func=cmd_join)

    args = parser.parse_args(argv)
    if args.command is None:
        try:
            import wam_tui  # full-screen UI; needs `pip install textual`
        except ImportError:
            print("(Install the full-screen UI with: pip install -r requirements.txt)\n")
        else:
            return wam_tui.run(args.port)
    if args.command in (None, "menu", "setup"):
        try:
            rc = easy_mode(args.port) if args.command == "setup" else menu(args.port)
        except KeyboardInterrupt:
            print("\nStopped.")
            rc = 1
        except RuntimeError as exc:
            print(f"\nWindows refused the Wi-Fi change:\n    {exc}")
            rc = 1
        if args.command is None:  # keep a double-clicked window open
            try:
                input("\nPress Enter to close...")
            except (EOFError, KeyboardInterrupt):
                pass
        return rc
    try:
        if hasattr(args, "ip"):
            args.ip = (resolve(args.ip) if args.ip
                       else choose_speakers("Which speaker? ", port=args.port)[0])
        if args.command == "group":
            if not args.main:
                args.main = choose_speakers("Which one is the main speaker (plays the music)? ",
                                            port=args.port)[0]
            if not args.members:
                args.members = choose_speakers("Which speakers join it? Numbers separated by "
                                               "spaces: ", many=True, skip=args.main, port=args.port)
        return args.func(args)
    except GroupError as exc:
        print(exc, file=sys.stderr)
        return 1
    except (EOFError, KeyboardInterrupt):
        print("\nCancelled.", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"Could not reach the speaker at {getattr(args, 'ip', '?')}:{args.port}: {exc}",
              file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
