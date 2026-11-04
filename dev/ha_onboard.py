"""Finish first-run onboarding of the throwaway dev Home Assistant (localhost:7123)
with a generated test account, and check the app's ingress panel end to end.

The login is written to dev-data/ha-dev-login.json (git-ignored) and never printed.
Usage: python dev/ha_onboard.py
"""
import json
import os
import secrets
import sys
import urllib.error
import urllib.parse
import urllib.request

HA = "http://localhost:7123"
CLIENT = HA + "/"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOGIN = os.path.join(ROOT, "dev-data", "ha-dev-login.json")
SLUG = "local_samsung_wam"


def call(method, path, body=None, token=None, form=False, cookie=None, raw=False):
    data = None
    headers = {}
    if body is not None:
        data = (urllib.parse.urlencode(body) if form else json.dumps(body)).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded" if form else "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if cookie:
        headers["Cookie"] = cookie
    req = urllib.request.Request(HA + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            text = r.read().decode("utf-8", "replace")
            return r.status, (text if raw else (json.loads(text) if text else {}))
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")[:300]


class WS:
    """Tiny WebSocket client for HA's /api/websocket (stdlib only)."""

    def __init__(self, token):
        import base64, socket
        self.sock = socket.create_connection(("localhost", int(HA.rsplit(":", 1)[1])), timeout=30)
        key = base64.b64encode(os.urandom(16)).decode()
        self.sock.sendall((f"GET /api/websocket HTTP/1.1\r\nHost: localhost\r\nUpgrade: websocket\r\n"
                           f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
        self.buf = b""
        while b"\r\n\r\n" not in self.buf:
            self.buf += self.sock.recv(4096)
        self.buf = self.buf.split(b"\r\n\r\n", 1)[1]
        self.n = 0
        assert self.recv()["type"] == "auth_required"
        self.send({"type": "auth", "access_token": token})
        if self.recv()["type"] != "auth_ok":
            sys.exit("websocket auth failed")

    def _need(self, n):
        while len(self.buf) < n:
            self.buf += self.sock.recv(65536)
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def send(self, obj):
        import struct
        data = json.dumps(obj).encode()
        mask = os.urandom(4)
        head = bytes([0x81])
        if len(data) < 126:
            head += bytes([0x80 | len(data)])
        elif len(data) < 65536:
            head += bytes([0x80 | 126]) + struct.pack(">H", len(data))
        else:
            head += bytes([0x80 | 127]) + struct.pack(">Q", len(data))
        self.sock.sendall(head + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))

    def recv(self):
        import struct
        msg = b""
        while True:
            b0, b1 = self._need(2)
            n = b1 & 0x7F
            if n == 126:
                n = struct.unpack(">H", self._need(2))[0]
            elif n == 127:
                n = struct.unpack(">Q", self._need(8))[0]
            msg += self._need(n)
            if b0 & 0x80:
                return json.loads(msg)

    def supervisor(self, endpoint, method="get", data=None):
        self.n += 1
        req = {"id": self.n, "type": "supervisor/api", "endpoint": endpoint, "method": method}
        if data is not None:
            req["data"] = data
        self.send(req)
        while True:
            r = self.recv()
            if r.get("id") == self.n:
                return r


def token_from_code(code):
    _, tok = call("POST", "/auth/token", {"grant_type": "authorization_code", "code": code,
                                          "client_id": CLIENT}, form=True)
    return tok["access_token"]


def main():
    os.makedirs(os.path.dirname(LOGIN), exist_ok=True)
    status, steps = call("GET", "/api/onboarding")
    pending = [s["step"] for s in steps if not s["done"]] if isinstance(steps, list) else []
    if "user" in pending:
        creds = {"username": "dev", "password": secrets.token_urlsafe(18)}
        with open(LOGIN, "w", encoding="utf-8") as f:
            json.dump(creds, f)
        st, res = call("POST", "/api/onboarding/users", {
            "client_id": CLIENT, "name": "Dev", "username": creds["username"],
            "password": creds["password"], "language": "en"})
        if st != 200:
            sys.exit(f"creating the test user failed: {st} {res}")
        token = token_from_code(res["auth_code"])
        print("created the dev test user (login saved in dev-data/ha-dev-login.json)")
    else:
        creds = json.load(open(LOGIN, encoding="utf-8"))
        # Log in through the auth flow to get a token.
        _, flow = call("POST", "/auth/login_flow", {"client_id": CLIENT, "handler": ["homeassistant", None],
                                                    "redirect_uri": CLIENT})
        _, res = call("POST", f"/auth/login_flow/{flow['flow_id']}",
                      {"client_id": CLIENT, "username": creds["username"], "password": creds["password"]})
        token = token_from_code(res["result"])
    for step, body in (("core_config", {}), ("analytics", {}),
                       ("integration", {"client_id": CLIENT, "redirect_uri": CLIENT})):
        if step in pending:
            st, _ = call("POST", f"/api/onboarding/{step}", body, token=token)
            print(f"onboarding {step}: {st}")

    # The app as Home Assistant sees it, then its panel through the ingress proxy.
    # App info and ingress sessions go over the WebSocket API, like the frontend.
    ws = WS(token)
    r = ws.supervisor(f"/addons/{SLUG}/info")
    if not r.get("success"):
        sys.exit(f"app info failed: {r}")
    d = r["result"]
    print(f"app: {d['name']} {d['version']} state={d['state']} ingress={d['ingress']} "
          f"panel={d.get('ingress_panel')} entry={d.get('ingress_entry')}")
    if not d.get("ingress_panel"):  # "Show in sidebar" is off by default
        r = ws.supervisor(f"/addons/{SLUG}/options", "post", {"ingress_panel": True})
        print(f"show in sidebar: {'on' if r.get('success') else r}")
    r = ws.supervisor("/ingress/session", "post")
    if not r.get("success"):
        sys.exit(f"ingress session failed: {r}")
    cookie = f"ingress_session={r['result']['session']}"
    entry = d["ingress_entry"]
    st, page = call("GET", entry + "/", cookie=cookie, raw=True)
    print(f"ingress {entry}/ -> HTTP {st}, title: {page.split('<title>')[1].split('</title>')[0] if '<title>' in page else '?'}")
    st, status = call("GET", entry + "/api/status", cookie=cookie)
    if st == 200:
        sp = status["spotify"]
        print(f"ingress api/status -> speakers={len(status['speakers'])} spotify.ready={sp['ready']} reason={sp['reason']!r}")
    else:
        print(f"ingress api/status -> HTTP {st} {status}")


if __name__ == "__main__":
    main()
