"""Samsung's own multiroom groups.

The main speaker (the one that plays the source) gets SetMultispkGroup
type=main listing every member's IP and MAC; each other member gets type=sub
pointing at the main speaker. SetUngroup leaves or dissolves a group.
"""
from __future__ import annotations

from . import protocol


class GroupError(Exception):
    pass


def _by_ip(speakers: list[dict]) -> dict[str, dict]:
    return {s["ip"]: s for s in speakers}


def summary(speakers: list[dict]) -> list[dict]:
    """Groups as [{name, main, members: [speaker...]}] from a sweep result."""
    groups = []
    for m in speakers:
        if m["role"] == "main":
            members = [s for s in speakers if s["role"] == "sub" and s["main_ip"] == m["ip"]]
            groups.append({"name": m["group"] or f"{m['name']} group", "main": m, "members": members})
    return groups


def make_group(main_ip: str, member_ips: list[str], name: str, speakers: list[dict]) -> list[str]:
    known = _by_ip(speakers)
    member_ips = [ip for ip in dict.fromkeys(member_ips) if ip != main_ip]
    if not member_ips:
        raise GroupError("A group needs at least two speakers.")
    for ip in [main_ip, *member_ips]:
        s = known.get(ip)
        if s is None:
            raise GroupError(f"No speaker known at {ip}; search again first.")
        if not s.get("mac"):
            raise GroupError(f"{s['name']} didn't report its MAC address.")
    main = known[main_ip]
    if main["role"] == "sub":
        raise GroupError(f"{main['name']} is in another group. Ungroup it first.")
    for ip in member_ips:
        s = known[ip]
        if s["role"] == "main" or (s["role"] == "sub" and s["main_ip"] != main_ip):
            raise GroupError(f"{s['name']} is in another group. Ungroup it first.")

    log = []
    if main["role"] == "main":  # members leaving an existing group
        for s in speakers:
            if s["role"] == "sub" and s["main_ip"] == main_ip and s["ip"] not in member_ips:
                log.append(f"{s['name']} leaves: {_res(protocol.ungroup, s['ip'])}")
    name = name.strip() or main.get("group") or f"{main['name']} group"
    subs = [(ip, known[ip]["mac"]) for ip in member_ips]
    log.append(f"{main['name']} leads \"{name}\": "
               f"{_res(protocol.group_main, main_ip, name, main['mac'], main['name'], subs)}")
    for ip in member_ips:
        log.append(f"{known[ip]['name']} joins: "
                   f"{_res(protocol.group_sub, ip, name, len(subs) + 1, main_ip, main['mac'])}")
    return log


def ungroup(ip: str, speakers: list[dict]) -> list[str]:
    known = _by_ip(speakers)
    s = known.get(ip)
    if s is None:
        raise GroupError(f"No speaker known at {ip}; search again first.")
    if not s["role"]:
        return [f"{s['name']} isn't grouped."]
    if s["role"] == "main":
        log = [f"{x['name']} leaves: {_res(protocol.ungroup, x['ip'])}"
               for x in speakers if x["role"] == "sub" and x["main_ip"] == ip]
        return log + [f"{s['name']} ends the group: {_res(protocol.ungroup, ip)}"]
    log = [f"{s['name']} leaves: {_res(protocol.ungroup, ip)}"]
    main = known.get(s["main_ip"])
    if main is None:
        return log
    rest = [x["ip"] for x in speakers if x["role"] == "sub" and x["main_ip"] == main["ip"] and x["ip"] != ip]
    if rest:
        return log + make_group(main["ip"], rest, main.get("group", ""),
                                [x for x in speakers if x["ip"] != ip])
    return log + [f"{main['name']} has no members left, ending the group: "
                  f"{_res(protocol.ungroup, main['ip'])}"]


def _res(fn, *args) -> str:
    try:
        return "ok" if fn(*args) else "refused"
    except protocol.SpeakerError as exc:
        return f"error: {exc}"
