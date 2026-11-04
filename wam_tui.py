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
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from rich.markup import escape
from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen, Screen
from textual.widgets import (Button, ContentSwitcher, DataTable, Footer, Header, Input, Label,
                             LoadingIndicator, OptionList, RichLog, Static)
from textual.widgets.option_list import Option

import samsung_wam_wifi as wam

CMD_GET_VOLUME = "<name>GetVolume</name>"
CMD_SET_VOLUME = '<name>SetVolume</name><p type="dec" name="volume" val="{volume}"/>'
MAX_VOLUME = 30  # raw speaker steps


def tx(value: str) -> str:
    """Escape text for Textual markup (Static.update), which reads any [word], e.g. the
    "[Samsung]" in default speaker names, as a tag. rich.markup.escape only suits the
    RichLog, which uses Rich markup."""
    return str(value).replace("[", r"\[")


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
# Wi-Fi setup
# --------------------------------------------------------------------------

WIFI_STEPS = ["Put the speaker in setup mode", "Find its setup Wi-Fi", "Connect this PC to it",
              "Choose your home Wi-Fi", "Send the Wi-Fi details", "Find it on your network"]


class WifiSetupScreen(Screen):
    """Connects a speaker to Wi-Fi: the same steps as the text wizard (easy_mode).

    Network work runs in threads, one at a time (self.net_lock). Cancelling sets
    self.cancelled, waits for the running step to notice, then puts this PC's Wi-Fi
    back if it was switched to the speaker. Dismisses with the speaker's new IP,
    or None.
    """
    SUB_TITLE = "Wi-Fi setup"
    BINDINGS = [Binding("escape", "cancel", "Cancel")]
    TICK = 3.0             # seconds between hotspot / speaker checks
    HOTSPOT_TIMEOUT = 90.0
    SPEAKER_TIMEOUT = 45.0
    HOME_POLL = 10.0       # seconds between searches for the speaker on the home network
    HOME_TIMEOUT = 180.0

    def __init__(self, port: int) -> None:
        super().__init__()
        self.port = port
        self.cancelled = threading.Event()
        self.net_lock = threading.Lock()
        self.closing = False
        self.step = 0
        self.failed = False
        self.iface = self.previous = ""
        self.connected = False  # this PC's Wi-Fi is on the speaker's hotspot
        self.ip = self.spk_name = None
        self.mac = ""
        self.candidates: list[dict[str, str]] = []
        self.homes: list[dict[str, str]] = []
        self.result: str | None = None

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal(id="wifi"):
            yield Static(id="steps")
            with Vertical(id="wifi-body"):
                yield Static(id="wifi-text")
                with ContentSwitcher(id="pane", initial="intro"):
                    with Vertical(id="intro"):
                        with Horizontal(classes="buttons"):
                            yield Button("Start", variant="primary", id="start")
                            yield Button("Cancel", classes="cancel")
                    with Vertical(id="busy"):
                        yield LoadingIndicator()
                        yield Static(id="busy-status")
                        with Horizontal(classes="buttons"):
                            yield Button("Cancel", classes="cancel")
                    with Vertical(id="pick-hotspot"):
                        yield OptionList(id="hotspots")
                        with Horizontal(classes="buttons"):
                            yield Button("Use this one", variant="primary", id="use-hotspot")
                            yield Button("Search again", id="retry")
                            yield Button("Cancel", classes="cancel")
                    with Vertical(id="home"):
                        yield OptionList(id="homes")
                        yield Label("Network name:")
                        yield Input(id="ssid", placeholder="pick above or type it (case-sensitive)")
                        yield Label("Wi-Fi password (leave empty if none):")
                        yield Input(id="password", password=True)
                        with Horizontal(classes="buttons"):
                            yield Button("Send to speaker", variant="primary", id="send")
                            yield Button("Cancel", classes="cancel")
                    with Vertical(id="done"):
                        with Horizontal(classes="buttons"):
                            yield Button("Back to speakers", variant="primary", classes="cancel")
        yield Footer()

    def on_mount(self) -> None:
        self.goto(0, "intro",
                  "This connects a speaker to your Wi-Fi without the Samsung app. It briefly "
                  "switches this PC's Wi-Fi to the speaker; a wired connection stays up.\n\n"
                  "Plug in the speaker and switch it on. Then hold the [b]WPS/Wi-Fi SETUP[/b] "
                  "button on its back for about 5 seconds, until its light changes.\n\n"
                  "Press [b]Start[/b] when you've done that.")

    # ---- UI helpers (UI thread) ----------------------------------------

    def goto(self, step: int, pane: str, text: str, failed: bool = False) -> None:
        self.step, self.failed = step, failed
        lines = []
        for n, title in enumerate(WIFI_STEPS):
            if n < step:
                mark = "[green]✓[/green]"
            elif n == step and failed:
                mark = "[red]✗[/red]"
            elif n == step:
                mark = "[b]▶[/b]"
            else:
                mark = "[dim]·[/dim]"
            lines.append(f"{mark} {n + 1}. {title}" if n != step else f"{mark} [b]{n + 1}. {title}[/b]")
        self.query_one("#steps", Static).update("\n".join(lines))
        self.query_one("#wifi-text", Static).update(text)
        self.query_one("#busy-status", Static).update("")
        self.query_one(ContentSwitcher).current = pane
        target = {"intro": "#start", "busy": "#busy .cancel", "pick-hotspot": "#hotspots",
                  "home": "#homes" if self.homes else "#ssid", "done": "#done .cancel"}[pane]
        self.query_one(target).focus()

    def set_status(self, text: str) -> None:
        self.query_one("#busy-status", Static).update(text)

    def ticker(self, label: str):
        """tick() for wam's wait helpers: shows elapsed time, stops early on cancel."""
        start = time.monotonic()

        def tick() -> bool:
            self.app.call_from_thread(self.set_status, f"{label} {time.monotonic() - start:.0f} s")
            return not self.cancelled.wait(self.TICK)
        return tick

    def ui(self, method, *args, **kwargs) -> None:
        """Run a UI method from a worker thread, unless the user is cancelling."""
        if not self.cancelled.is_set():
            self.app.call_from_thread(method, *args, **kwargs)

    def finish(self) -> None:
        self.dismiss(self.result)

    # ---- worker helpers (worker threads) --------------------------------

    def disconnect(self) -> None:
        if self.connected:
            wam.disconnect_hotspot(self.iface, self.previous)
            self.connected = False
            self.app.wifi_cleanup = None

    def fail(self, step: int, text: str) -> None:
        self.disconnect()
        self.ui(self.goto, step, "done", text, failed=True)

    # ---- steps -----------------------------------------------------------

    @on(Button.Pressed, "#start")
    @on(Button.Pressed, "#retry")
    def start_search(self) -> None:
        self.goto(1, "busy", "Looking for the speaker's setup Wi-Fi (up to 90 seconds)...")
        self.find_worker()

    @work(thread=True, group="wifi")
    def find_worker(self) -> None:
        with self.net_lock:
            if self.cancelled.is_set():
                return
            if not self.iface:
                self.iface = wam.wifi_interface() if wam.IS_WINDOWS else ""
            if not self.iface:
                self.fail(1, "I can't switch Wi-Fi networks automatically here "
                          + ("(no Wi-Fi adapter found)." if wam.IS_WINDOWS else "(only on Windows).")
                          + "\n\nDo it by hand instead:\n"
                          "  1. Connect a phone or laptop to the Wi-Fi called \"\\[Samsung] ...\" "
                          f"(password {wam.HOTSPOT_PASSWORD}).\n"
                          "  2. From that device run:\n"
                          "     python samsung_wam_wifi.py join <its-gateway-ip> --ssid \"YourWiFi\"")
                return
            hits = wam.wait_for_hotspot(self.iface, self.HOTSPOT_TIMEOUT,
                                         self.ticker("Searching..."))
            if self.cancelled.is_set():
                return
            if len(hits) == 1:
                self.connect(hits[0])
                return
            if hits:
                self.candidates = hits
                text = "Found several speakers in setup mode. Which one?"
            else:
                blocked = wam.location_blocked(self.iface)
                if blocked:
                    self.fail(1, "Windows won't list Wi-Fi networks:\n\n" + tx(blocked))
                    return
                self.candidates, _ = wam.visible_networks(self.iface)
                if not self.candidates:
                    self.fail(1, "I couldn't find the speaker's setup Wi-Fi. Hold the button a "
                              "bit longer (or unplug the speaker for 10 seconds first) and try "
                              "again. If it never appears, use WPS or an Ethernet cable instead.")
                    return
                text = ("I didn't recognise the speaker's Wi-Fi by name. If one of these is "
                        "it, pick it. Otherwise hold the button a bit longer and search again.")
            self.ui(self.show_candidates, text)

    def show_candidates(self, text: str) -> None:
        options = self.query_one("#hotspots", OptionList)
        options.clear_options()
        options.add_options([Option(Text(f"{n['ssid']}   ({n['auth'] or '?'})")) for n in self.candidates])
        options.highlighted = 0
        self.goto(1, "pick-hotspot", text)

    @on(OptionList.OptionSelected, "#hotspots")
    @on(Button.Pressed, "#use-hotspot")
    def use_hotspot(self) -> None:
        index = self.query_one("#hotspots", OptionList).highlighted
        if index is not None:
            self.goto(2, "busy", "")
            self.connect_worker(self.candidates[index])

    @work(thread=True, group="wifi")
    def connect_worker(self, hotspot: dict[str, str]) -> None:
        with self.net_lock:
            if not self.cancelled.is_set():
                self.connect(hotspot)

    def connect(self, hotspot: dict[str, str]) -> None:
        """Steps 3-4, in a worker that holds the lock."""
        self.ui(self.goto, 2, "busy", f"Connecting this PC's Wi-Fi ({tx(self.iface)}) to "
                f"[b]{tx(hotspot['ssid'])}[/b]...")
        self.previous = wam.current_profile(self.iface)
        self.connected = True
        self.app.wifi_cleanup = (self.iface, self.previous)
        try:
            wam.connect_hotspot(self.iface, hotspot["ssid"], hotspot["auth"])
        except RuntimeError as exc:
            self.fail(2, "Windows refused the Wi-Fi change:\n\n" + tx(str(exc)))
            return
        self.ip = wam.wait_for_speaker_ip(self.iface, self.port, self.SPEAKER_TIMEOUT,
                                         self.ticker("Waiting for the speaker..."))
        if self.cancelled.is_set():
            return
        if not self.ip:
            self.fail(2, "Connected, but the speaker didn't answer. Try again, or check "
                      "Windows' Wi-Fi status for the speaker network.")
            return
        self.spk_name = wam.speaker_name(self.ip, self.port) or "the speaker"
        self.mac = wam.speaker_mac(self.ip, self.port)
        self.ui(self.goto, 3, "busy", f"Asking [b]{tx(self.spk_name)}[/b] which networks it can "
                "see (takes a few seconds)...")
        self.homes = wam.speaker_networks(self.ip, self.port)
        self.ui(self.show_homes)

    def show_homes(self) -> None:
        options = self.query_one("#homes", OptionList)
        options.clear_options()
        options.add_options([Option(Text(f"{r['ssid']}" + (f"   signal {r['rssi']}" if r.get("rssi") else "")))
                             for r in self.homes])
        options.display = bool(self.homes)
        self.query_one("#ssid", Input).value = self.homes[0]["ssid"] if self.homes else ""
        if self.homes:
            options.highlighted = 0
            text = (f"Talking to [b]{tx(self.spk_name)}[/b]. Which network should it join? "
                    "Pick the 2.4 GHz one if your router has separate names.")
        else:
            text = (f"Talking to [b]{tx(self.spk_name)}[/b]. It didn't send a list of networks, "
                    "so type the name yourself.")
        self.goto(3, "home", text)

    @on(OptionList.OptionHighlighted, "#homes")
    def home_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        self.query_one("#ssid", Input).value = self.homes[event.option_index]["ssid"]

    @on(OptionList.OptionSelected, "#homes")
    @on(Input.Submitted, "#ssid")
    def to_password(self) -> None:
        self.query_one("#password", Input).focus()

    @on(Input.Submitted, "#password")
    @on(Button.Pressed, "#send")
    def send_details(self) -> None:
        target = self.query_one("#ssid", Input).value.strip()
        password = self.query_one("#password", Input).value
        if not target:
            self.notify("Pick or type the network name first.", severity="warning")
            return
        index = self.query_one("#homes", OptionList).highlighted
        picked = self.homes[index] if self.homes and index is not None else None
        if picked and picked["ssid"] == target:
            ssid, index = None, int(picked["index"])
        elif "]]>" in target:
            self.notify("That network name can't be used.", severity="error")
            return
        else:
            ssid, index = target, None
        warnings = wam.password_warnings(password)
        message = f'Send "{target}" to {self.spk_name}?' + "".join("\n\n" + w for w in warnings)

        def confirmed(ok: bool | None) -> None:
            if ok:
                self.query_one("#password", Input).value = ""
                self.goto(4, "busy", f"Sending your Wi-Fi details to [b]{tx(self.spk_name)}[/b]...")
                self.send_worker(target, ssid, index, password)

        self.app.push_screen(ConfirmScreen("Send Wi-Fi details", message, yes="Send"), confirmed)

    @work(thread=True, group="wifi")
    def send_worker(self, target: str, ssid: str | None, index: int | None, password: str) -> None:
        with self.net_lock:
            if self.cancelled.is_set():
                return
            command = wam.join_command(password, ssid, index, "wpa" if password else "none")
            try:
                reply = wam.response_fields(wam.send(self.ip, command, self.port, wait=10.0)).get("result")
            except OSError:
                reply = ""
            self.ui(self.set_status, f"Speaker replied: {reply or 'nothing (normal, it is switching networks)'}. "
                    "Reconnecting this PC's own Wi-Fi...")
            self.disconnect()
            self.ui(self.goto, 5, "busy", f"Waiting for [b]{tx(self.spk_name)}[/b] to show up on "
                    f"your network (up to {self.HOME_TIMEOUT / 60:.0f} minutes)...")
            start = time.monotonic()
            while time.monotonic() - start < self.HOME_TIMEOUT:
                self.ui(self.set_status, f"Searching... {time.monotonic() - start:.0f} s")
                if self.cancelled.wait(self.HOME_POLL):
                    return
                found = wam.find_set_up_speaker(self.spk_name, self.mac, self.port)
                if found:
                    self.result, spk = found
                    ap = wam.response_fields(wam.send(self.result, wam.CMD_GET_AP_INFO, self.port))
                    on_wifi = f' on Wi-Fi "{tx(ap["ssid"])}"' if ap.get("ssid") else ""
                    self.ui(self.goto, 6, "done", f'[green]Done![/green] [b]{tx(spk)}[/b] is at '
                            f"{self.result}{on_wifi}. It's added to your speaker list.")
                    return
            self.ui(self.goto, 5, "done",
                    "I can't see the speaker on the networks I search. If its light has stopped "
                    f'blinking it probably worked: "{tx(target)}" may be a separate network '
                    "(an IoT or guest network/VLAN). Press [b]n[/b] in the speaker list to add that "
                    "network, then [b]s[/b] to scan.\n\nIf the light still blinks, the name or "
                    "password was probably wrong: run the setup again and pick the network from "
                    "the list.", failed=True)

    # ---- leaving --------------------------------------------------------

    @on(Button.Pressed, ".cancel")
    def action_cancel(self) -> None:
        if self.closing:
            return
        self.closing = True
        self.cancelled.set()
        if self.net_lock.locked() or self.connected:
            self.goto(self.step, "busy", "Stopping" + (" and reconnecting this PC's own Wi-Fi..."
                                                       if self.connected else "..."))
        self.close_worker()

    @work(thread=True, group="wifi-close")
    def close_worker(self) -> None:
        with self.net_lock:  # waits for the running step to notice self.cancelled
            self.disconnect()
        self.app.call_from_thread(self.finish)


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
    #wifi { height: 1fr; }
    #steps { width: 38; border: round $accent; padding: 1 1; }
    #wifi-body { padding: 1 2; }
    #wifi-text { margin-bottom: 1; }
    #pane, #pane > Vertical { height: auto; }
    #pane OptionList { height: auto; max-height: 12; margin-bottom: 1; }
    #busy LoadingIndicator { height: 3; }
    #busy-status { color: $text-muted; }
    #pane .buttons { align-horizontal: left; }
    #pane .buttons Button { margin-left: 0; margin-right: 1; }
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
        self.wifi_cleanup: tuple[str, str] | None = None  # set while the PC is on a speaker's hotspot

    MAIN_ACTIONS = {"refresh", "scan", "mark", "group", "ungroup", "volume", "add", "forget",
                    "networks", "wifi"}

    def check_action(self, action: str, parameters: tuple) -> bool | None:
        """Main-screen keys don't act behind a dialog; no quitting mid Wi-Fi setup."""
        if len(self.screen_stack) > 1:
            if action in self.MAIN_ACTIONS:
                return False
            if action == "quit" and any(isinstance(s, WifiSetupScreen) for s in self.screen_stack):
                return False
        return True

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
            group = f'main of "{tx(r.group)}"'
        elif r.role == "sub":
            group = f"member, main is {tx(self.name_of(r.main_ip))}"
        else:
            group = "not grouped"
        state = {None: "checking…", False: "[red]offline[/red]", True: "[green]online[/green]"}[r.online]
        lines = [f"[b]{tx(r.name or r.ip)}[/b]  {state}", "",
                 f"IP        {r.ip}", f"MAC       {tx(r.mac)}", f"Model     {tx(r.model)}"]
        if r.online:
            lines += [f"Firmware  {tx(r.firmware)}",
                      f"Network   {tx(r.ssid)} ({tx(r.conn)})" if r.ssid else f"Network   {tx(r.conn)}",
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
        def done(ip: str | None) -> None:
            if ip:
                self.add_worker(ip)

        self.push_screen(WifiSetupScreen(self.port), done)


def run(port: int = wam.PORT) -> int:
    app = SpeakerApp(port)
    try:
        app.run()
    finally:
        if app.wifi_cleanup:  # closed in the middle of Wi-Fi setup: put this PC's Wi-Fi back
            print("Reconnecting this PC's own Wi-Fi...")
            wam.disconnect_hotspot(*app.wifi_cleanup)
    return 0


if __name__ == "__main__":
    port = wam.PORT
    if len(sys.argv) == 3 and sys.argv[1] == "--port":
        port = int(sys.argv[2])
    sys.exit(run(port))
