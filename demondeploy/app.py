"""Textual TUI: setup → dashboard → app details / new app / deploy / logs / env / domains."""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Callable

from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Center, Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen, Screen
from textual.widgets import (Button, DataTable, Footer, Header, Input, Label, LoadingIndicator,
                             OptionList, RichLog, Select, Static, Switch)
from textual.widgets.option_list import Option

from . import __version__, deploy, domains, gitsrc, paths, proxy, setup, sysd
from .detect import Plan, detect
from .model import (ACTIVE, CANCELLED, FAILED, KINDS, NAME_RE, STATIC, STEPS, SUPERSEDED, Project,
                    ago, human_size, human_time, slugify)
from .proc import DemonError
from .state import State

# status → (label, fg, bg)
PILLS = {
    "running": ("● RUNNING", "#282a36", "#50fa7b"),
    "deploying": ("◐ DEPLOYING", "#282a36", "#bd93f9"),
    "restarting": ("◐ RESTARTING", "#282a36", "#f1fa8c"),
    "stopped": ("■ STOPPED", "#f8f8f2", "#44475a"),
    "failed": ("✗ FAILED", "#f8f8f2", "#ff5555"),
    "crashed": ("✗ CRASHED", "#f8f8f2", "#ff5555"),
    "new": ("○ NEW", "#f8f8f2", "#44475a"),
    "live": ("● LIVE", "#282a36", "#50fa7b"),
    "cancelled": ("■ CANCELLED", "#f8f8f2", "#44475a"),
}
DOT = {"running": ("●", "#50fa7b"), "deploying": ("◐", "#bd93f9"), "restarting": ("◐", "#f1fa8c"),
       "stopped": ("■", "#6272a4"), "failed": ("✗", "#ff5555"), "crashed": ("✗", "#ff5555"),
       "new": ("○", "#6272a4")}
DEP_COLORS = {ACTIVE: "#50fa7b", SUPERSEDED: "#6272a4", FAILED: "#ff5555", CANCELLED: "#6272a4",
              "building": "#bd93f9", "starting": "#bd93f9", "queued": "#bd93f9"}


def pill(status: str) -> Text:
    label, fg, bg = PILLS.get(status, (status.upper(), "#f8f8f2", "#44475a"))
    return Text(f" {label} ", style=f"bold {fg} on {bg}")


def status_dot(status: str) -> Text:
    ch, color = DOT.get(status, ("?", "#6272a4"))
    return Text(f"{ch} {status}", style=color)


def log_text(line: str) -> Text:
    if line.startswith("==> "):
        return Text(line, style="bold #bd93f9")
    if line.startswith("✓ "):
        return Text(line, style="#50fa7b")
    if line.startswith("!! "):
        return Text(line, style="bold #ff5555")
    return Text(line)


_JOURNAL = re.compile(r"^\d{4}-\d\d-\d\dT(\d\d:\d\d:\d\d)\S*\s+\S+\s+([^\[:]+)(?:\[\d+\])?:\s?(.*)$")


def format_journal_line(line: str) -> Text:
    """'2026-…T18:41:27+05:30 host ident[pid]: msg' → '18:41:27 ident msg' (no hostname)."""
    m = _JOURNAL.match(line)
    if not m:
        return Text(line, style="#6272a4")
    clock, ident, msg = m.groups()
    bad = any(w in msg.lower() for w in ("error", "exception", "traceback", "fatal"))
    style = "#ff5555" if bad else ("#6272a4" if ident == "systemd" else "#f8f8f2")
    return Text.assemble((clock + " ", "#6272a4"), (f"{ident[:16]:16} ", "#bd93f9"), (msg, style))


def app_status(st: State, p: Project) -> str:
    from .__main__ import app_status as _s
    return _s(st, p)


# ================================================================== modals
class Confirm(ModalScreen[bool]):
    BINDINGS = [Binding("y", "yes", "Yes"), Binding("n,escape", "no", "No")]

    def __init__(self, title: str, body: str | Text, yes: str = "Yes", no: str = "Cancel",
                 yes_variant: str = "error") -> None:
        super().__init__()
        self.t, self.body, self.yes, self.no, self.yes_variant = title, body, yes, no, yes_variant

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(self.t, id="dialog-title")
            yield Static(self.body, id="dialog-body")
            with Horizontal(id="dialog-buttons"):
                yield Button(self.yes, variant=self.yes_variant, id="yes")  # type: ignore[arg-type]
                yield Button(self.no, variant="primary", id="no")

    def on_mount(self) -> None:
        self.query_one("#no", Button).focus()

    def action_yes(self) -> None:
        self.dismiss(True)

    def action_no(self) -> None:
        self.dismiss(False)

    @on(Button.Pressed)
    def _pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "yes")


class MessageModal(ModalScreen[None]):
    BINDINGS = [Binding("escape,enter,q", "close", "Close")]

    def __init__(self, title: str, body: str | Text, error: bool = False) -> None:
        super().__init__()
        self.t, self.body, self.error = title, body, error

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog", classes="error" if self.error else ""):
            yield Label(self.t, id="dialog-title")
            with VerticalScroll(id="dialog-body"):
                yield Static(self.body)
            with Horizontal(id="dialog-buttons"):
                yield Button("OK", variant="primary", id="ok")

    def action_close(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed)
    def _pressed(self) -> None:
        self.dismiss(None)


class InputModal(ModalScreen[str | None]):
    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, title: str, prompt: str, value: str = "", placeholder: str = "",
                 validate: Callable[[str], str | None] | None = None, password: bool = False) -> None:
        super().__init__()
        self.t, self.prompt, self.value = title, prompt, value
        self.placeholder, self.validate_fn, self.password = placeholder, validate, password

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(self.t, id="dialog-title")
            yield Static(self.prompt, id="dialog-body")
            yield Input(self.value, placeholder=self.placeholder, id="value", password=self.password)
            yield Static("", id="error", classes="hint")
            with Horizontal(id="dialog-buttons"):
                yield Button("OK", variant="success", id="ok")
                yield Button("Cancel", variant="primary", id="cancel")

    def on_mount(self) -> None:
        self.query_one(Input).focus()

    def _submit(self) -> None:
        val = self.query_one(Input).value.strip()
        if self.validate_fn:
            err = self.validate_fn(val)
            if err:
                self.query_one("#error", Static).update(Text(err, style="#ff5555"))
                return
        self.dismiss(val)

    @on(Input.Submitted)
    def _submitted(self) -> None:
        self._submit()

    @on(Button.Pressed)
    def _pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "ok":
            self._submit()
        else:
            self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)


# ================================================================== base screen
class BaseScreen(Screen):
    @property
    def st(self) -> State:
        return self.app.st  # type: ignore[attr-defined]

    def fail(self, e: Exception, title: str = "Something went wrong") -> None:
        detail = getattr(e, "detail", "")
        body = Text(getattr(e, "message", str(e)), style="bold")
        if detail:
            body.append("\n\n" + detail, style="#f8f8f2 not bold")
        self.app.push_screen(MessageModal(title, body, error=True))

    def run_action(self, label: str, fn: Callable[[], object], done: Callable[[], None] | None = None) -> None:
        """Run a blocking operation in a thread with a toast before/after."""
        self.notify(f"{label}…", timeout=3)

        def work_() -> None:
            try:
                fn()
            except DemonError as e:
                self.app.call_from_thread(self.fail, e, f"{label} failed")
                return
            except Exception as e:  # pragma: no cover - unexpected
                self.app.call_from_thread(self.fail, e, f"{label} failed")
                return
            self.app.call_from_thread(self.notify, f"✓ {label} done", severity="information")
            if done:
                self.app.call_from_thread(done)

        self.run_worker(work_, thread=True, group="action", exclusive=False)


# ================================================================== setup
class SetupScreen(BaseScreen):
    BINDINGS = [
        Binding("enter", "fix", "Fix selected", show=True),
        Binding("f", "fix_all", "Fix all"),
        Binding("r", "recheck", "Recheck"),
        Binding("c,escape", "done", "Continue"),
        Binding("q", "app.quit", "Quit"),
    ]

    def __init__(self, setup_only: bool = False) -> None:
        super().__init__()
        self.setup_only = setup_only
        self.items: list[setup.Check] = []
        self.busy = False

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("", id="setup-summary", classes="summary")
        yield Label("Checks", classes="section")
        yield OptionList(id="checks")
        yield Static("Enter fixes the selected item, f fixes everything. Items that need sudo "
                     "will ask for your password in the terminal.", classes="hint")
        yield RichLog(id="setup-out", wrap=True, markup=False)
        yield Footer()

    def on_mount(self) -> None:
        self.app.sub_title = "Setup"
        self.action_recheck()

    @work(thread=True, exclusive=True, group="checks")
    def action_recheck(self) -> None:
        items = setup.checks(self.st)
        self.app.call_from_thread(self._show, items)

    def _show(self, items: list[setup.Check]) -> None:
        self.items = items
        ol = self.query_one("#checks", OptionList)
        idx = ol.highlighted or 0
        ol.clear_options()
        for c in items:
            t = Text()
            if c.ok:
                t.append(" ✓ ", style="bold #50fa7b")
            elif c.required:
                t.append(" ✗ ", style="bold #ff5555")
            else:
                t.append(" ! ", style="bold #f1fa8c")
            t.append(f"{c.label:42}", style="bold #f8f8f2")
            t.append(c.detail, style="#6272a4" if c.ok else "#f8f8f2")
            if not c.ok and c.fix:
                t.append("  [fixable" + (", sudo" if c.needs_sudo else "") + "]", style="#ffb86c")
            ol.add_option(Option(t, id=c.key))
        ol.highlighted = min(idx, len(items) - 1) if items else None
        ol.focus()
        bad = [c for c in items if not c.ok and c.required]
        summ = Text()
        if bad:
            summ.append(f"{len(bad)} required item{'s' if len(bad) > 1 else ''} need attention", style="bold #ff5555")
            summ.append("  ·  press ", style="#6272a4").append("f ", style="bold #ffb86c")
            summ.append("to fix everything", style="#6272a4")
        else:
            summ.append("✓ Ready", style="bold #50fa7b")
            summ.append(f"  ·  apps at {proxy.app_url(self.st, '<app>')}", style="#8be9fd")
            summ.append("  ·  press ", style="#6272a4").append("c ", style="bold #ffb86c")
            summ.append("to continue", style="#6272a4")
        self.query_one("#setup-summary", Static).update(summ)

    def _out(self, line: str) -> None:
        self.query_one("#setup-out", RichLog).write(log_text(line))

    def _fix(self, checks: list[setup.Check]) -> None:
        if self.busy:
            return
        todo = [c for c in checks if not c.ok and c.fix]
        if not todo:
            self.notify("Nothing to fix here.")
            return
        self.busy = True
        for c in [c for c in todo if c.needs_sudo]:
            if msg := setup.try_without_sudo(c):
                self._out(f"✓ {c.label}: {msg}")
                todo.remove(c)
        sudo_items = [c for c in todo if c.needs_sudo]
        plain = [c for c in todo if not c.needs_sudo]
        # sudo items run on the real terminal, one by one, before the rest
        for c in sudo_items:
            with self.app.suspend():
                print(f"\n\033[1;35m==> {c.label}\033[0m\n    {c.detail}")
                try:
                    msg = c.fix()  # type: ignore[misc]
                    print(f"\033[32m✓ {msg}\033[0m")
                    self._out(f"✓ {c.label}: {msg}")
                except DemonError as e:
                    print(f"\033[31m✗ {e}\033[0m")
                    self._out(f"!! {c.label}: {e}")
                    time.sleep(1.5)
        self._fix_plain(plain)

    @work(thread=True, exclusive=True, group="fix")
    def _fix_plain(self, todo: list[setup.Check]) -> None:
        try:
            for c in todo:
                self.app.call_from_thread(self._out, f"==> {c.label}")
                try:
                    if c.key == "caddy":
                        msg = setup.fix_caddy(lambda m: self.app.call_from_thread(self._out, "   " + m))
                    else:
                        msg = c.fix()  # type: ignore[misc]
                    self.app.call_from_thread(self._out, f"✓ {msg}")
                except DemonError as e:
                    self.app.call_from_thread(self._out, f"!! {e}")
            # the proxy may need refreshing after other fixes (e.g. ports)
            again = [c for c in setup.checks(self.st) if c.key == "proxy" and not c.ok]
            if again and proxy.find_caddy():
                self.app.call_from_thread(self._out, "==> Proxy service")
                try:
                    self.app.call_from_thread(self._out, "✓ " + setup.fix_proxy())
                except DemonError as e:
                    self.app.call_from_thread(self._out, f"!! {e}")
        finally:
            self.busy = False
            items = setup.checks(self.st)
            self.app.call_from_thread(self._show, items)

    def action_fix(self) -> None:
        ol = self.query_one("#checks", OptionList)
        if ol.highlighted is None or not self.items:
            return
        self._fix([self.items[ol.highlighted]])

    @on(OptionList.OptionSelected)
    def _selected(self) -> None:
        self.action_fix()

    def action_fix_all(self) -> None:
        self._fix(self.items)

    def action_done(self) -> None:
        if any(not c.ok and c.required for c in self.items):
            self.notify("Some required items still need fixing (f fixes all).", severity="warning")
            return
        if self.setup_only:
            self.app.exit()
            return
        self.app.pop_screen()
        app = self.app
        if getattr(app, "new_source", None):
            src, app.new_source = app.new_source, None  # type: ignore[attr-defined]
            app.push_screen(NewAppScreen(src, app.new_name or "", app.new_branch or ""))  # type: ignore[attr-defined]


# ================================================================== dashboard
class DashboardScreen(BaseScreen):
    BINDINGS = [
        Binding("enter", "open", "Open", show=False),
        Binding("n", "new", "New app"),
        Binding("d", "redeploy", "Redeploy"),
        Binding("l", "logs", "Logs"),
        Binding("r", "rollback", "Rollback"),
        Binding("e", "env", "Env"),
        Binding("o", "domains", "Domains"),
        Binding("s", "startstop", "Start/Stop"),
        Binding("w", "browse", "Open URL"),
        Binding("x", "delete", "Delete"),
        Binding("S", "setup", "Setup", show=False),
        Binding("q", "app.quit", "Quit"),
        Binding("j", "cursor_down", show=False),
        Binding("k", "cursor_up", show=False),
    ]

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("", id="dash-summary", classes="summary")
        yield DataTable(id="apps", cursor_type="row", zebra_stripes=False)
        with Center(id="empty"):
            with Vertical(id="empty-box"):
                yield Label("No apps yet", id="empty-title")
                yield Static(Text.assemble(
                    "Press ", ("n", "bold #ffb86c"), " to deploy a folder or a git repository.\n\n",
                    ("or from any project folder:  ", "#6272a4"), ("demon deploy", "bold #8be9fd")),
                    id="empty-body")
        yield Footer()

    def on_mount(self) -> None:
        self.app.sub_title = "Apps"
        t = self.query_one("#apps", DataTable)
        t.add_column("APP", key="app")
        t.add_column("STATUS", key="status", width=12)
        t.add_column("URL", key="url")
        t.add_column("TYPE", key="type")
        t.add_column("DEPLOYED", key="deployed")
        t.add_column("COMMIT", key="commit")
        t.add_column("MEMORY", key="mem")
        self.refresh_rows()
        self.set_interval(2.0, self.refresh_rows)

    def on_screen_resume(self) -> None:
        self.app.sub_title = "Apps"
        self.refresh_rows()

    @work(thread=True, exclusive=True, group="refresh")
    def refresh_rows(self) -> None:
        st = self.st
        rows = []
        n_run = 0
        for p in st.projects():
            status = app_status(st, p)
            n_run += status == "running"
            dep = st.deployment(p.active_id) if p.active_id else None
            last = st.deployments(p.name, limit=1)
            mem = ""
            if dep and dep.unit and status == "running":
                m = sysd.show(dep.unit, "MemoryCurrent").get("MemoryCurrent", "")
                mem = human_size(int(m)) if m.isdigit() else ""
            doms = st.domains(p.name)
            url = Text(proxy.app_url(st, p.name), style="#8be9fd")
            if doms:
                url.append(f" +{len(doms)}", style="#ffb86c")
            shown = dep or (last[0] if last else None)
            commit = Text()
            if shown:
                commit.append(shown.sha[:7] + " " if shown.sha else "", style="#ffb86c")
                msg = shown.message if len(shown.message) <= 24 else shown.message[:23] + "…"
                commit.append(msg, style="#f8f8f2")
            kind = (dep.kind if dep else p.kind)
            src = "git" if p.source_type == "git" else "dir"
            rows.append((p.name, [
                Text(p.name, style="bold #f8f8f2"), status_dot(status), url,
                Text(f"{kind}·{src}", style="#6272a4"),
                Text(ago(shown.created if shown else None), style="#f8f8f2"),
                commit, Text(mem, style="#6272a4")]))
        proxy_ok = sysd.is_active(paths.PROXY_UNIT)
        self.app.call_from_thread(self._apply, rows, n_run, proxy_ok)

    def _apply(self, rows, n_run: int, proxy_ok: bool) -> None:
        t = self.query_one("#apps", DataTable)
        key = self.selected()
        t.clear()
        for name, cells in rows:
            t.add_row(*cells, key=name)
        if key:
            try:
                t.move_cursor(row=t.get_row_index(key))
            except Exception:
                pass
        empty = not rows
        t.display = not empty
        self.query_one("#empty").display = empty
        s = Text()
        s.append(f"{len(rows)} ", style="bold #50fa7b").append(f"app{'s' if len(rows) != 1 else ''}  ·  ")
        s.append(f"{n_run} ", style="bold #50fa7b").append("running  ·  proxy ")
        s.append("● up" if proxy_ok else "✗ down", style="bold #50fa7b" if proxy_ok else "bold #ff5555")
        s.append(f"  ·  {proxy.app_url(self.st, '<app>')}", style="#8be9fd")
        if not proxy_ok:
            s.append("   press S for setup", style="#ffb86c")
        self.query_one("#dash-summary", Static).update(s)
        if not empty and self.focused is None:
            t.focus()

    def selected(self) -> str | None:
        t = self.query_one("#apps", DataTable)
        if not t.row_count:
            return None
        try:
            return t.coordinate_to_cell_key(t.cursor_coordinate).row_key.value
        except Exception:
            return None

    def _need(self) -> str | None:
        name = self.selected()
        if not name:
            self.notify("No app selected. Press n to create one.", severity="warning")
        return name

    @on(DataTable.RowSelected)
    def _row(self) -> None:
        self.action_open()

    def action_cursor_down(self) -> None:
        self.query_one("#apps", DataTable).action_cursor_down()

    def action_cursor_up(self) -> None:
        self.query_one("#apps", DataTable).action_cursor_up()

    def action_open(self) -> None:
        if name := self._need():
            self.app.push_screen(AppScreen(name))

    def action_new(self) -> None:
        self.app.push_screen(NewAppScreen())

    def action_redeploy(self) -> None:
        if name := self._need():
            start_deploy(self, name)

    def action_logs(self) -> None:
        if name := self._need():
            open_logs(self, name)

    def action_rollback(self) -> None:
        if name := self._need():
            ask_rollback(self, name)

    def action_env(self) -> None:
        if name := self._need():
            self.app.push_screen(EnvScreen(name))

    def action_domains(self) -> None:
        if name := self._need():
            self.app.push_screen(DomainsScreen(name))

    def action_startstop(self) -> None:
        if name := self._need():
            toggle_start_stop(self, name, self.refresh_rows)

    def action_browse(self) -> None:
        if name := self._need():
            self.app.open_url(proxy.app_url(self.st, name))

    def action_delete(self) -> None:
        if name := self._need():
            ask_delete(self, name, self.refresh_rows)

    def action_setup(self) -> None:
        self.app.push_screen(SetupScreen())


# ================================================================== shared actions
def start_deploy(screen: BaseScreen, name: str) -> None:
    try:
        dep = deploy.queue_deploy(screen.st, name)
    except DemonError as e:
        running = screen.st.in_progress(name)
        if running:
            screen.app.push_screen(DeployScreen(name, running[0].id))
            return
        screen.fail(e, "Could not start the deploy")
        return
    screen.app.push_screen(DeployScreen(name, dep.id))


def open_logs(screen: BaseScreen, name: str) -> None:
    st = screen.st
    p = st.project(name)
    running = st.in_progress(name)
    if running:
        screen.app.push_screen(DeployScreen(name, running[0].id))
        return
    dep = st.deployment(p.active_id) if p and p.active_id else None
    if dep and dep.unit:
        screen.app.push_screen(LogsScreen(name, unit=dep.unit))
    else:
        last = st.deployments(name, limit=1)
        if not last:
            screen.notify("No deployments yet.", severity="warning")
            return
        screen.app.push_screen(DeployScreen(name, (dep or last[0]).id))


def ask_rollback(screen: BaseScreen, name: str, dep_id: int | None = None) -> None:
    try:
        target = deploy.rollback_target(screen.st, name, dep_id)
    except DemonError as e:
        screen.fail(e, "Can't roll back")
        return
    body = Text.assemble(f"Serve deployment #{target.id} of {name} again?\n\n",
                         (target.sha[:7] + "  " if target.sha else "", "#ffb86c"),
                         (target.message, "#f8f8f2"), (f"\nbuilt {ago(target.created)}", "#6272a4"),
                         ("\n\nIt starts next to the current version and takes over only "
                          "once it answers.", "#6272a4"))

    def go(ok: bool | None) -> None:
        if ok:
            screen.run_action(f"Rolling back {name} to #{target.id}",
                              lambda: deploy.rollback(screen.st, name, target.id, out=lambda s: None))
    screen.app.push_screen(Confirm("↺  Roll back", body, "Roll back", "Cancel", "warning"), go)


def toggle_start_stop(screen: BaseScreen, name: str, done: Callable[[], None] | None = None) -> None:
    p = screen.st.project(name)
    if p is None:
        return
    if p.stopped or app_status(screen.st, p) in ("crashed", "inactive"):
        screen.run_action(f"Starting {name}", lambda: deploy.start(screen.st, name), done)
    else:
        def go(ok: bool | None) -> None:
            if ok:
                screen.run_action(f"Stopping {name}", lambda: deploy.stop(screen.st, name), done)
        screen.app.push_screen(Confirm("Stop app", f"Stop {name}? Visitors will see an "
                                       "'app stopped' page until you start it again.",
                                       "Stop", "Cancel"), go)


def ask_delete(screen: BaseScreen, name: str, done: Callable[[], None] | None = None) -> None:
    def go(ok: bool | None) -> None:
        if ok:
            screen.run_action(f"Deleting {name}", lambda: deploy.remove(screen.st, name), done)
    screen.app.push_screen(Confirm(
        "Delete app", Text.assemble(("Delete ", ""), (name, "bold"),
                                    "?\n\nThis stops it and removes all its deployments, environment "
                                    "variables and domains. The source folder/repository is not touched."),
        "Delete", "Cancel"), go)


# ================================================================== new app
class NewAppScreen(BaseScreen):
    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("ctrl+s", "deploy", "Deploy"),
    ]

    def __init__(self, source: str = "", name: str = "", branch: str = "") -> None:
        super().__init__()
        self.init_source = source or os.getcwd()
        self.init_name = name
        self.init_branch = branch
        self.plan: Plan | None = None
        self.detected_for = ""
        self.name_touched = bool(name)

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static(Text.assemble(("New app", "bold #bd93f9"),
                                   ("  ·  enter a folder or a git URL, check the detected settings, "
                                    "then Deploy (ctrl+s)", "#6272a4")), classes="summary")
        with VerticalScroll(id="form"):
            with Vertical(classes="field"):
                yield Label("Source")
                yield Input(self.init_source, id="source",
                            placeholder="/path/to/project  or  https://github.com/you/repo.git")
                yield Static("Press Enter to detect the app type.", classes="help", id="source-help")
            yield LoadingIndicator(id="detect-loading")
            with Vertical(id="detect-wrap"):
                with Horizontal(classes="field"):
                    with Vertical():
                        yield Label("Name")
                        yield Input(self.init_name, id="name", placeholder="my-app")
                        yield Static("", classes="help", id="name-help")
                    with Vertical():
                        yield Label("Type")
                        yield Select([(k, k) for k in KINDS], id="kind", allow_blank=False, value="node")
                with Vertical(classes="field", id="branch-field"):
                    yield Label("Git branch")
                    yield Input(self.init_branch, id="branch", placeholder="default branch")
                with Horizontal(classes="switch-row", id="auto-row"):
                    yield Switch(True, id="auto")
                    yield Label("Redeploy automatically when the branch gets new commits")
                with Vertical(classes="field"):
                    yield Label("Install command")
                    yield Input(id="install", placeholder="e.g. npm ci")
                with Vertical(classes="field"):
                    yield Label("Build command")
                    yield Input(id="build", placeholder="optional, e.g. npm run build")
                with Vertical(classes="field", id="start-field"):
                    yield Label("Start command")
                    yield Input(id="start", placeholder="must listen on $PORT, e.g. npm start")
                with Vertical(classes="field", id="static-field"):
                    yield Label("Folder to serve")
                    yield Input(id="static", placeholder="dist, build, out… (blank = find index.html)")
                yield Static("", id="notes")
            with Horizontal(id="form-buttons"):
                yield Button("Deploy", variant="success", id="go")
                yield Button("Cancel", variant="primary", id="cancel")
        yield Footer()

    def on_mount(self) -> None:
        self.app.sub_title = "New app"
        self.query_one("#detect-wrap").display = False
        self.query_one("#go", Button).disabled = True
        self.query_one("#source", Input).focus()
        if self.init_source:
            self.run_detect()

    # ----- detection
    @on(Input.Submitted, "#source")
    def _source_submitted(self) -> None:
        self.run_detect()

    def run_detect(self) -> None:
        src = self.query_one("#source", Input).value.strip()
        if not src:
            return
        self.query_one("#detect-loading").display = True
        self.query_one("#source-help", Static).update("Detecting…")
        self._detect(src, self.query_one("#branch", Input).value.strip())

    @work(thread=True, exclusive=True, group="detect")
    def _detect(self, src: str, branch: str) -> None:
        try:
            if gitsrc.looks_like_git_url(src):
                branch = branch or gitsrc.default_branch(src)
                with tempfile.TemporaryDirectory() as tmp:
                    gitsrc.checkout(src, branch, Path(tmp) / "repo", Path(tmp) / "src")
                    plan = detect(Path(tmp) / "src")
                name, stype = slugify(gitsrc.repo_name(src)), "git"
            else:
                path = Path(src).expanduser().resolve()
                if not path.is_dir():
                    raise DemonError(f"'{src}' is not a folder or a git URL.")
                plan = detect(path)
                name, stype, src = slugify(path.name), "dir", str(path)
            self.app.call_from_thread(self._detected, src, stype, name, branch, plan, None)
        except DemonError as e:
            self.app.call_from_thread(self._detected, src, "", "", branch, None, e)

    def _detected(self, src: str, stype: str, name: str, branch: str, plan: Plan | None,
                  err: DemonError | None) -> None:
        self.query_one("#detect-loading").display = False
        help_ = self.query_one("#source-help", Static)
        if err or plan is None:
            help_.update(Text(str(err), style="#ff5555"))
            self.query_one("#detect-wrap").display = False
            self.query_one("#go", Button).disabled = True
            return
        existing = self.st.project_by_source(src)
        self.plan, self.stype, self.detected_for = plan, stype, src
        self.query_one("#source", Input).value = src
        help_.update(Text.assemble(("✓ Detected ", "#50fa7b"), (plan.label, "bold #50fa7b"),
                                   (f"  ·  {'git repository' if stype == 'git' else 'local folder'}", "#6272a4"),
                                   (f"  ·  already deployed as '{existing.name}'" if existing else "", "#ffb86c")))
        if not self.name_touched:
            n = name
            i = 2
            while self.st.project(n):
                n = f"{name}-{i}"
                i += 1
            self.query_one("#name", Input).value = n
        self.query_one("#branch", Input).value = branch
        self.query_one("#kind", Select).value = plan.kind
        self.query_one("#install", Input).value = plan.install
        self.query_one("#build", Input).value = plan.build
        self.query_one("#start", Input).value = plan.start
        self.query_one("#static", Input).value = plan.static_dir
        notes = "\n".join(f"• {n}" for n in plan.notes)
        self.query_one("#notes", Static).update(notes)
        self.query_one("#branch-field").display = stype == "git"
        self.query_one("#auto-row").display = stype == "git"
        self.query_one("#detect-wrap").display = True
        self._kind_changed()
        self.query_one("#go", Button).disabled = False
        self.query_one("#name", Input).focus()

    @on(Input.Changed, "#name")
    def _name_changed(self, event: Input.Changed) -> None:
        if event.input.has_focus:
            self.name_touched = True
        v = event.value.strip()
        help_ = self.query_one("#name-help", Static)
        if not v:
            help_.update("")
        elif not NAME_RE.match(v):
            help_.update(Text("lowercase letters, digits and dashes", style="#ff5555"))
        elif self.st.project(v):
            help_.update(Text("an app with this name exists", style="#ff5555"))
        else:
            help_.update(Text(proxy.app_url(self.st, v), style="#8be9fd"))

    @on(Select.Changed, "#kind")
    def _kind_changed(self) -> None:
        static = self.query_one("#kind", Select).value == STATIC
        self.query_one("#start-field").display = not static
        self.query_one("#static-field").display = static

    # ----- submit
    @on(Button.Pressed, "#go")
    def action_deploy(self) -> None:
        if self.plan is None or self.query_one("#go", Button).disabled:
            return
        if self.query_one("#source", Input).value.strip() != self.detected_for:
            self.run_detect()
            return
        v = lambda i: self.query_one(f"#{i}", Input).value.strip()  # noqa: E731
        name, kind = v("name"), str(self.query_one("#kind", Select).value)
        if not NAME_RE.match(name):
            self.notify("Pick a valid name: lowercase letters, digits and dashes.", severity="error")
            self.query_one("#name", Input).focus()
            return
        if kind != STATIC and not v("start"):
            self.notify("A start command is needed (the app must listen on $PORT).", severity="error")
            self.query_one("#start", Input).focus()
            return
        p = Project(name=name, source_type=self.stype, source=self.detected_for,
                    branch=v("branch") if self.stype == "git" else "",
                    autodeploy=self.stype == "git" and self.query_one("#auto", Switch).value,
                    kind=kind, install_cmd=v("install"), build_cmd=v("build"),
                    start_cmd=v("start") if kind != STATIC else "",
                    static_dir=v("static") if kind == STATIC else "", health_path=self.plan.health)
        try:
            deploy.create_project(self.st, p)
            if p.source_type == "git":
                self.st.update_project(name, last_sha="")
            dep = deploy.queue_deploy(self.st, name)
        except DemonError as e:
            self.fail(e, "Could not create the app")
            return
        self.app.switch_screen(DeployScreen(name, dep.id))

    @on(Button.Pressed, "#cancel")
    def action_cancel(self) -> None:
        if len(self.app.screen_stack) > 2:
            self.app.pop_screen()
        else:
            self.app.switch_screen(DashboardScreen())


# ================================================================== deploy (live build)
class DeployScreen(BaseScreen):
    BINDINGS = [
        Binding("escape,q", "back", "Back"),
        Binding("c", "cancel_deploy", "Cancel deploy"),
        Binding("w", "browse", "Open URL"),
        Binding("d", "redeploy", "Deploy again"),
    ]

    def __init__(self, name: str, dep_id: int) -> None:
        super().__init__()
        self.name_ = name
        self.dep_id = dep_id
        self.pos = 0
        self.finished = False
        self.started = time.time()

    def compose(self) -> ComposeResult:
        yield Header()
        with Vertical(id="dp"):
            with Horizontal(id="dp-top"):
                yield Static("", id="dp-state")
                yield Static("", id="dp-target")
            yield Label("Progress", classes="dp-label")
            yield Static("", id="dp-steps")
            yield Static("", id="dp-meta")
            yield RichLog(id="dp-log", wrap=True, markup=False, max_lines=20000)
        yield Footer()

    def on_mount(self) -> None:
        self.app.sub_title = f"{self.name_} · deployment #{self.dep_id}"
        dep = self.st.deployment(self.dep_id)
        if dep:
            self.started = dep.created
        self.timer = self.set_interval(0.3, self.tick)
        self.tick()

    def tick(self) -> None:
        dep = self.st.deployment(self.dep_id)
        if dep is None:
            return
        path = deploy.log_path(self.name_, self.dep_id)
        if path.exists():
            with open(path, errors="replace") as f:
                f.seek(self.pos)
                chunk = f.read()
                self.pos = f.tell()
            log = self.query_one("#dp-log", RichLog)
            for line in chunk.splitlines():
                log.write(log_text(line))
        p = self.st.project(self.name_)
        status = {"active": "live", "superseded": "live", "failed": "failed",
                  "cancelled": "cancelled"}.get(dep.status, "deploying")
        if p and dep.status == SUPERSEDED:
            status = "live"
        self.query_one("#dp-state", Static).update(pill(status))
        tgt = Text()
        tgt.append(self.name_, style="bold #f8f8f2").append("  →  ", style="#6272a4")
        tgt.append(proxy.app_url(self.st, self.name_), style="#8be9fd")
        if dep.sha or dep.message:
            tgt.append(f"   {dep.sha[:7]} ", style="#ffb86c").append(dep.message[:60], style="#6272a4")
        self.query_one("#dp-target", Static).update(tgt)

        steps = Text()
        cur = dep.step if dep.step in STEPS else "fetch"
        idx = STEPS.index(cur)
        is_static = dep.kind == STATIC
        for i, s in enumerate(STEPS):
            if is_static and s == "health":
                continue
            label = s.capitalize()
            if dep.status == ACTIVE or (dep.status == SUPERSEDED):
                steps.append(f"✓ {label}", style="#50fa7b")
            elif i < idx:
                steps.append(f"✓ {label}", style="#50fa7b")
            elif i == idx and not dep.done:
                steps.append(f"● {label}", style="bold #bd93f9")
            elif i == idx and dep.status in (FAILED, CANCELLED):
                steps.append(f"✗ {label}", style="bold #ff5555")
            else:
                steps.append(f"○ {label}", style="#6272a4")
            if s != "live":
                steps.append("  ▸  ", style="#44475a")
        self.query_one("#dp-steps", Static).update(steps)

        end = dep.finished if dep.finished else time.time()
        meta = Text(f"elapsed {human_time(end - dep.created)}", style="#6272a4")
        if dep.status == FAILED:
            meta.append(f"   ✗ {dep.error}", style="bold #ff5555")
            meta.append("   the previous version is still live" if p and p.active_id else "", style="#6272a4")
        elif dep.done and status == "live":
            meta.append("   ✓ deployed", style="bold #50fa7b")
        self.query_one("#dp-meta", Static).update(meta)

        if dep.done and not self.finished:
            self.finished = True
            if status == "live":
                self.notify(f"✓ {self.name_} is live", severity="information")
            elif dep.status == FAILED:
                self.notify(f"Deploy of {self.name_} failed", severity="error")
            self.set_timer(1.0, self.timer.stop)

    def action_back(self) -> None:
        if not self.finished:
            self.notify("The deploy continues in the background.")
        if len(self.app.screen_stack) > 2:
            self.app.pop_screen()
        else:
            self.app.switch_screen(DashboardScreen())

    def action_cancel_deploy(self) -> None:
        if self.finished:
            return

        def go(ok: bool | None) -> None:
            if ok:
                self.run_action("Cancelling deploy", lambda: deploy.cancel(self.st, self.dep_id))
        self.app.push_screen(Confirm("Cancel deploy", "Stop this deploy? The current live version "
                                     "is not affected.", "Stop deploy", "Keep going"), go)

    def action_browse(self) -> None:
        self.app.open_url(proxy.app_url(self.st, self.name_))

    def action_redeploy(self) -> None:
        if not self.finished:
            self.notify("Still deploying.", severity="warning")
            return
        try:
            dep = deploy.queue_deploy(self.st, self.name_)
        except DemonError as e:
            self.fail(e)
            return
        self.app.switch_screen(DeployScreen(self.name_, dep.id))


# ================================================================== app details
class AppScreen(BaseScreen):
    BINDINGS = [
        Binding("escape", "back", "Back"),
        Binding("d", "redeploy", "Redeploy"),
        Binding("r", "rollback", "Rollback"),
        Binding("l", "logs", "Logs"),
        Binding("b", "build_log", "Build log", show=False),
        Binding("e", "env", "Env"),
        Binding("o", "domains", "Domains"),
        Binding("c", "settings", "Settings"),
        Binding("s", "startstop", "Start/Stop"),
        Binding("R", "restart", "Restart"),
        Binding("a", "auto", "Auto-deploy", show=False),
        Binding("w", "browse", "Open URL"),
        Binding("x", "delete", "Delete"),
        Binding("j", "cursor_down", show=False),
        Binding("k", "cursor_up", show=False),
    ]

    def __init__(self, name: str) -> None:
        super().__init__()
        self.name_ = name

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("", id="app-summary", classes="summary")
        yield Static("", id="info")
        yield Label("Deployments", classes="section")
        yield DataTable(id="deps", cursor_type="row")
        yield Footer()

    def on_mount(self) -> None:
        self.app.sub_title = self.name_
        t = self.query_one("#deps", DataTable)
        for col in ("", "ID", "STATUS", "WHEN", "COMMIT", "MESSAGE", "ERROR"):
            t.add_column(col)
        self.refresh_info()
        self.set_interval(2.0, self.refresh_info)
        t.focus()

    def on_screen_resume(self) -> None:
        self.app.sub_title = self.name_
        self.refresh_info()

    @work(thread=True, exclusive=True, group="info")
    def refresh_info(self) -> None:
        st = self.st
        p = st.project(self.name_)
        if p is None:
            self.app.call_from_thread(self.app.pop_screen)
            return
        status = app_status(st, p)
        dep = st.deployment(p.active_id) if p.active_id else None
        svc = {}
        if dep and dep.unit:
            svc = sysd.show(dep.unit, "MemoryCurrent", "ActiveEnterTimestampMonotonic", "MainPID",
                            "NRestarts", "CPUUsageNSec")
        deps = st.deployments(p.name, limit=50)
        doms = [(d, proxy.domain_url(st, d), proxy.cert_status(d)) for d, _ in st.domains(p.name)]
        self.app.call_from_thread(self._apply, p, status, dep, svc, deps, doms)

    def _apply(self, p: Project, status, dep, svc, deps, doms) -> None:
        s = Text()
        s.append_text(pill(status))
        s.append(f"  {p.name}", style="bold #f8f8f2").append("  →  ", style="#6272a4")
        s.append(proxy.app_url(self.st, p.name), style="#8be9fd")
        for d, url, _ in doms:
            s.append("  ").append(url, style="#8be9fd")
        self.query_one("#app-summary", Static).update(s)

        i = Text()

        def row(k: str, v: str | Text, style: str = "#f8f8f2") -> None:
            i.append(f"{k:12}", style="#bd93f9")
            i.append(v if isinstance(v, Text) else Text(v, style=style))
            i.append("\n")

        src = p.source if p.source_type == "git" else p.source.replace(str(Path.home()), "~", 1)
        src += f"  ({p.branch})" if p.source_type == "git" else ""
        row("source", src)
        if p.source_type == "git":
            row("autodeploy", "on — new commits deploy within a minute" if p.autodeploy else "off (a toggles)",
                "#50fa7b" if p.autodeploy else "#6272a4")
        row("type", p.kind)
        if p.install_cmd:
            row("install", p.install_cmd, "#6272a4")
        if p.build_cmd:
            row("build", p.build_cmd, "#6272a4")
        if p.kind == STATIC:
            row("serve", (dep.static_dir if dep else "") or p.static_dir or "(auto)", "#6272a4")
        else:
            row("start", p.start_cmd, "#6272a4")
        if dep and dep.unit:
            mem = svc.get("MemoryCurrent", "")
            extra = f"pid {svc.get('MainPID', '?')} · port {dep.port}"
            if mem.isdigit():
                extra += f" · {human_size(int(mem))}"
            if svc.get("NRestarts", "0") not in ("0", ""):
                extra += f" · restarted {svc['NRestarts']}×"
            row("process", extra)
        if doms:
            row("domains", Text("  ".join(f"{d} ({c})" for d, _, c in doms)))
        env_n = len(self.st.env(p.name))
        row("env", f"{env_n} variable{'s' if env_n != 1 else ''} (e to edit)")
        i.rstrip()
        self.query_one("#info", Static).update(i)

        t = self.query_one("#deps", DataTable)
        key = None
        if t.row_count:
            try:
                key = t.coordinate_to_cell_key(t.cursor_coordinate).row_key.value
            except Exception:
                key = None
        t.clear()
        for d in deps:
            live = d.id == p.active_id
            t.add_row(Text("●" if live else "", style="#50fa7b"),
                      Text(f"#{d.id}", style="bold"),
                      Text(d.status, style=DEP_COLORS.get(d.status, "#f8f8f2")),
                      Text(ago(d.created)), Text(d.sha[:7], style="#ffb86c"),
                      Text(d.message[:50]), Text(d.error[:60], style="#ff5555"), key=str(d.id))
        if key:
            try:
                t.move_cursor(row=t.get_row_index(key))
            except Exception:
                pass

    def selected_dep(self) -> int | None:
        t = self.query_one("#deps", DataTable)
        if not t.row_count:
            return None
        try:
            return int(t.coordinate_to_cell_key(t.cursor_coordinate).row_key.value)
        except Exception:
            return None

    def action_cursor_down(self) -> None:
        self.query_one("#deps", DataTable).action_cursor_down()

    def action_cursor_up(self) -> None:
        self.query_one("#deps", DataTable).action_cursor_up()

    @on(DataTable.RowSelected)
    def _row(self) -> None:
        self.action_build_log()

    def action_back(self) -> None:
        self.app.pop_screen()

    def action_redeploy(self) -> None:
        start_deploy(self, self.name_)

    def action_rollback(self) -> None:
        sel = self.selected_dep()
        p = self.st.project(self.name_)
        d = self.st.deployment(sel) if sel else None
        target = sel if d and d.status == SUPERSEDED and p and sel != p.active_id else None
        ask_rollback(self, self.name_, target)

    def action_logs(self) -> None:
        open_logs(self, self.name_)

    def action_build_log(self) -> None:
        sel = self.selected_dep()
        if sel:
            self.app.push_screen(DeployScreen(self.name_, sel))

    def action_env(self) -> None:
        self.app.push_screen(EnvScreen(self.name_))

    def action_domains(self) -> None:
        self.app.push_screen(DomainsScreen(self.name_))

    def action_settings(self) -> None:
        self.app.push_screen(SettingsScreen(self.name_))

    def action_startstop(self) -> None:
        toggle_start_stop(self, self.name_, self.refresh_info)

    def action_restart(self) -> None:
        self.run_action(f"Restarting {self.name_}",
                        lambda: deploy.restart(self.st, self.name_, out=lambda s: None), self.refresh_info)

    def action_auto(self) -> None:
        p = self.st.project(self.name_)
        if p is None or p.source_type != "git":
            self.notify("Auto-deploy is for apps deployed from git.", severity="warning")
            return
        self.st.update_project(p.name, autodeploy=not p.autodeploy)
        self.notify(f"Auto-deploy {'off' if p.autodeploy else 'on'}")
        self.refresh_info()

    def action_browse(self) -> None:
        self.app.open_url(proxy.app_url(self.st, self.name_))

    def action_delete(self) -> None:
        ask_delete(self, self.name_, lambda: self.app.pop_screen())


# ================================================================== settings
class SettingsScreen(BaseScreen):
    BINDINGS = [Binding("escape", "back", "Back"), Binding("ctrl+s", "save", "Save")]
    FIELDS = (("install_cmd", "Install command", ""), ("build_cmd", "Build command", ""),
              ("start_cmd", "Start command", "must listen on $PORT"),
              ("static_dir", "Folder to serve (static)", "blank = find index.html"),
              ("health_path", "Health check path", "checked before a new version goes live"),
              ("branch", "Git branch", ""))

    def __init__(self, name: str) -> None:
        super().__init__()
        self.name_ = name

    def compose(self) -> ComposeResult:
        p = self.st.project(self.name_)
        yield Header()
        yield Static(Text.assemble(("Settings", "bold #bd93f9"),
                                   ("  ·  changes apply on the next deploy  ·  a demon.toml in the "
                                    "source overrides these", "#6272a4")), classes="summary")
        with VerticalScroll(id="form"):
            with Vertical(classes="field"):
                yield Label("Type")
                yield Select([(k, k) for k in KINDS], id="kind", allow_blank=False, value=p.kind)
            for key, label, help_ in self.FIELDS:
                if key == "branch" and p.source_type != "git":
                    continue
                with Vertical(classes="field"):
                    yield Label(label)
                    yield Input(str(getattr(p, key) or ""), id=key, placeholder=help_)
            with Horizontal(id="form-buttons"):
                yield Button("Save", variant="success", id="save")
                yield Button("Save & redeploy", variant="warning", id="save-deploy")
                yield Button("Cancel", variant="primary", id="cancel")
        yield Footer()

    def on_mount(self) -> None:
        self.app.sub_title = f"{self.name_} · settings"

    def _save(self) -> bool:
        kw = {"kind": str(self.query_one("#kind", Select).value)}
        for key, _, _ in self.FIELDS:
            try:
                kw[key] = self.query_one(f"#{key}", Input).value.strip()
            except Exception:
                continue
        if kw["kind"] != STATIC and not kw.get("start_cmd"):
            self.notify("A start command is needed for this type.", severity="error")
            return False
        kw["health_path"] = kw.get("health_path") or "/"
        self.st.update_project(self.name_, **kw)
        self.notify("✓ Saved")
        return True

    @on(Button.Pressed, "#save")
    def action_save(self) -> None:
        if self._save():
            self.app.pop_screen()

    @on(Button.Pressed, "#save-deploy")
    def _save_deploy(self) -> None:
        if self._save():
            self.app.pop_screen()
            start_deploy(self.app.screen, self.name_)  # type: ignore[arg-type]

    @on(Button.Pressed, "#cancel")
    def action_back(self) -> None:
        self.app.pop_screen()


# ================================================================== logs
class LogsScreen(BaseScreen):
    BINDINGS = [Binding("escape,q", "back", "Back"), Binding("c", "clear", "Clear")]

    def __init__(self, name: str, unit: str) -> None:
        super().__init__()
        self.name_, self.unit = name, unit
        self.proc: subprocess.Popen | None = None

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static(Text.assemble(("Live output of ", "#6272a4"), (self.name_, "bold #f8f8f2"),
                                   ("   ·   ", "#6272a4"), (self.unit, "#6272a4")), classes="summary")
        yield RichLog(id="log", wrap=True, markup=False, max_lines=20000, auto_scroll=True)
        yield Footer()

    def on_mount(self) -> None:
        self.app.sub_title = f"{self.name_} · logs"
        self.query_one(RichLog).focus()
        self.follow()

    @work(thread=True, exclusive=True, group="logs")
    def follow(self) -> None:
        self.proc = sysd.journal_follow(self.unit, 300)
        assert self.proc.stdout
        log = self.query_one(RichLog)
        for line in self.proc.stdout:
            self.app.call_from_thread(log.write, format_journal_line(line.rstrip("\n")))

    def on_unmount(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()

    def action_clear(self) -> None:
        self.query_one(RichLog).clear()

    def action_back(self) -> None:
        self.app.pop_screen()


# ================================================================== env
class EnvScreen(BaseScreen):
    BINDINGS = [
        Binding("escape", "back", "Back"),
        Binding("a", "add", "Add"),
        Binding("enter", "edit", "Edit", show=False),
        Binding("x", "remove", "Remove"),
        Binding("v", "reveal", "Show/Hide values"),
        Binding("R", "apply", "Apply (restart)"),
    ]

    def __init__(self, name: str) -> None:
        super().__init__()
        self.name_ = name
        self.reveal = False

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static(Text.assemble(("Environment variables", "bold #bd93f9"),
                                   ("  ·  available at build and run time  ·  R restarts the app "
                                    "with no downtime to apply  ·  PORT is set automatically", "#6272a4")),
                     classes="summary")
        yield DataTable(id="env", cursor_type="row")
        yield Footer()

    def on_mount(self) -> None:
        self.app.sub_title = f"{self.name_} · env"
        t = self.query_one(DataTable)
        t.add_column("KEY", key="k")
        t.add_column("VALUE", key="v")
        self.load()
        t.focus()

    def load(self) -> None:
        t = self.query_one(DataTable)
        t.clear()
        for k, v in self.st.env(self.name_).items():
            shown = v if self.reveal else ("•" * min(max(len(v), 3), 12))
            t.add_row(Text(k, style="bold #8be9fd"), Text(shown), key=k)

    def selected(self) -> str | None:
        t = self.query_one(DataTable)
        if not t.row_count:
            return None
        return t.coordinate_to_cell_key(t.cursor_coordinate).row_key.value

    @staticmethod
    def _valid(s: str) -> str | None:
        k, sep, _ = s.partition("=")
        if not sep:
            return "Use KEY=VALUE"
        if not k or not (k[0].isalpha() or k[0] == "_") or not k.replace("_", "").isalnum():
            return "Keys use letters, digits and _ and can't start with a digit"
        if k == "PORT":
            return "PORT is managed by demondeploy"
        return None

    def action_add(self) -> None:
        def done(val: str | None) -> None:
            if val:
                k, _, v = val.partition("=")
                self.st.set_env(self.name_, k.strip(), v)
                self.load()
                self.notify(f"✓ {k} saved · press R to apply now")
        self.app.push_screen(InputModal("Add variable", "KEY=VALUE", placeholder="DATABASE_URL=postgres://…",
                                        validate=self._valid), done)

    @on(DataTable.RowSelected)
    def action_edit(self) -> None:
        k = self.selected()
        if not k:
            return
        cur = self.st.env(self.name_).get(k, "")

        def done(val: str | None) -> None:
            if val is not None:
                self.st.set_env(self.name_, k, val)
                self.load()
                self.notify(f"✓ {k} saved · press R to apply now")
        self.app.push_screen(InputModal(f"Edit {k}", "New value", value=cur), done)

    def action_remove(self) -> None:
        k = self.selected()
        if not k:
            return

        def go(ok: bool | None) -> None:
            if ok:
                self.st.unset_env(self.name_, k)
                self.load()
        self.app.push_screen(Confirm("Remove variable", f"Remove {k}?", "Remove", "Cancel"), go)

    def action_reveal(self) -> None:
        self.reveal = not self.reveal
        self.load()

    def action_apply(self) -> None:
        self.run_action(f"Restarting {self.name_} with the new environment",
                        lambda: deploy.restart(self.st, self.name_, out=lambda s: None))

    def action_back(self) -> None:
        self.app.pop_screen()


# ================================================================== domains
class DomainsScreen(BaseScreen):
    BINDINGS = [
        Binding("escape", "back", "Back"),
        Binding("a", "add", "Add domain"),
        Binding("x", "remove", "Remove"),
        Binding("w", "browse", "Open"),
        Binding("r", "reload", "Refresh"),
    ]

    def __init__(self, name: str) -> None:
        super().__init__()
        self.name_ = name

    def compose(self) -> ComposeResult:
        yield Header()
        tls = proxy.tls_enabled(self.st)
        yield Static(Text.assemble(
            ("Custom domains", "bold #bd93f9"),
            ("  ·  point an A/AAAA record at this machine, add it here, and HTTPS is set up "
             "automatically" if tls else
             "  ·  ports 80/443 are off (run setup) so domains are served over plain HTTP on "
             f":{proxy.http_port(self.st)}", "#6272a4")), classes="summary")
        yield DataTable(id="doms", cursor_type="row")
        yield Footer()

    def on_mount(self) -> None:
        self.app.sub_title = f"{self.name_} · domains"
        t = self.query_one(DataTable)
        t.add_column("DOMAIN", key="d")
        t.add_column("URL", key="u")
        t.add_column("CERTIFICATE", key="c")
        self.action_reload()
        t.focus()

    def action_reload(self) -> None:
        t = self.query_one(DataTable)
        t.clear()
        for d, _ in self.st.domains(self.name_):
            cs = proxy.cert_status(d) if proxy.tls_enabled(self.st) else "no TLS"
            color = {"issued": "#50fa7b", "internal CA": "#8be9fd"}.get(cs, "#f1fa8c")
            t.add_row(Text(d, style="bold"), Text(proxy.domain_url(self.st, d), style="#8be9fd"),
                      Text(cs, style=color), key=d)

    def selected(self) -> str | None:
        t = self.query_one(DataTable)
        if not t.row_count:
            return None
        return t.coordinate_to_cell_key(t.cursor_coordinate).row_key.value

    @staticmethod
    def _valid(s: str) -> str | None:
        try:
            domains.normalize(s)
        except DemonError as e:
            return e.message
        return None

    def action_add(self) -> None:
        def entered(val: str | None) -> None:
            if val:
                self.notify(f"Checking DNS for {val}…")
                self._check(domains.normalize(val))
        self.app.push_screen(InputModal("Add domain", f"Domain for {self.name_}",
                                        placeholder="app.example.com", validate=self._valid), entered)

    @work(thread=True, exclusive=True, group="dns")
    def _check(self, d: str) -> None:
        chk = domains.check(self.st, d)
        self.app.call_from_thread(self._confirm, chk)

    def _confirm(self, chk: domains.Check) -> None:
        body = Text()
        for m in chk.ok:
            body.append(f"✓ {m}\n", style="#50fa7b")
        for m in chk.warn:
            body.append(f"! {m}\n", style="#f1fa8c")
        if chk.warn:
            body.append("\nYou can add it now; it starts working as soon as DNS is right.", style="#6272a4")

        def go(ok: bool | None) -> None:
            if ok:
                self.run_action(f"Adding {chk.domain}",
                                lambda: domains.add(self.st, self.name_, chk.domain), self.action_reload)
        self.app.push_screen(Confirm(f"Add {chk.domain}?", body, "Add domain", "Cancel",
                                     "success" if chk.good else "warning"), go)

    def action_remove(self) -> None:
        d = self.selected()
        if not d:
            return

        def go(ok: bool | None) -> None:
            if ok:
                self.run_action(f"Removing {d}", lambda: domains.remove(self.st, d), self.action_reload)
        self.app.push_screen(Confirm("Remove domain", f"Stop serving {self.name_} on {d}?", "Remove",
                                     "Cancel"), go)

    def action_browse(self) -> None:
        if d := self.selected():
            self.app.open_url(proxy.domain_url(self.st, d))

    def action_back(self) -> None:
        self.app.pop_screen()


# ================================================================== app
class DemonApp(App):
    TITLE = "demondeploy"
    CSS_PATH = "app.tcss"
    BINDINGS = [Binding("ctrl+c", "quit", "Quit", show=False, priority=True)]

    def __init__(self, new_source: str | None = None, new_name: str | None = None,
                 new_branch: str | None = None, setup_only: bool = False) -> None:
        super().__init__()
        self.st = State()
        self.new_source, self.new_name, self.new_branch = new_source, new_name, new_branch
        self.setup_only = setup_only

    def on_mount(self) -> None:
        self.title = f"demondeploy {__version__}"
        if self.setup_only:
            self.push_screen(SetupScreen(setup_only=True))
            return
        self.push_screen(DashboardScreen())
        self.check_setup()

    @work(thread=True, exclusive=True)
    def check_setup(self) -> None:
        ready = setup.is_ready(self.st)
        self.call_from_thread(self._after_check, ready)

    def _after_check(self, ready: bool) -> None:
        if not ready:
            self.push_screen(SetupScreen())
        elif self.new_source:
            src, self.new_source = self.new_source, None
            self.push_screen(NewAppScreen(src, self.new_name or "", self.new_branch or ""))
