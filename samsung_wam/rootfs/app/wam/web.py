"""Ingress UI + JSON API, and the stream server the speakers pull audio from."""
from __future__ import annotations

import http.server
import json
import mimetypes
import os
import socketserver
import threading
import time
import uuid

from . import discovery, groups, protocol, soloist, state, wifi

STATIC = os.path.join(os.path.dirname(__file__), "..", "static")
INGRESS_PROXY = "172.30.32.2"   # Home Assistant's ingress proxy address


class Jobs:
    """Long operations (scans, Wi-Fi setup, grouping) run in threads; the UI polls."""

    def __init__(self):
        self.jobs: dict[str, dict] = {}
        self.lock = threading.Lock()

    def run(self, kind: str, fn, *args) -> str:
        job = {"id": uuid.uuid4().hex[:10], "kind": kind, "state": "running", "log": [],
               "result": None, "started": time.time()}
        with self.lock:
            self.jobs[job["id"]] = job
            for old in sorted(self.jobs.values(), key=lambda j: j["started"])[:-30]:
                self.jobs.pop(old["id"], None)

        def log(msg: str) -> None:
            job["log"].append(msg)
            print(f"[{kind}] {msg}", flush=True)

        def target():
            try:
                job["result"] = fn(log, *args)
                job["state"] = "done"
            except Exception as exc:  # noqa: BLE001 - shown to the user
                job["state"] = "error"
                log(str(exc))
        threading.Thread(target=target, daemon=True).start()
        return job["id"]

    def get(self, job_id: str) -> dict | None:
        return self.jobs.get(job_id)


class App:
    def __init__(self, manager, dev: bool = False):
        self.m = manager
        self.dev = dev
        self.jobs = Jobs()
        self.speakers: list[dict] = []
        self.last_scan = 0.0
        self.scanning = False
        self.sup = wifi.Supervisor()

    # -- speakers ----------------------------------------------------------
    def networks(self) -> list:
        st = state.load()
        nets = discovery.local_subnets()
        for s in st["subnets"]:
            try:
                n = discovery.parse_subnet(s)
            except ValueError:
                continue
            if n not in nets:
                nets.append(n)
        return nets

    def scan(self, log=print) -> list[dict]:
        self.scanning = True
        try:
            st = state.load()
            nets = self.networks()
            remembered = [s["ip"] for s in st["speakers"].values()]
            log("Searching " + ", ".join(map(str, nets)) + " ...")
            found = discovery.sweep(nets, remembered)
            self.speakers = found
            self.last_scan = time.time()
            self.m.set_speakers(found)

            def remember(st):
                for s in found:
                    if s["mac"]:
                        st["speakers"][s["mac"]] = {"ip": s["ip"], "name": s["name"], "model": s["model"]}
            state.update(remember)
            log(f"Found {len(found)} speaker(s).")
            return found
        finally:
            self.scanning = False

    def refresh_one(self, ip: str) -> dict | None:
        s = discovery.speaker_state(ip)
        if s:
            self.speakers = sorted([x for x in self.speakers if x["ip"] != ip] + [s],
                                   key=lambda x: (x["name"].lower(), x["ip"]))
            self.m.set_speakers(self.speakers)
        return s

    def speaker(self, ip: str) -> dict:
        for s in self.speakers:
            if s["ip"] == ip:
                return s
        raise ValueError(f"No speaker known at {ip}. Search again.")

    # -- status ------------------------------------------------------------
    def status(self) -> dict:
        ok, why = self.m.ready()
        st = state.load()
        return {
            "speakers": self.speakers, "groups": groups.summary(self.speakers),
            "scanning": self.scanning, "last_scan": self.last_scan,
            "subnets": st["subnets"], "local_subnets": [str(n) for n in discovery.local_subnets()],
            "bridges": [b.info() for b in self.m.bridges.values()],
            "spotify": {"ready": ok, "reason": why, "accepted": self.m.accepted,
                        "has_key": bool(self.m.api_key), "soloist": self.m.soloist_info,
                        "error": self.m.soloist_error},
            "dev": self.dev,
        }

    # -- API ---------------------------------------------------------------
    def api(self, method: str, path: str, body: dict) -> tuple[int, dict]:
        parts = [p for p in path.split("/") if p]
        if method == "GET" and parts == ["status"]:
            return 200, self.status()
        if method == "GET" and len(parts) == 2 and parts[0] == "jobs":
            job = self.jobs.get(parts[1])
            return (200, job) if job else (404, {"error": "no such job"})
        if method == "GET" and parts == ["hotspot"]:
            return 200, wifi.host_wifi(self.sup)
        if method != "POST":
            return 404, {"error": "not found"}

        if parts == ["scan"]:
            return 200, {"job": self.jobs.run("scan", self.scan)}
        if parts == ["subnets", "add"]:
            net = str(discovery.parse_subnet(body.get("subnet", "")))
            state.update(lambda st: st["subnets"].append(net) if net not in st["subnets"] else None)
            return 200, {"job": self.jobs.run("scan", self.scan)}
        if parts == ["subnets", "remove"]:
            state.update(lambda st: st.__setitem__("subnets", [s for s in st["subnets"] if s != body.get("subnet")]))
            return 200, {}
        if parts == ["speakers", "add"]:
            ip = body.get("ip", "").strip()

            def add(log):
                s = self.refresh_one(ip)
                if not s:
                    raise RuntimeError(f"No Samsung speaker answers at {ip}.")
                state.update(lambda st: st["speakers"].__setitem__(s["mac"], {"ip": ip, "name": s["name"], "model": s["model"]}))
                log(f"Added {s['name']}.")
                return s
            return 200, {"job": self.jobs.run("add", add)}
        if parts == ["speaker", "volume"]:
            ip, vol = body["ip"], int(body["volume"])
            if not protocol.set_volume(ip, vol):
                return 502, {"error": "The speaker refused the volume change."}
            for s in self.speakers:
                if s["ip"] == ip:
                    s["volume"] = vol
            return 200, {}
        if parts == ["speaker", "networks"]:
            ip = body["ip"]
            return 200, {"job": self.jobs.run("networks", lambda log: protocol.ap_list(ip))}
        if parts == ["speaker", "wifi"]:
            ip, pw = body["ip"], body.get("password", "")
            ssid, index = body.get("ssid"), body.get("index")

            def join(log):
                for p in wifi.password_problems(pw):
                    log("Warning: " + p)
                res = wifi.join(ip, pw, ssid, index)
                log(f"Speaker replied: {res}. It now switches networks; search again in a minute.")
                return res
            return 200, {"job": self.jobs.run("wifi", join)}
        if parts == ["groups", "create"]:
            main, members, name = body["main"], body.get("members", []), body.get("name", "")

            def make(log):
                for line in groups.make_group(main, members, name, self.speakers):
                    log(line)
                time.sleep(2)
                self.scan(lambda _m: None)
            return 200, {"job": self.jobs.run("group", make)}
        if parts == ["groups", "ungroup"]:
            ip = body["ip"]

            def undo(log):
                for line in groups.ungroup(ip, self.speakers):
                    log(line)
                time.sleep(2)
                self.scan(lambda _m: None)
            return 200, {"job": self.jobs.run("ungroup", undo)}
        if parts == ["bridges"]:
            spk = self.speaker(body["speaker_ip"])
            vol = body.get("speaker_volume")
            b = self.m.add(body.get("name", ""), spk, int(vol) if vol not in (None, "") else None)
            return 200, b.info()
        if len(parts) == 3 and parts[0] == "bridges":
            bid, action = parts[1], parts[2]
            if bid not in self.m.bridges:
                return 404, {"error": "no such bridge"}
            if action == "pair":
                self.m.pair(bid)
            elif action == "restart":
                self.m.restart(bid)
            elif action == "remove":
                self.m.remove(bid)
            elif action == "update":
                changes = {}
                if "speaker_volume" in body:
                    v = body["speaker_volume"]
                    changes["speaker_volume"] = int(v) if v not in (None, "") else None
                if body.get("name"):
                    changes["name"] = body["name"].strip()
                self.m.update(bid, changes)
                if "name" in changes:
                    self.m.restart(bid)
            else:
                return 404, {"error": "unknown action"}
            return 200, {}
        if parts == ["soloist", "update"]:
            def upd(log):
                info = soloist.download(log)
                self.m.soloist_info = info
                self.m.soloist_error = ""
                self.m.refresh_soloist_and_restart()
                return info
            return 200, {"job": self.jobs.run("soloist", upd)}
        if parts == ["hotspot", "find"]:
            iface = body.get("interface") or wifi.host_wifi(self.sup).get("interface")
            return 200, {"job": self.jobs.run("hotspots", lambda log: wifi.find_hotspots(self.sup, iface))}
        if parts == ["hotspot", "setup"]:
            iface = body.get("interface") or wifi.host_wifi(self.sup).get("interface")
            args = (self.sup, iface, body["hotspot"], body.get("password", ""), body.get("ssid"), body.get("index"))
            return 200, {"job": self.jobs.run("hotspot", lambda log: wifi.hotspot_setup(*args, log))}
        return 404, {"error": "not found"}


def _server(port: int, handler_cls):
    class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
        daemon_threads = True
        allow_reuse_address = True
    srv = Server(("0.0.0.0", port), handler_cls)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def ui_server(app: App, port: int):
    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _allowed(self) -> bool:
            if app.dev or self.client_address[0] in (INGRESS_PROXY, "127.0.0.1"):
                return True
            self.send_error(403, "Open this through Home Assistant")
            return False

        def _json(self, code: int, data) -> None:
            raw = json.dumps(data).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def _handle(self, method: str) -> None:
            if not self._allowed():
                return
            path = self.path.split("?", 1)[0]
            if path.startswith("/api/"):
                body = {}
                if method == "POST":
                    n = int(self.headers.get("Content-Length") or 0)
                    try:
                        body = json.loads(self.rfile.read(n) or b"{}")
                    except ValueError:
                        return self._json(400, {"error": "bad JSON"})
                try:
                    code, data = app.api(method, path[5:], body)
                except (KeyError, ValueError, RuntimeError, protocol.SpeakerError) as exc:
                    code, data = 400, {"error": str(exc) or exc.__class__.__name__}
                return self._json(code, data)
            if method != "GET":
                return self._json(405, {"error": "method not allowed"})
            name = "index.html" if path in ("", "/") else path.lstrip("/")
            full = os.path.normpath(os.path.join(STATIC, name))
            if not full.startswith(os.path.normpath(STATIC)) or not os.path.isfile(full):
                self.send_error(404)
                return
            with open(full, "rb") as f:
                raw = f.read()
            self.send_response(200)
            self.send_header("Content-Type", mimetypes.guess_type(full)[0] or "application/octet-stream")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self):
            self._handle("GET")

        def do_POST(self):
            self._handle("POST")
    return _server(port, Handler)


def stream_server(manager, port: int):
    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.0"  # close-delimited body, no Content-Length

        def log_message(self, *a):
            pass

        def do_GET(self):
            path = self.path.split("?", 1)[0]
            if path.startswith("/stream/") and path.endswith(".wav"):
                b = manager.stream(path[len("/stream/"):-len(".wav")])
                if b:
                    return b.serve(self)
            self.send_response(404)
            self.end_headers()
    return _server(port, Handler)
