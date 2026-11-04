"""A fake Samsung multiroom speaker for demos and tests (TCP 55001).

Answers the commands the app uses with believable replies and keeps its state
(volume, group, Wi-Fi) in memory. Run one per container/IP, e.g.:

  python3 fake_speaker.py --name "Kitchen" --model SPK-WAM750 --mac 02:00:00:00:00:01 \
      --ssid HomeWiFi --rssi 4 --volume 9

`dev/run-demo.ps1` starts a few of these next to a demo copy of the app.
"""
import argparse
import http.server
import re
import socketserver
import urllib.parse

ARGS = None
STATE = {}


def reply(method: str, body: str, result: str = "ok") -> bytes:
    xml = (f'<?xml version="1.0" encoding="UTF-8"?><UIC><method>{method}</method><version>1.0</version>'
           f'<speakerip>{STATE["ip"]}</speakerip><user_identifier>public</user_identifier>'
           f'<response result="{result}">{body}</response></UIC>')
    return xml.encode()


def handle(cmd: str) -> bytes:
    name = re.search(r"<name>(\w+)</name>", cmd).group(1)
    params = dict(re.findall(r'name="(\w+)" val="([^"]*)"', cmd))
    cdata = dict(re.findall(r'name="(\w+)" val="empty"><!\[CDATA\[(.*?)\]\]>', cmd))
    params.update(cdata)
    s = STATE
    if name == "GetSpkName":
        return reply("SpkName", f"<spkname><![CDATA[{s['name']}]]></spkname>")
    if name == "GetMainInfo":
        return reply("MainInfo", f"<party>off</party><partymain></partymain><grouptype>{s['grouptype']}</grouptype>"
                     f"<groupmainip>{s['main_ip']}</groupmainip><groupmainmacaddr></groupmainmacaddr>"
                     f"<spkmacaddr>{s['mac']}</spkmacaddr><spkmodelname>{s['model']}</spkmodelname>"
                     "<groupmode>none</groupmode><channeltype>front</channeltype>")
    if name == "GetApInfo":
        return reply("ApInfo", f"<ssid>{s['ssid']}</ssid><mac>{s['mac']}</mac><rssi>{s['rssi']}</rssi>"
                     f"<ch>{s['ch']}</ch><connectiontype>wireless</connectiontype>")
    if name == "GetVolume":
        return reply("VolumeLevel", f"<volume>{s['volume']}</volume>")
    if name == "SetVolume":
        s["volume"] = max(0, min(30, int(params.get("volume", s["volume"]))))
        return reply("VolumeLevel", f"<volume>{s['volume']}</volume>")
    if name == "GetGroupName":
        return reply("GroupName", f"<groupname><![CDATA[{s['group']}]]></groupname>")
    if name == "SetMultispkGroup":
        s["group"] = params.get("name", "")
        s["grouptype"] = "M" if params.get("type") == "main" else "S"
        s["main_ip"] = s["ip"] if params.get("type") == "main" else params.get("mainspkip", "")
        return reply("MultispkGroup", "")
    if name == "SetUngroup":
        s["group"], s["grouptype"], s["main_ip"] = "", "N", ""
        return reply("Ungroup", "")
    if name == "GetApList":
        aps = "".join(f"<ap><index>{i}</index><apname>{n}</apname><lockstatus>lock</lockstatus></ap>"
                      for i, n in enumerate([s["ssid"], "HomeWiFi-5G", "Neighbour"]))
        return reply("ApList", f"<listcount>3</listcount><aplist>{aps}</aplist>")
    if name == "GetSoftwareVersion":
        return reply("SoftwareVersion", f"<version>{s['model'].replace('SPK-', '')}WWB-3117.1</version>")
    return reply(name, "", result="ng")


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_GET(self):
        query = urllib.parse.urlparse(self.path).query
        cmd = urllib.parse.unquote(urllib.parse.parse_qs(query).get("cmd", [""])[0])
        body = handle(cmd) if "<name>" in cmd else b""
        self.send_response(200)
        self.send_header("Content-Type", "text/xml")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main():
    global ARGS
    p = argparse.ArgumentParser()
    p.add_argument("--name", required=True)
    p.add_argument("--model", default="SPK-WAM350")
    p.add_argument("--mac", required=True)
    p.add_argument("--ip", default="")
    p.add_argument("--ssid", default="HomeWiFi")
    p.add_argument("--rssi", type=int, default=4)
    p.add_argument("--ch", type=int, default=6)
    p.add_argument("--volume", type=int, default=8)
    p.add_argument("--group", default="", help="group name if this speaker leads a group")
    p.add_argument("--main-ip", default="", help="main speaker's IP if this speaker is a group member")
    ARGS = p.parse_args()
    import socket
    ip = ARGS.ip or socket.gethostbyname(socket.gethostname())
    STATE.update(name=ARGS.name, model=ARGS.model, mac=ARGS.mac, ip=ip, ssid=ARGS.ssid, rssi=ARGS.rssi,
                 ch=ARGS.ch, volume=ARGS.volume, group=ARGS.group,
                 grouptype="M" if ARGS.group else ("S" if ARGS.main_ip else "N"), main_ip=ARGS.main_ip or (ip if ARGS.group else ""))

    class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
        daemon_threads = True
        allow_reuse_address = True
    print(f"fake speaker {ARGS.name} on {ip}:55001", flush=True)
    Server(("0.0.0.0", 55001), Handler).serve_forever()


if __name__ == "__main__":
    main()
