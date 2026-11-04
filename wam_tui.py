"""Full-screen terminal UI for Samsung multiroom speakers.

    python samsung_wam_wifi.py          (opens this UI when Textual is installed)
    python wam_tui.py

Remembers your speakers and networks in samsung_wam_config.json, shows them in
a table with their group, Wi-Fi and volume, and lets you group, ungroup,
change volume, add speakers by IP and run the Wi-Fi setup.
Needs: pip install -r requirements.txt
"""
from __future__ import annotations

import ipaddress
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from rich.markup import escape
from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import (Button, DataTable, Footer, Header, Input, Label, OptionList,
                             RichLog, Static)
from textual.widgets.option_list import Option

import samsung_wam_wifi as wam

CMD_GET_VOLUME = "<name>GetVolume</name>"
CMD_SET_VOLUME = '<name>SetVolume</name><p type="dec" name="volume" val="{volume}"/>'
MAX_VOLUME = 30  # raw speaker steps


@dataclass
class Row:
    ip: str
    name: str = ""
    mac: str = ""
    model: str = ""
    online: bool | None = None  # None = not checked yet
    role: str = ""
    main_ip: str = ""
    group: str = ""
    ssid: str = ""
    rssi: str = ""
    conn: str = ""
    volume: int | None = None
    firmware: str = ""


def fetch(ip: str, port: int, known: Row | None = None) -> Row:
    """Everything the UI shows about one speaker. Blocking: run it in a thread."""
    row = Row(ip=ip)
    if known:
        row.name, row.mac, row.model = known.name, known.mac, known.model
    state = wam.speaker_state(ip, port)
    if state is None:
        row.online = False
        return row
    row.online = True
    row.name, row.mac, row.model = state.name, state.mac, state.model
    row.role, row.main_ip, row.group = state.role, state.main_ip, state.group
    try:
        ap = wam.response_fields(wam.send(ip, wam.CMD_GET_AP_INFO, port))
        row.ssid, row.rssi, row.conn = ap.get("ssid", ""), ap.get("rssi", ""), ap.get("connectiontype", "")
        vol = wam.response_fields(wam.send(ip, CMD_GET_VOLUME, port)).get("volume", "")
        row.volume = int(vol) if vol.isdigit() else None
        row.firmware = wam.response_fields(wam.send(ip, wam.CMD_GET_SW_VERSION, port)).get("version", "")
    except OSError:
        pass
    return row


# --------------------------------------------------------------------------
# Dialogs
# --------------------------------------------------------------------------

class Dialog(ModalScreen):
    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def action_cancel(self) -> None:
        self.dismiss(None)


class ConfirmScreen(Dialog):
    BINDINGS = [Binding("y", "yes", "Yes"), Binding("n", "cancel", "No")]

    def __init__(self, title: str, message: str, yes: str = "Yes") -> None:
        super().__init__()
        self.title_text, self.message, self.yes_label = title, message, yes

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(self.title_text, classes="title")
            yield Static(Text(self.message))
            with Horizontal(classes="buttons"):
                yield Button(self.yes_label, variant="primary", id="yes")
                yield Button("Cancel", id="no")

    def action_yes(self) -> None:
        self.dismiss(True)

    @on(Button.Pressed)
    def pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "yes" or None)


class InputScreen(Dialog):
    def __init__(self, title: str, prompt: str, value: str = "", placeholder: str = "") -> None:
        super().__init__()
        self.title_text, self.prompt, self.value, self.placeholder = title, prompt, value, placeholder

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(self.title_text, classes="title")
            yield Label(self.prompt)
            yield Input(value=self.value, placeholder=self.placeholder, id="value")
            with Horizontal(classes="buttons"):
                yield Button("OK", variant="primary", id="ok")
                yield Button("Cancel", id="cancel")

    @on(Input.Submitted)
    def submitted(self) -> None:
        self.dismiss(self.query_one(Input).value.strip())

    @on(Button.Pressed)
    def pressed(self, event: Button.Pressed) -> None:
        self.dismiss(self.query_one(Input).value.strip() if event.button.id == "ok" else None)


class GroupScreen(Dialog):
    """Pick the main speaker among the marked ones and name the group."""

    def __init__(self, members: list[Row]) -> None:
        super().__init__()
        self.members = members

    def default_name(self, main: Row) -> str:
        return main.group if main.role == "main" and main.group else f"{main.name} group"

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(f"Group {len(self.members)} speakers", classes="title")
            yield Label("Main speaker (plays the music, the others follow it):")
            yield OptionList(*[Option(Text(f"{r.name}  {r.ip}"), id=r.ip) for r in self.members],
                             id="main")
            yield Label("Group name:")
            yield Input(placeholder=self.default_name(self.members[0]), id="name")
            with Horizontal(classes="buttons"):
                yield Button("Group", variant="primary", id="ok")
                yield Button("Cancel", id="cancel")

    def on_mount(self) -> None:
        self.query_one(OptionList).highlighted = 0

    def chosen(self) -> Row:
        index = self.query_one(OptionList).highlighted
        return self.members[index if index is not None else 0]

    @on(OptionList.OptionHighlighted)
    def highlighted(self) -> None:
        self.query_one(Input).placeholder = self.default_name(self.chosen())

    @on(OptionList.OptionSelected)
    def selected(self) -> None:
        self.query_one(Input).focus()

    @on(Input.Submitted)
    @on(Button.Pressed, "#ok")
    def ok(self) -> None:
        main = self.chosen()
        self.dismiss((main.ip, self.query_one(Input).value.strip() or self.default_name(main)))

    @on(Button.Pressed, "#cancel")
    def cancel(self) -> None:
        self.dismiss(None)


# --------------------------------------------------------------------------
# Main screen
# --------------------------------------------------------------------------

class SpeakerApp(App):
    TITLE = "Samsung multiroom speakers"
    CSS = """
    #main { height: 1fr; }
    #table { width: 2fr; }
    #details { width: 1fr; min-width: 36; border: round $accent; padding: 0 1; }
    #log { height: 9; border: round $panel; }
    Dialog { align: center middle; }
    #dialog { width: 64; height: auto; border: thick $accent; background: $surface; padding: 1 2; }
    #dialog .title { text-style: bold; margin-bottom: 1; }
    #dialog OptionList { height: auto; max-height: 10; margin-bottom: 1; }
    .buttons { height: auto; align-horizontal: right; margin-top: 1; }
    .buttons Button { margin-left: 1; }
    """
    BINDINGS = [
        Binding("r", "refresh", "Refresh"),
        Binding("s", "scan", "Scan"),
        Binding("space", "mark", "Mark"),
        Binding("g", "group", "Group"),
        Binding("u", "ungroup", "Ungroup"),
        Binding("plus,equals_sign", "volume(1)", "Vol+"),
        Binding("minus", "volume(-1)", "Vol-"),
        Binding("a", "add", "Add IP"),
        Binding("d,delete", "forget", "Forget"),
        Binding("n", "networks", "Networks"),
        Binding("w", "wifi", "Wi-Fi setup"),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(self, port: int = wam.PORT, scan_on_start: bool = False) -> None:
        super().__init__()
        self.port, self.scan_on_start = port, scan_on_start
        self.rows: dict[str, Row] = {}
        self.marked: set[str] = set()
        self.volume_timers: dict = {}

    # ---- layout ---------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal(id="main"):
            yield DataTable(id="table", cursor_type="row", zebra_stripes=True)
            yield Static(id="details")
        yield RichLog(id="log", markup=True, wrap=True)
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one(DataTable)
        for key, label in [("mark", " "), ("name", "Speaker"), ("ip", "IP"),
                           ("group", "Group"), ("wifi", "Wi-Fi"), ("vol", "Volume")]:
            table.add_column(label, key=key)
        for s in wam.known_speakers():
            self.rows[s["ip"]] = Row(ip=s["ip"], name=s.get("name", ""), mac=s.get("mac", ""),
                                     model=s.get("model", ""))
        self.rebuild()
        table.focus()
        if not self.rows or self.scan_on_start:
            self.say("Looking for speakers..." if not self.rows else "Scanning for speakers...")
            self.scan_worker()
        else:
            self.refresh_worker()
        self.say("[dim]Space marks speakers for grouping. Keys are listed at the bottom.[/dim]")

    # ---- helpers --------------------------------------------------------

    def say(self, message: str) -> None:
        self.query_one(RichLog).write(message)

    def selected(self) -> Row | None:
        table = self.query_one(DataTable)
        if not table.row_count:
            return None
        key = table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value
        return self.rows.get(key)

    def name_of(self, ip: str) -> str:
        row = self.rows.get(ip)
        return row.name if row and row.name else ip

    def cells(self, r: Row) -> list[Text]:
        dim = "dim" if r.online is False else ""
        if r.online is None:
            status, wifi, vol = "…", "", ""
        elif not r.online:
            status, wifi, vol = "offline", "", ""
        else:
            status = {"main": f"★ {r.group or 'group'}",
                      "sub": f"↳ {self.name_of(r.main_ip)}"}.get(r.role, "–")
            wifi = f"{r.ssid or r.conn} {r.rssi}/5" if r.ssid else (r.conn or "")
            vol = f"{r.volume}/{MAX_VOLUME}" if r.volume is not None else ""
        return [Text("✓" if r.ip in self.marked else " ", style="bold green"),
                Text(r.name or "?", style=dim), Text(r.ip, style=dim),
                Text(status, style=dim), Text(wifi, style=dim), Text(vol, style=dim)]

    def rebuild(self) -> None:
        table = self.query_one(DataTable)
        current = self.selected()
        table.clear()
        for r in sorted(self.rows.values(), key=lambda r: (r.name.lower(), r.ip)):
            table.add_row(*self.cells(r), key=r.ip)
        if current and current.ip in self.rows:
            table.move_cursor(row=table.get_row_index(current.ip))
        self.show_details()

    def render_row(self, ip: str) -> None:
        table = self.query_one(DataTable)
        for key, cell in zip(["mark", "name", "ip", "group", "wifi", "vol"], self.cells(self.rows[ip])):
            table.update_cell(ip, key, cell, update_width=True)
        self.show_details()

    def show_details(self) -> None:
        r = self.selected()
        panel = self.query_one("#details", Static)
        if r is None:
            panel.update("No speakers yet.\n\nPress [b]s[/b] to scan or [b]a[/b] to add one by IP.")
            return
        if r.role == "main":
            group = f'main of "{escape(r.group)}"'
        elif r.role == "sub":
            group = f"member, main is {escape(self.name_of(r.main_ip))}"
        else:
            group = "not grouped"
        state = {None: "checking…", False: "[red]offline[/red]", True: "[green]online[/green]"}[r.online]
        lines = [f"[b]{escape(r.name or r.ip)}[/b]  {state}", "",
                 f"IP        {r.ip}", f"MAC       {escape(r.mac)}", f"Model     {escape(r.model)}"]
        if r.online:
            lines += [f"Firmware  {escape(r.firmware)}",
                      f"Network   {escape(r.ssid)} ({escape(r.conn)})" if r.ssid else f"Network   {escape(r.conn)}",
                      f"Signal    {r.rssi}/5" if r.rssi else "Signal    ?",
                      f"Volume    {r.volume}/{MAX_VOLUME}" if r.volume is not None else "Volume    ?",
                      f"Group     {group}"]
        panel.update("\n".join(lines))

    @on(DataTable.RowHighlighted)
    def highlighted(self) -> None:
        self.show_details()

    def apply(self, rows: list[Row]) -> None:
        added = False
        for r in rows:
            added |= r.ip not in self.rows
            self.rows[r.ip] = r
        if added:
            self.rebuild()
        else:
            for r in rows:
                self.render_row(r.ip)
        online = [r for r in rows if r.online]
        if online:
            wam.remember_speakers([wam.Spk(r.ip, r.name, r.mac, r.role, r.main_ip, r.group, r.model)
                                   for r in online])
        for r in rows:
            if r.online is False:
                self.say(f"[yellow]{escape(r.name or r.ip)} ({r.ip}) doesn't answer.[/yellow]")

    def clear_marks(self) -> None:
        self.marked.clear()

    def fetch_all(self, ips: list[str]) -> list[Row]:
        with ThreadPoolExecutor(max_workers=8) as pool:
            return list(pool.map(lambda ip: fetch(ip, self.port, self.rows.get(ip)), ips))

    # ---- workers (network calls happen off the UI thread) ---------------

    @work(thread=True, group="net")
    def refresh_worker(self) -> None:
        rows = self.fetch_all(list(self.rows))
        self.call_from_thread(self.apply, rows)
        self.call_from_thread(self.say, f"Refreshed {sum(r.online for r in rows)}/{len(rows)} speakers.")

    @work(thread=True, group="net")
    def scan_worker(self) -> None:
        nets = wam.networks([], around=[ip for ip in self.rows])
        if not nets:
            self.call_from_thread(self.say, "[yellow]No networks to scan. Press [b]n[/b] to add "
                                            "one, e.g. 192.168.10.0/24.[/yellow]")
            return
        self.call_from_thread(self.say, "Scanning " + ", ".join(map(str, nets)) + " ...")
        found = wam.all_speakers(nets, self.port)
        new = [s.ip for s in found if s.ip not in self.rows]
        rows = self.fetch_all(list(dict.fromkeys([*self.rows, *(s.ip for s in found)])))
        self.call_from_thread(self.apply, rows)
        message = f"Found {len(found)} speaker(s)" + (f", {len(new)} new." if new else ".")
        if not found:
            message += " If they're on another network, press [b]n[/b] to add it."
        self.call_from_thread(self.say, message)

    @work(thread=True, group="net")
    def group_worker(self, main_ip: str, others: list[str], name: str) -> None:
        nets = wam.networks([], around=[main_ip, *others])
        try:
            lines = wam.make_group(main_ip, others, name, self.port, nets)
        except wam.GroupError as exc:
            self.call_from_thread(self.say, f"[red]{escape(str(exc))}[/red]")
            return
        for line in lines:
            self.call_from_thread(self.say, escape(line))
        self.call_from_thread(self.clear_marks)
        rows = self.fetch_all(list(self.rows))
        self.call_from_thread(self.apply, rows)

    @work(thread=True, group="net")
    def ungroup_worker(self, ip: str) -> None:
        try:
            lines = wam.ungroup(ip, self.port, wam.networks([], around=[ip, *self.rows]))
        except wam.GroupError as exc:
            self.call_from_thread(self.say, f"[red]{escape(str(exc))}[/red]")
            return
        for line in lines:
            self.call_from_thread(self.say, escape(line))
        rows = self.fetch_all(list(self.rows))
        self.call_from_thread(self.apply, rows)

    @work(thread=True, group="volume")
    def volume_worker(self, ip: str, volume: int) -> None:
        try:
            result = wam.response_fields(wam.send(ip, CMD_SET_VOLUME.format(volume=volume), self.port))
            ok = result.get("result") == "ok"
        except OSError:
            ok = False
        if not ok:  # show what the speaker really has
            self.call_from_thread(self.say, f"[red]{escape(self.name_of(ip))} didn't take the "
                                            "volume change.[/red]")
            row = fetch(ip, self.port, self.rows.get(ip))
            self.call_from_thread(self.apply, [row])

    @work(thread=True, group="net")
    def add_worker(self, ip: str) -> None:
        row = fetch(ip, self.port)
        if not row.online:
            self.call_from_thread(self.say, f"[red]No speaker answers at {ip}.[/red]")
            return
        self.call_from_thread(self.apply, [row])
        self.call_from_thread(self.say, f"Added {escape(row.name)} ({ip}).")

    # ---- actions --------------------------------------------------------

    def action_refresh(self) -> None:
        if self.rows:
            self.say("Refreshing...")
            self.refresh_worker()

    def action_scan(self) -> None:
        self.scan_worker()

    def action_mark(self) -> None:
        r = self.selected()
        if r:
            self.marked ^= {r.ip}
            self.render_row(r.ip)

    def action_group(self) -> None:
        members = [self.rows[ip] for ip in self.marked if ip in self.rows]
        if len(members) < 2:
            self.say("[yellow]Mark at least two speakers with Space, then press g.[/yellow]")
            return
        offline = [m for m in members if not m.online]
        if offline:
            self.say(f"[yellow]{escape(offline[0].name or offline[0].ip)} is offline.[/yellow]")
            return
        members.sort(key=lambda r: (r.role != "main", r.name.lower()))

        def done(result: tuple[str, str] | None) -> None:
            if result:
                main_ip, name = result
                self.say(f"Grouping as \"{escape(name)}\"...")
                self.group_worker(main_ip, [m.ip for m in members if m.ip != main_ip], name)

        self.push_screen(GroupScreen(members), done)

    def action_ungroup(self) -> None:
        r = self.selected()
        if r is None:
            return
        if not r.role:
            self.say(f"{escape(r.name)} isn't grouped.")
            return
        if r.role == "main":
            members = [x.name for x in self.rows.values() if x.role == "sub" and x.main_ip == r.ip]
            message = (f'End the group "{r.group}"? {r.name} and '
                       f"{', '.join(members) or 'its members'} play separately again.")
        else:
            message = f"Take {r.name} out of its group? The rest of the group stays together."

        def done(ok: bool | None) -> None:
            if ok:
                self.ungroup_worker(r.ip)

        self.push_screen(ConfirmScreen("Ungroup", message, yes="Ungroup"), done)

    def action_volume(self, delta: int) -> None:
        """Show the new level at once; send it once the key presses stop."""
        r = self.selected()
        if r is None or not r.online or r.volume is None:
            return
        volume = max(0, min(MAX_VOLUME, r.volume + delta))
        if volume == r.volume:
            return
        r.volume = volume
        self.render_row(r.ip)
        if r.ip in self.volume_timers:
            self.volume_timers.pop(r.ip).stop()
        self.volume_timers[r.ip] = self.set_timer(
            0.3, lambda ip=r.ip, v=volume: self.volume_worker(ip, v))

    def action_add(self) -> None:
        def done(value: str | None) -> None:
            if not value:
                return
            try:
                ipaddress.ip_address(value)
            except ValueError:
                self.say(f"[red]{escape(value)} isn't an IP address.[/red]")
                return
            self.add_worker(value)

        self.push_screen(InputScreen("Add speaker", "Speaker's IP address (from your router):",
                                     placeholder="192.168.10.50"), done)

    def action_forget(self) -> None:
        r = self.selected()
        if r is None:
            return

        def done(ok: bool | None) -> None:
            if ok:
                wam.forget_speaker(r.ip)
                self.rows.pop(r.ip, None)
                self.marked.discard(r.ip)
                self.rebuild()
                self.say(f"Forgot {escape(r.name or r.ip)}.")

        self.push_screen(ConfirmScreen("Forget speaker", f"Remove {r.name or r.ip} ({r.ip}) from "
                                       "the list? A scan finds it again.", yes="Forget"), done)

    def action_networks(self) -> None:
        def done(value: str | None) -> None:
            if value is None:
                return
            subnets = []
            for part in value.replace(",", " ").split():
                try:
                    subnets.append(str(ipaddress.ip_network(part, strict=False)))
                except ValueError:
                    self.say(f"[red]{escape(part)} isn't a network like 192.168.10.0/24.[/red]")
                    return
            cfg = wam.load_config()
            cfg["subnets"] = subnets
            wam.save_config(cfg)
            self.say("Networks to scan: " + (", ".join(subnets) or "only this PC's own") + ".")
            self.scan_worker()

        self.push_screen(InputScreen(
            "Networks", "Extra networks to scan, separated by commas (this PC's own are always "
            "included):", value=", ".join(wam.load_subnets()), placeholder="192.168.10.0/24"), done)

    def action_wifi(self) -> None:
        self.exit("wifi")


def run(port: int = wam.PORT) -> int:
    scan = False
    while True:
        result = SpeakerApp(port, scan_on_start=scan).run()
        if result != "wifi":
            return 0
        try:
            wam.easy_mode(port)
        except KeyboardInterrupt:
            print("\nStopped.")
        except RuntimeError as exc:
            print(f"\nWindows refused the Wi-Fi change:\n    {exc}")
        try:
            input("\nPress Enter to go back to the speaker list...")
        except (EOFError, KeyboardInterrupt):
            return 0
        scan = True


if __name__ == "__main__":
    port = wam.PORT
    if len(sys.argv) == 3 and sys.argv[1] == "--port":
        port = int(sys.argv[2])
    sys.exit(run(port))
