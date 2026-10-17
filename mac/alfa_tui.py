#!/usr/bin/env python3
"""ALFA LINK — terminal dashboard for the Mac ⇄ VM ⇄ USB Wi-Fi tunnel.

Mac apps → WireGuard tunnel → UTM Linux VM → USB Wi-Fi (Alfa / D-Link) → Wi-Fi → Internet
"""
from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Grid, Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Footer, Header, Input, Label, RichLog, Sparkline, Static

HOME = Path(__file__).resolve().parent
STATE = Path(os.environ.get("ALFA_LINK_HOME", Path.home() / ".config" / "alfa-link"))
VM_NAME = os.environ.get("ALFA_VM_NAME", "alfa-vm")
VM_IP = (STATE / "vm-ip").read_text().strip() if (STATE / "vm-ip").exists() else "192.168.64.2"
WG_CONF = STATE / "wg" / "alfa.conf"
WG_ADDR = "10.66.0.2"
BREW = str(Path(shutil.which("brew") or "/opt/homebrew/bin/brew").resolve().parent.parent / "bin")
ADAPTER_IDS = {"0E8D:7961": "Alfa AWUS036AXML", "2001:331C": "D-Link DWA-182"}
POLL_SECONDS = 2.5
HISTORY = 60

SSH = [
    "ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=3",
    "-o", "ControlMaster=auto", "-o", f"ControlPath={Path.home()}/.ssh/cm-alfa-%r@%h",
    "-o", "ControlPersist=120", f"alfa@{VM_IP}",
]


# ───────────────────────────── backend helpers ──────────────────────────────
def run(cmd: list[str] | str, timeout: float = 10, stdin: str | None = None) -> tuple[int, str]:
    try:
        p = subprocess.run(cmd, shell=isinstance(cmd, str), capture_output=True, text=True,
                           timeout=timeout, input=stdin)
        return p.returncode, (p.stdout + p.stderr).strip()
    except subprocess.TimeoutExpired:
        return 124, "timed out"
    except FileNotFoundError as e:
        return 127, str(e)


def vm(cmd: str, timeout: float = 10, stdin: str | None = None) -> tuple[int, str]:
    return run(SSH + [cmd], timeout=timeout, stdin=stdin)


def utmctl(*args: str, timeout: float = 20) -> tuple[int, str]:
    return run([f"{BREW}/utmctl", *args], timeout=timeout)


def mac_tunnel_iface() -> str | None:
    """utun interface carrying the tunnel address, if the tunnel is up (no sudo needed)."""
    _, out = run(["ifconfig"], timeout=3)
    cur = None
    for line in out.splitlines():
        if line and not line[0].isspace():
            cur = line.split(":")[0]
        elif cur and f"inet {WG_ADDR} " in line:
            return cur
    return None


def usb_on_mac() -> dict[str, str]:
    """Adapters plugged into the Mac that UTM can pass through: {VID:PID: name}."""
    _, out = utmctl("usb", "list", timeout=8)
    return {vid: name for vid, name in ADAPTER_IDS.items() if vid.lower() in out.lower()}


def sudo_cached() -> bool:
    return run(["sudo", "-n", "true"], timeout=3)[0] == 0


def human_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024:
            return f"{n:,.0f} {unit}" if unit == "B" else f"{n:,.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


def human_rate(bps: float) -> str:
    return human_bytes(bps) + "/s"


def ago(ts: float | None) -> str:
    if not ts:
        return "never"
    s = int(time.time() - ts)
    if s < 60:
        return f"{s}s ago"
    if s < 3600:
        return f"{s // 60}m {s % 60}s ago"
    return f"{s // 3600}h {s % 3600 // 60}m ago"


def duration(sec: float) -> str:
    sec = int(sec)
    d, sec = divmod(sec, 86400)
    h, sec = divmod(sec, 3600)
    m, _ = divmod(sec, 60)
    return (f"{d}d " if d else "") + f"{h}h {m:02d}m"


def signal_quality(dbm: int | None) -> int:
    if dbm is None:
        return 0
    return max(0, min(100, 2 * (dbm + 100)))


def signal_bar(pct: int, width: int = 14) -> Text:
    filled = round(pct / 100 * width)
    color = "green" if pct >= 60 else "yellow" if pct >= 35 else "red"
    t = Text("█" * filled, style=color)
    t.append("░" * (width - filled), style="grey35")
    t.append(f" {pct:>3}%", style=f"bold {color}")
    return t


def band(freq: int | None) -> str:
    if not freq:
        return "—"
    if freq < 3000:
        return f"2.4 GHz · ch {(freq - 2407) // 5 if freq != 2484 else 14}"
    if freq < 5950:
        return f"5 GHz · ch {(freq - 5000) // 5}"
    return f"6 GHz · ch {(freq - 5950) // 5}"


# ──────────────────────────────── state ─────────────────────────────────────
@dataclass
class State:
    vm_state: str = "unknown"
    vm_reachable: bool = False
    vm: dict = field(default_factory=dict)
    mac_tunnel: str | None = None
    usb_mac: dict = field(default_factory=dict)
    public_ip: str | None = None
    public_ip_at: float = 0
    last_rx: tuple[float, int, int] | None = None
    down: float = 0
    up: float = 0


# ─────────────────────────────── widgets ────────────────────────────────────
class Card(Static):
    """A titled panel of key/value rows."""

    def __init__(self, title: str, action: str | None = None, hint: str = "", **kw) -> None:
        super().__init__(**kw)
        self.border_title = title
        self.click_action = action
        if hint:
            self.border_subtitle = hint
            self.tooltip = hint

    async def on_click(self) -> None:
        if self.click_action:
            await self.app.run_action(self.click_action)

    def show(self, rows: list[tuple[str, Text | str]], status: str = "idle") -> None:
        t = Text()
        for i, (k, v) in enumerate(rows):
            if i:
                t.append("\n")
            t.append(f"{k:<11}", style="grey62")
            t.append(v if isinstance(v, Text) else Text(str(v), style="bold"))
        self.update(t)
        self.set_class(status == "ok", "ok")
        self.set_class(status == "warn", "warn")
        self.set_class(status == "bad", "bad")


class Chart(Vertical):
    def __init__(self, title: str, color: str, **kw) -> None:
        super().__init__(**kw)
        self.border_title = title
        self.color = color
        self.data: deque[float] = deque([0.0] * HISTORY, maxlen=HISTORY)

    def compose(self) -> ComposeResult:
        yield Label("—", classes="chart-value")
        yield Sparkline(list(self.data), min_color="#3a3a3a", max_color=self.color)

    def push(self, value: float | None, label: str) -> None:
        self.data.append(value or 0.0)
        self.query_one(Sparkline).data = list(self.data)
        self.query_one(".chart-value", Label).update(label)


# ─────────────────────────────── modals ─────────────────────────────────────
class PasswordScreen(ModalScreen[str | None]):
    BINDINGS = [Binding("escape", "dismiss(None)", "Cancel")]

    def __init__(self, title: str, prompt: str) -> None:
        super().__init__()
        self.title_text, self.prompt = title, prompt

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(self.title_text, id="dialog-title")
            yield Label(self.prompt, classes="muted")
            yield Input(password=True, placeholder="password", id="pw")
            with Horizontal(classes="buttons"):
                yield Button("Cancel", id="cancel")
                yield Button("OK", variant="primary", id="ok")

    @on(Input.Submitted)
    def submit(self, event: Input.Submitted) -> None:
        self.dismiss(event.value)

    @on(Button.Pressed, "#ok")
    def ok(self) -> None:
        self.dismiss(self.query_one("#pw", Input).value)

    @on(Button.Pressed, "#cancel")
    def cancel(self) -> None:
        self.dismiss(None)


class ConfirmScreen(ModalScreen[bool]):
    BINDINGS = [Binding("y,enter", "dismiss(True)", "Yes"), Binding("n,escape", "dismiss(False)", "No")]

    def __init__(self, question: str) -> None:
        super().__init__()
        self.question = question

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label("Confirm", id="dialog-title")
            yield Label(self.question)
            with Horizontal(classes="buttons"):
                yield Button("No", id="no")
                yield Button("Yes", variant="warning", id="yes")

    @on(Button.Pressed, "#yes")
    def yes(self) -> None:
        self.dismiss(True)

    @on(Button.Pressed, "#no")
    def no(self) -> None:
        self.dismiss(False)


class HotspotScreen(ModalScreen[tuple[str, str] | None]):
    """Hotspot name + password. Returns (name, password) or None."""

    BINDINGS = [Binding("escape", "dismiss(None)", "Cancel")]

    def __init__(self, name: str = "AlfaLink") -> None:
        super().__init__()
        self.default_name = name

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label("Hotspot settings", id="dialog-title")
            yield Label("Devices join this network to share the adapter's internet.", classes="muted")
            yield Label("Name")
            yield Input(value=self.default_name, id="hs-name", max_length=32)
            yield Label("Password (8–63 characters, stored only in the VM)")
            yield Input(password=True, id="hs-pw", placeholder="password")
            yield Input(password=True, id="hs-pw2", placeholder="repeat password")
            yield Label("", id="hs-error")
            with Horizontal(classes="buttons"):
                yield Button("Cancel", id="cancel")
                yield Button("Start hotspot", variant="success", id="ok")

    def on_mount(self) -> None:
        self.query_one("#hs-pw", Input).focus()

    def _submit(self) -> None:
        name = self.query_one("#hs-name", Input).value.strip()
        pw, pw2 = self.query_one("#hs-pw", Input).value, self.query_one("#hs-pw2", Input).value
        err = self.query_one("#hs-error", Label)
        if not name:
            err.update(Text("Enter a name", style="red"))
        elif not 8 <= len(pw) <= 63:
            err.update(Text("Password must be 8–63 characters", style="red"))
        elif pw != pw2:
            err.update(Text("Passwords don't match", style="red"))
        else:
            self.dismiss((name, pw))

    @on(Input.Submitted)
    def submitted(self, event: Input.Submitted) -> None:
        nxt = {"hs-name": "#hs-pw", "hs-pw": "#hs-pw2"}.get(event.input.id or "")
        if nxt:
            self.query_one(nxt, Input).focus()
        else:
            self._submit()

    @on(Button.Pressed, "#ok")
    def ok(self) -> None:
        self._submit()

    @on(Button.Pressed, "#cancel")
    def cancel(self) -> None:
        self.dismiss(None)


BANDS = {"all": "All", "2.4": "2.4 GHz", "5": "5 GHz", "6": "6 GHz"}


def band_of(freq: int) -> str | None:
    if not freq:
        return None
    return "2.4" if freq < 3000 else "5" if freq < 5950 else "6"


class WifiScreen(ModalScreen[tuple[str, bool, bool] | None]):
    """Pick a Wi-Fi network for the USB adapter. Returns (ssid, needs_password, saved)."""

    BINDINGS = [
        Binding("escape", "dismiss(None)", "Close"),
        Binding("r", "rescan", "Rescan"),
        Binding("f", "forget", "Forget network"),
        Binding("b", "cycle_band", "Band filter"),
        Binding("0", "band('all')", "All bands", show=False),
        Binding("2", "band('2.4')", "2.4 GHz", show=False),
        Binding("5", "band('5')", "5 GHz", show=False),
        Binding("6", "band('6')", "6 GHz", show=False),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.data: dict = {}
        self.all_networks: list[dict] = []
        self.networks: list[dict] = []      # rows currently shown (after the band filter)
        self.band = "all"
        self.cached = True

    def compose(self) -> ComposeResult:
        with Vertical(id="wifi"):
            yield Label("Wi-Fi networks seen by the USB adapter", id="dialog-title")
            with Horizontal(id="bands"):
                yield Label("Band ", classes="muted band-label")
                for key, name in BANDS.items():
                    yield Button(name, id=f"band-{key.replace('.', '')}", compact=True,
                                 classes="band" + (" -active" if key == "all" else ""))
            yield Label("Scanning…", id="wifi-status", classes="muted")
            yield DataTable(id="wifi-table", cursor_type="row", zebra_stripes=True)
            with Horizontal(classes="buttons"):
                yield Button("Close", id="close")
                yield Button("Rescan", id="rescan")
                yield Button("Forget", variant="error", id="forget")
                yield Button("Connect", variant="success", id="connect")
            yield Label("Click a row to select · click again or Enter to connect · b/0/2/5/6 band · f forget · r rescan",
                        classes="hint")

    def on_mount(self) -> None:
        t = self.query_one(DataTable)
        t.add_columns(" ", "Network", "Signal", "Band", "Security", "Saved")
        self.scan(cached=True)
        self.scan(cached=False)
        t.focus()

    # scanning
    def action_rescan(self) -> None:
        self.query_one("#wifi-status", Label).update("Scanning…")
        self.scan(cached=False)

    def scan(self, cached: bool) -> None:
        # cached listing is instant; a fresh scan takes a few seconds — run them independently
        (self._scan_cached if cached else self._scan_fresh)()

    @work(thread=True, exclusive=True, group="scan-cached")
    def _scan_cached(self) -> None:
        self._scan(True)

    @work(thread=True, exclusive=True, group="scan-fresh")
    def _scan_fresh(self) -> None:
        self._scan(False)

    def _scan(self, cached: bool) -> None:
        code, out = vm("alfa-scan" + (" --cached" if cached else ""), timeout=30)
        try:
            data = json.loads(out.splitlines()[-1])
        except Exception:
            data = {"error": out or f"exit {code}"}
        self.app.call_from_thread(self.fill, data, cached)

    def fill(self, data: dict, cached: bool) -> None:
        if "error" in data:
            self.query_one("#wifi-status", Label).update(
                Text(f"✖ {data['error']} — plug the adapter in and press a on the dashboard", style="red"))
            return
        self.data, self.cached = data, cached
        self.all_networks = data["networks"]
        self.render_rows()

    # band filter
    def action_band(self, band: str) -> None:
        self.band = band
        for key in BANDS:
            self.query_one(f"#band-{key.replace('.', '')}", Button).set_class(key == band, "-active")
        self.render_rows()

    def action_cycle_band(self) -> None:
        keys = list(BANDS)
        self.action_band(keys[(keys.index(self.band) + 1) % len(keys)])

    @on(Button.Pressed, ".band")
    def band_clicked(self, event: Button.Pressed) -> None:
        key = event.button.id.removeprefix("band-")
        self.action_band("2.4" if key == "24" else key)

    def render_rows(self) -> None:
        t = self.query_one(DataTable)
        selected = self.networks[t.cursor_row]["ssid"] if self.networks and t.cursor_row < len(self.networks) else None
        self.networks = [n for n in self.all_networks
                         if self.band == "all" or band_of(n["freq"]) == self.band]
        t.clear()
        for n in self.networks:
            away = n.get("out_of_range")
            t.add_row(
                Text("●", style="bold green") if n["active"] else "",
                Text(n["ssid"], style="bold" if n["active"] else "grey50" if away else ""),
                Text("out of range", style="grey50") if away else signal_bar(n["signal"], 10),
                "" if away else band(n["freq"]).split(" ·")[0],
                Text("—", style="grey50") if away else Text(n["security"] or "open", style="yellow" if not n["security"] else ""),
                Text("✓", style="green") if n["saved"] else "",
            )
        if self.networks:
            row = next((i for i, n in enumerate(self.networks) if n["ssid"] == selected), 0)
            t.move_cursor(row=row)
        counts = {b: sum(1 for n in self.all_networks if band_of(n["freq"]) == b) for b in ("2.4", "5", "6")}
        summary = " · ".join(f"{BANDS[b]}: {c}" for b, c in counts.items() if c or b != "6")
        shown = f"{len(self.networks)} shown" if self.band != "all" else f"{len(self.networks)} networks"
        self.query_one("#wifi-status", Label).update(
            f"{shown} via {self.data.get('iface', '?')}  ({summary})" + ("  — scanning…" if self.cached else ""))

    # selection
    def current(self) -> dict | None:
        t = self.query_one(DataTable)
        return self.networks[t.cursor_row] if self.networks and t.cursor_row < len(self.networks) else None

    def choose(self, n: dict | None) -> None:
        if not n:
            return
        if n.get("out_of_range"):
            self.query_one("#wifi-status", Label).update(
                Text(f"“{n['ssid']}” is out of range — can't connect (Forget removes it)", style="yellow"))
            return
        self.dismiss((n["ssid"], bool(n["security"]) and not n["saved"], n["saved"]))

    @on(DataTable.RowSelected)
    def pick(self, event: DataTable.RowSelected) -> None:
        self.choose(self.networks[event.cursor_row])

    @on(Button.Pressed, "#connect")
    def connect_clicked(self) -> None:
        self.choose(self.current())

    @on(Button.Pressed, "#rescan")
    def rescan_clicked(self) -> None:
        self.action_rescan()

    @on(Button.Pressed, "#forget")
    def forget_clicked(self) -> None:
        self.action_forget()

    @on(Button.Pressed, "#close")
    def close_clicked(self) -> None:
        self.dismiss(None)

    # forget
    def action_forget(self) -> None:
        n = self.current()
        if not n:
            return
        status = self.query_one("#wifi-status", Label)
        if not n["saved"]:
            status.update(Text(f"“{n['ssid']}” has no saved password — nothing to forget", style="yellow"))
            return
        q = f"Forget “{n['ssid']}” and its saved password?"
        if n["active"]:
            q += " The adapter is connected to it and will disconnect" + (
                " — the Mac loses internet while the tunnel is on." if self.app.state.mac_tunnel else ".")
        self.app.push_screen(ConfirmScreen(q), lambda yes: yes and self.forget(n["ssid"]))

    @work(thread=True, exclusive=True, group="forget")
    def forget(self, ssid: str) -> None:
        code, out = vm(f"sudo nmcli con delete id {shlex.quote(ssid)}", timeout=20)
        if code == 0:
            self.app.call_from_thread(self.app.log_event, f"Forgot “{ssid}” (saved password deleted)", "ok")
        else:
            self.app.call_from_thread(self.app.log_event, f"Could not forget “{ssid}”: {out.splitlines()[-1] if out else code}", "bad")
        self.app.call_from_thread(self.scan, True)
        self.app.call_from_thread(self.app.poll)


# ──────────────────────────────── app ───────────────────────────────────────
class AlfaLink(App):
    TITLE = "ALFA LINK"
    SUB_TITLE = "Mac ⇄ VM ⇄ USB Wi-Fi"
    CSS = """
    Screen { background: $surface; }
    #route { height: 3; content-align: center middle; border: round $primary-darken-2; margin: 0 1; }
    #actions { height: 3; margin: 0 1; }
    #actions Button { margin-right: 1; min-width: 14; }
    Card { }
    Card:hover { background: $boost; }
    #cards { grid-size: 4; grid-gutter: 0 1; height: 9; margin: 0 1; }
    Card { border: round #5f5f5f; padding: 0 1; height: 100%; }
    Card.ok { border: round green; }
    Card.warn { border: round yellow; }
    Card.bad { border: round red; }
    #charts { height: 7; margin: 0 1; }
    Chart { border: round #5f5f5f; padding: 0 1; width: 1fr; }
    Chart Sparkline { height: 1fr; }
    #charts Card { width: 1fr; }
    #hs-error { height: 1; }
    .chart-value { text-style: bold; }
    #log { border: round #5f5f5f; margin: 0 1; height: 1fr; }
    #dialog, #wifi { background: $panel; border: thick $primary; padding: 1 2; width: 64; height: auto; }
    #wifi { width: 90; height: 80%; }
    #wifi DataTable { height: 1fr; }
    ModalScreen { align: center middle; background: $background 70%; }
    #dialog-title { text-style: bold; color: $accent; margin-bottom: 1; }
    .buttons { height: 3; align-horizontal: right; margin-top: 1; }
    .buttons Button { margin-left: 1; }
    #bands { height: 1; margin-bottom: 1; }
    #bands .band-label { padding-top: 0; }
    .band { margin-right: 1; min-width: 9; }
    .band.-active { background: $accent; color: $text; text-style: bold; }
    .muted { color: $text-muted; }
    .hint { color: $text-muted; margin-top: 1; text-style: italic; }
    """
    BINDINGS = [
        Binding("w", "wifi", "Wi-Fi"),
        Binding("t", "tunnel", "Tunnel on/off"),
        Binding("h", "hotspot", "Hotspot on/off"),
        Binding("H", "hotspot_settings", "Hotspot settings", show=False),
        Binding("a", "attach", "Attach adapter"),
        Binding("s", "vm_power", "Start/stop VM"),
        Binding("p", "public_ip", "Public IP"),
        Binding("r", "refresh", "Refresh"),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.state = State()
        self.busy: set[str] = set()
        self.hotspot_pending: str | None = None

    # layout
    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Static(id="route")
        with Horizontal(id="actions"):
            yield Button("Wi-Fi", id="b-wifi", variant="primary", action="app.wifi")
            yield Button("Tunnel ON", id="b-tunnel", variant="success", action="app.tunnel")
            yield Button("Hotspot ON", id="b-hotspot", action="app.hotspot")
            yield Button("Attach adapter", id="b-attach", action="app.attach")
            yield Button("Stop VM", id="b-vm", action="app.vm_power")
            yield Button("Refresh", id="b-refresh", action="app.refresh")
            yield Button("Quit", id="b-quit", variant="error", action="app.quit")
        with Grid(id="cards"):
            yield Card("VM", id="c-vm")
            yield Card("Adapter", action="app.wifi", hint="click → Wi-Fi", id="c-adapter")
            yield Card("Tunnel", action="app.tunnel", hint="click → on/off", id="c-tunnel")
            yield Card("Internet", action="app.public_ip", hint="click → refresh IP", id="c-net")
        with Horizontal(id="charts"):
            yield Chart("Latency", "cyan", id="ch-lat")
            yield Chart("Download", "green", id="ch-down")
            yield Chart("Upload", "magenta", id="ch-up")
            yield Card("Hotspot", action="app.hotspot", hint="click → on/off", id="c-hot")
        log = RichLog(id="log", markup=True, wrap=True)
        log.border_title = "Events"
        yield log
        yield Footer()

    def on_mount(self) -> None:
        self.log_event("Dashboard started. [b]w[/] Wi-Fi · [b]t[/] tunnel · [b]a[/] attach adapter · [b]s[/] VM power")
        self.poll()
        self.set_interval(POLL_SECONDS, self.poll)
        self.set_interval(60, self.action_public_ip)
        self.set_interval(240, self.keep_sudo)
        self.action_public_ip()

    def log_event(self, msg: str, level: str = "info") -> None:
        style = {"info": "grey70", "ok": "green", "warn": "yellow", "bad": "red"}[level]
        icon = {"info": "·", "ok": "✔", "warn": "!", "bad": "✖"}[level]
        self.query_one(RichLog).write(Text.from_markup(
            f"[grey50]{time.strftime('%H:%M:%S')}[/] [{style}]{icon}[/] {msg}"))

    # polling
    @work(thread=True, exclusive=True, group="poll")
    def poll(self) -> None:
        s = self.state
        code, out = utmctl("status", VM_NAME, timeout=8)
        s.vm_state = out.splitlines()[-1].strip() if code == 0 and out else "unknown"
        s.mac_tunnel = mac_tunnel_iface()
        info: dict = {}
        if s.vm_state == "started":
            code, out = vm("alfa-status", timeout=8)
            try:
                info = json.loads(out.splitlines()[-1])
                s.vm_reachable = True
            except Exception:
                s.vm_reachable = False
        else:
            s.vm_reachable = False
        if not info.get("iface"):
            s.usb_mac = usb_on_mac() if s.vm_state == "started" else {}
        s.vm = info
        # throughput from adapter byte counters
        if "rx_bytes" in info:
            now = info["time"]
            if s.last_rx and now > s.last_rx[0]:
                dt = now - s.last_rx[0]
                s.down = max(0, (info["rx_bytes"] - s.last_rx[1]) / dt)
                s.up = max(0, (info["tx_bytes"] - s.last_rx[2]) / dt)
            s.last_rx = (now, info["rx_bytes"], info["tx_bytes"])
        else:
            s.last_rx, s.down, s.up = None, 0, 0
        self.call_from_thread(self.render_state)

    def render_state(self) -> None:
        s, v = self.state, self.state.vm
        wg = v.get("wg", {})
        hs = wg.get("handshake") or 0
        tunnel_alive = bool(s.mac_tunnel) and hs and time.time() - hs < 180
        vm_ok = s.vm_state == "started" and s.vm_reachable
        adapter_ok = bool(v.get("iface"))
        wifi_ok = bool(v.get("ssid"))
        net_ok = v.get("ping_ms") is not None

        # route bar
        def node(label: str, ok: bool | None) -> Text:
            style = "bold green" if ok else "bold red" if ok is False else "grey50"
            return Text(f" {label} ", style=style)
        arrow = Text(" ━▶ ", style="grey42")
        route = Text()
        parts = [
            ("Mac", True),
            ("Tunnel" + ("" if s.mac_tunnel else " (off)"), bool(tunnel_alive) if s.mac_tunnel else None),
            ("VM", vm_ok),
            (v.get("adapter") or "No adapter", adapter_ok if vm_ok else None),
            (v.get("ssid") or "No Wi-Fi", wifi_ok if adapter_ok else None),
            ("Internet", net_ok if wifi_ok else None),
        ]
        for i, (lbl, ok) in enumerate(parts):
            if i:
                route.append_text(arrow)
            route.append_text(node(lbl, ok))
        if tunnel_alive and net_ok:
            headline = Text("●  MAC IS ONLINE THROUGH THE USB ADAPTER   ", style="bold green")
        elif net_ok:
            headline = Text("●  VM ONLINE · Mac on its own Wi-Fi (tunnel off)   ", style="bold yellow")
        else:
            headline = Text("●  NOT CONNECTED   ", style="bold red")
        headline.append_text(route)
        hs_state = v.get("hotspot") or {}
        if hs_state.get("on"):
            n = len(hs_state.get("clients", []))
            headline.append(f"   ⦿ Hotspot “{hs_state.get('ssid')}” · {n} device{'s' if n != 1 else ''}", style="bold cyan")
        self.query_one("#route", Static).update(headline)

        # VM card
        vm_rows = [("State", Text(s.vm_state, style="bold green" if vm_ok else "bold red")),
                   ("Address", VM_IP),
                   ("Uptime", duration(v["uptime"]) if "uptime" in v else "—"),
                   ("Service", Text("wg0 listening", style="green") if wg.get("up") else Text("wg0 down", style="red"))]
        self.query_one("#c-vm", Card).show(vm_rows, "ok" if vm_ok else "bad")

        # Adapter card
        if adapter_ok:
            q = signal_quality(v.get("signal"))
            rows = [("Device", v.get("adapter") or v["iface"]),
                    ("Network", Text(v.get("ssid") or "not connected", style="bold" if wifi_ok else "yellow")),
                    ("Signal", signal_bar(q) if wifi_ok else "—"),
                    ("dBm", f"{v.get('signal')} dBm" if wifi_ok else "—"),
                    ("Band", band(v.get("freq"))),
                    ("Link", "↓ {}  ↑ {}".format(*[(v.get(k) or "—").replace(" MBit/s", " Mb/s") for k in ("rx_rate", "tx_rate")]))]
            status = "ok" if wifi_ok and q >= 35 else "warn"
        else:
            plugged = ", ".join(s.usb_mac.values())
            rows = [("Device", Text("not in VM", style="bold red")),
                    ("On Mac", Text(plugged + " — press a", style="yellow") if plugged else Text("nothing plugged in", style="red"))]
            status = "bad"
        self.query_one("#c-adapter", Card).show(rows, status)

        # Tunnel card
        rows = [("Mac side", Text(f"up ({s.mac_tunnel})", style="bold green") if s.mac_tunnel else Text("off", style="bold yellow")),
                ("Handshake", Text(ago(hs) if hs else "never", style="green" if tunnel_alive else "grey62")),
                ("To Mac", human_bytes(wg.get("tx", 0))),
                ("From Mac", human_bytes(wg.get("rx", 0)))]
        self.query_one("#c-tunnel", Card).show(rows, "ok" if tunnel_alive else "warn" if not s.mac_tunnel else "bad")

        # Internet card
        lat = v.get("ping_ms")
        rows = [("Status", Text("online", style="bold green") if net_ok else Text("offline", style="bold red")),
                ("Latency", f"{lat:.0f} ms" if lat is not None else "—"),
                ("Gateway", v.get("gateway") or "—"),
                ("Public IP", s.public_ip or "—"),
                ("Traffic", f"↓ {human_rate(s.down)}  ↑ {human_rate(s.up)}")]
        self.query_one("#c-net", Card).show(rows, "ok" if net_ok else "bad")

        hs = v.get("hotspot") or {}
        clients = hs.get("clients", [])
        if self.hotspot_pending:
            rows = [("State", Text(self.hotspot_pending, style="bold cyan")),
                    ("Name", hs.get("ssid") or "—")]
            hstatus = "warn"
        elif hs.get("on"):
            rows = [("State", Text("on", style="bold green")),
                    ("Name", hs.get("ssid") or "—"),
                    ("Channel", str(hs.get("channel") or "—")),
                    ("Devices", Text(str(len(clients)), style="bold cyan"))]
            for c in clients[:2]:
                label = c.get("name") or c.get("ip") or c["mac"]
                rows.append(("", Text(f"• {label}" + (f"  {c['signal']} dBm" if c.get("signal") else ""), style="grey70")))
            hstatus = "ok"
        else:
            rows = [("State", Text("off", style="bold yellow")),
                    ("Name", hs.get("ssid") or Text("not set up — press h", style="grey62"))]
            hstatus = "warn" if hs.get("configured") else "idle"
        self.query_one("#c-hot", Card).show(rows, hstatus)
        hb = self.query_one("#b-hotspot", Button)
        hb.label = "Hotspot OFF" if hs.get("on") else "Hotspot ON"
        hb.disabled = bool(self.hotspot_pending) or (not wifi_ok and not hs.get("on"))

        tb = self.query_one("#b-tunnel", Button)
        tb.label = "Tunnel OFF" if s.mac_tunnel else "Tunnel ON"
        tb.variant = "warning" if s.mac_tunnel else "success"
        self.query_one("#b-vm", Button).label = "Stop VM" if s.vm_state == "started" else "Start VM"
        self.query_one("#b-attach", Button).disabled = adapter_ok or s.vm_state != "started"

        self.query_one("#ch-lat", Chart).push(lat, f"{lat:.0f} ms" if lat is not None else "no reply")
        self.query_one("#ch-down", Chart).push(s.down, human_rate(s.down))
        self.query_one("#ch-up", Chart).push(s.up, human_rate(s.up))

    # actions
    def action_refresh(self) -> None:
        self.poll()
        self.log_event("Refreshed")

    @work(thread=True, exclusive=True, group="ip")
    def action_public_ip(self) -> None:
        if not self.state.vm_reachable and self.state.vm_state != "started":
            return
        code, out = vm("curl -s --max-time 8 https://ifconfig.me", timeout=12)
        if code == 0 and re.fullmatch(r"[\d.:a-fA-F]+", out or ""):
            self.state.public_ip = out

    def action_wifi(self) -> None:
        if not self.state.vm.get("iface"):
            self.log_event("No USB Wi-Fi adapter in the VM — press [b]a[/] to attach it first", "warn")
            return

        def picked(result: tuple[str, bool, bool] | None) -> None:
            if not result:
                return
            ssid, needs_pw, saved = result
            if needs_pw:
                self.push_screen(PasswordScreen(f"Join “{ssid}”", "Wi-Fi password (stays inside the VM):"),
                                 lambda pw: pw is not None and self.connect_wifi(ssid, pw))
            else:
                self.connect_wifi(ssid, "")
        self.push_screen(WifiScreen(), picked)

    @work(thread=True, exclusive=True, group="wifi")
    def connect_wifi(self, ssid: str, password: str) -> None:
        self.call_from_thread(self.log_event, f"Connecting adapter to [b]{ssid}[/]…")
        code, out = vm(f"alfa-connect {shlex.quote(ssid)}", timeout=60, stdin=password + "\n")
        password = ""
        last = out.splitlines()[-1] if out else ""
        if code == 0:
            self.call_from_thread(self.log_event, f"Online via [b]{ssid}[/]", "ok")
        elif code == 4:
            self.call_from_thread(self.log_event, f"Joined {ssid} but it has no internet", "warn")
        else:
            self.call_from_thread(self.log_event, f"Could not join {ssid}: {last or 'error'} (wrong password?)", "bad")
        self.call_from_thread(self.poll)

    def action_attach(self) -> None:
        self.attach_adapter()

    @work(thread=True, exclusive=True, group="usb")
    def attach_adapter(self) -> None:
        found = usb_on_mac()
        if not found:
            self.call_from_thread(self.log_event, "No Alfa / D-Link plugged into the Mac — plug it into a USB port", "bad")
            return
        for vid, name in found.items():
            code, out = utmctl("usb", "connect", VM_NAME, vid, timeout=20)
            if code == 0 or "already connected" in out:
                self.call_from_thread(self.log_event, f"Attached {name} to the VM", "ok")
            else:
                self.call_from_thread(self.log_event, f"Attach {name} failed: {out}", "bad")
        time.sleep(6)
        self.call_from_thread(self.poll)

    def action_vm_power(self) -> None:
        if self.state.vm_state == "started":
            q = "Stop the VM?" + (" The tunnel is on — the Mac will lose internet until you turn it off." if self.state.mac_tunnel else "")
            self.push_screen(ConfirmScreen(q), lambda yes: yes and self.vm_power(False))
        else:
            self.vm_power(True)

    @work(thread=True, exclusive=True, group="power")
    def vm_power(self, start: bool) -> None:
        self.call_from_thread(self.log_event, "Starting VM…" if start else "Stopping VM…")
        code, out = utmctl("start" if start else "stop", VM_NAME, timeout=60)
        if code != 0:
            self.call_from_thread(self.log_event, f"VM {'start' if start else 'stop'} failed: {out}", "bad")
            return
        if start:
            for _ in range(40):
                if vm("true", timeout=4)[0] == 0:
                    break
                time.sleep(2)
            self.call_from_thread(self.log_event, "VM is up", "ok")
            self.call_from_thread(self.attach_adapter)
        else:
            self.call_from_thread(self.log_event, "VM stopped", "ok")
        self.call_from_thread(self.poll)

    # hotspot (runs in the VM on the adapter's radio)
    def action_hotspot(self) -> None:
        if "hotspot" in self.busy:
            self.log_event("Hotspot is still changing — one moment", "warn")
            return
        hs = self.state.vm.get("hotspot") or {}
        if hs.get("on"):
            n = len(hs.get("clients", []))
            if n:
                self.push_screen(ConfirmScreen(f"Turn off the hotspot? {n} device{'s' if n != 1 else ''} will be disconnected."),
                                 lambda yes: yes and self.hotspot_cmd("off"))
            else:
                self.hotspot_cmd("off")
            return
        if not self.state.vm.get("ssid"):
            self.log_event("Connect the adapter to a Wi-Fi network first ([b]w[/]) — the hotspot shares that connection", "warn")
            return
        if hs.get("configured"):
            self.hotspot_cmd("on")
        else:
            self.action_hotspot_settings()

    def action_hotspot_settings(self) -> None:
        if not self.state.vm.get("ssid"):
            self.log_event("Connect the adapter to a Wi-Fi network first ([b]w[/])", "warn")
            return
        name = (self.state.vm.get("hotspot") or {}).get("ssid") or "AlfaLink"
        self.push_screen(HotspotScreen(name), lambda r: r and self.hotspot_cmd("on", r[0], r[1]))

    @work(thread=True, exclusive=True, group="hotspot")
    def hotspot_cmd(self, cmd: str, name: str | None = None, password: str = "") -> None:
        self.busy.add("hotspot")
        self.hotspot_pending = "starting…" if cmd == "on" else "stopping…"
        self.call_from_thread(self.render_state)
        try:
            self._hotspot_cmd(cmd, name, password)
        finally:
            self.busy.discard("hotspot")
            self.hotspot_pending = None
            self.call_from_thread(self.poll)

    def _hotspot_cmd(self, cmd: str, name: str | None, password: str) -> None:
        if cmd == "on":
            self.call_from_thread(self.log_event, "Starting hotspot…")
            if password:
                vm(f"alfa-hotspot off", timeout=20)
                vm("sudo nmcli con delete alfa-hotspot", timeout=20)   # new name/password → fresh profile
            arg = f" {shlex.quote(name)}" if name else ""
            code, out = vm(f"alfa-hotspot on{arg}", timeout=60, stdin=password + "\n")
        else:
            self.call_from_thread(self.log_event, "Stopping hotspot…")
            code, out = vm("alfa-hotspot off", timeout=30)
        password = ""
        if code == 0:
            msg = "Hotspot on — devices can join now" if cmd == "on" else "Hotspot off"
            self.call_from_thread(self.log_event, msg, "ok")
        else:
            self.call_from_thread(self.log_event, f"Hotspot {cmd} failed: {out.splitlines()[-1] if out else code}", "bad")

    # tunnel (needs sudo)
    def action_tunnel(self) -> None:
        turning_on = not self.state.mac_tunnel
        if turning_on and self.state.vm.get("ping_ms") is None:
            self.log_event("The VM's USB Wi-Fi has no internet — connect it first ([b]w[/]). Tunnel not started.", "warn")
            return
        if sudo_cached():
            self.tunnel(turning_on)
            return

        def got(pw: str | None) -> None:
            if pw is None:
                return
            self.unlock_sudo(pw, turning_on)
        self.push_screen(PasswordScreen("Administrator password", "Your Mac password (for the tunnel network settings):"), got)

    @work(thread=True, exclusive=True, group="sudo")
    def unlock_sudo(self, pw: str, turning_on: bool) -> None:
        code, _ = run(["sudo", "-S", "-p", "", "-v"], timeout=10, stdin=pw + "\n")
        pw = ""
        if code != 0:
            self.call_from_thread(self.log_event, "Wrong Mac password", "bad")
            return
        self.call_from_thread(self.tunnel, turning_on)

    def keep_sudo(self) -> None:
        if self.state.mac_tunnel:
            run(["sudo", "-n", "-v"], timeout=3)

    @work(thread=True, exclusive=True, group="tunnel")
    def tunnel(self, on_: bool) -> None:
        wgq = ["sudo", "-n", f"{BREW}/bash", f"{BREW}/wg-quick"]
        if on_:
            self.call_from_thread(self.log_event, "Starting tunnel…")
            run(wgq + ["down", str(WG_CONF)], timeout=15)
            code, out = run(wgq + ["up", str(WG_CONF)], timeout=30)
            if code == 0:
                self.call_from_thread(self.log_event, "Tunnel up — all Mac traffic now goes through the USB adapter", "ok")
            else:
                self.call_from_thread(self.log_event, f"Tunnel failed: {out.splitlines()[-1] if out else code}", "bad")
        else:
            self.call_from_thread(self.log_event, "Stopping tunnel…")
            code, out = run(wgq + ["down", str(WG_CONF)], timeout=30)
            if code == 0:
                self.call_from_thread(self.log_event, "Tunnel off — Mac is back on its own Wi-Fi", "ok")
            else:
                self.call_from_thread(self.log_event, f"Tunnel stop failed: {out.splitlines()[-1] if out else code}", "bad")
        time.sleep(2)
        self.call_from_thread(self.poll)
        self.call_from_thread(self.action_public_ip)


if __name__ == "__main__":
    os.chdir(HOME)
    AlfaLink().run()
