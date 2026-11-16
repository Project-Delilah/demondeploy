"""First-run setup and health checks (`demon setup`, `demon doctor`)."""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import paths, proxy, sysd
from .proc import DemonError, run
from .state import State

SYSCTL_FILE = "/etc/sysctl.d/50-demondeploy.conf"


@dataclass
class Check:
    key: str
    label: str
    ok: bool
    detail: str
    fix: Callable[[], str] | None = None   # returns a message; may raise DemonError
    needs_sudo: bool = False
    required: bool = True


def tilde(p: Path | str) -> str:
    home = str(Path.home())
    s = str(p)
    return "~" + s[len(home):] if s == home or s.startswith(home + "/") else s


def unpriv_port_start() -> int:
    try:
        return int(Path("/proc/sys/net/ipv4/ip_unprivileged_port_start").read_text())
    except (OSError, ValueError):
        return 1024


def _port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("0.0.0.0", port))
            return True
        except OSError:
            return False


def wanted_http_port(st: State) -> tuple[int, str]:
    """(port, reason) the proxy should listen on."""
    if unpriv_port_start() > 80:
        return 8080, "ports 80/443 need a one-time system setting"
    ours = sysd.is_active(paths.PROXY_UNIT) and proxy.http_port(st) == 80
    if not ours and not (_port_free(80) and _port_free(443)):
        return 8080, "ports 80/443 are used by another program"
    return 80, "listening on 80/443"


# ------------------------------------------------------------------ fixes
LINGER_MSG = "apps now start at boot, even before you log in"


def try_without_sudo(c: "Check") -> str | None:
    """Some 'sudo' fixes often work without it (polkit). Returns a message on success."""
    if c.key == "linger":
        user = os.environ.get("USER") or os.getlogin()
        cp = run(["loginctl", "enable-linger", user], check=False, timeout=60)
        if cp.returncode == 0 and sysd.linger_enabled():
            return LINGER_MSG
    return None


def fix_linger() -> str:
    user = os.environ.get("USER") or os.getlogin()
    sudo(["loginctl", "enable-linger", user])
    return LINGER_MSG


def fix_caddy(progress=lambda m: None) -> str:
    dest = proxy.download_caddy(progress)
    return f"installed Caddy {proxy.caddy_version(dest)} to {dest}"


def fix_ports() -> str:
    script = (f"echo 'net.ipv4.ip_unprivileged_port_start=80' > {SYSCTL_FILE} && "
              "sysctl -q -w net.ipv4.ip_unprivileged_port_start=80")
    sudo(["sh", "-c", script])
    return "ports 80 and 443 can now be used without root (setting persists across reboots)"


def fix_proxy() -> str:
    st = State()
    caddy = proxy.find_caddy()
    if caddy is None:
        raise DemonError("Install Caddy first.")
    port, _ = wanted_http_port(st)
    st.set_meta("http_port", str(port))
    proxy.install_unit(caddy)
    sysd.enable(paths.PROXY_UNIT, now=False)
    sysd.restart(paths.PROXY_UNIT)
    if not sysd.is_active(paths.PROXY_UNIT):
        raise DemonError("The proxy did not start.", sysd.journal(paths.PROXY_UNIT, 30))
    return f"proxy running on port {port}"


def fix_poll() -> str:
    service, timer = sysd.poll_units()
    sysd.write_unit(paths.POLL_SERVICE, service)
    sysd.write_unit(paths.POLL_TIMER, timer)
    sysd.daemon_reload()
    sysd.enable(paths.POLL_TIMER, now=True)
    return "git auto-deploy checks every minute"


def sudo(cmd: list[str]) -> None:
    """Run a command with sudo on the user's terminal (may prompt for a password)."""
    if os.geteuid() == 0:
        full = cmd
    elif shutil.which("sudo"):
        full = ["sudo", *cmd]
    elif shutil.which("doas"):
        full = ["doas", *cmd]
    else:
        raise DemonError("sudo is not available; run as root: " + " ".join(cmd))
    print(f"\n  demondeploy needs administrator rights for:\n    {' '.join(cmd)}\n", flush=True)
    rc = subprocess.call(full)
    if rc != 0:
        raise DemonError(f"`{' '.join(full)}` failed (exit {rc}).")


# ------------------------------------------------------------------ checks
def checks(st: State | None = None) -> list[Check]:
    st = st or State()
    out: list[Check] = []

    err = sysd.user_manager_ok()
    out.append(Check("systemd", "systemd user services", err is None,
                     "available" if err is None else f"unavailable: {err}"))
    if err is not None:
        return out

    lg = sysd.linger_enabled()
    out.append(Check("linger", "Start apps at boot (linger)", lg,
                     "enabled" if lg else "apps would stop when you log out", fix_linger,
                     needs_sudo=True))

    caddy = proxy.find_caddy()
    out.append(Check("caddy", "Caddy proxy binary", caddy is not None,
                     f"{tilde(caddy)} ({proxy.caddy_version(caddy)})" if caddy else
                     f"not installed: will download Caddy {proxy.CADDY_VERSION} (checksum verified)",
                     fix_caddy))

    low = unpriv_port_start() <= 80
    out.append(Check("ports", "Ports 80/443 (HTTPS for custom domains)", low,
                     "allowed" if low else "off: apps use :8080, domains get no HTTPS",
                     fix_ports, needs_sudo=True, required=False))

    want, why = wanted_http_port(st)
    installed = (paths.unit_dir() / paths.PROXY_UNIT).exists()
    active = installed and sysd.is_active(paths.PROXY_UNIT)
    cur = proxy.http_port(st)
    up_to_date = active and str(want) == st.get_meta("http_port")
    unit_ok = installed and caddy is not None and str(caddy) in (paths.unit_dir() / paths.PROXY_UNIT).read_text() \
        and paths.python_cmd()[0] in (paths.unit_dir() / paths.PROXY_UNIT).read_text()
    out.append(Check("proxy", "Proxy service", bool(up_to_date and unit_ok),
                     f"running on port {cur}" if up_to_date and unit_ok else
                     ("not running" if not active else
                      f"needs update ({why}, port {want})"), fix_proxy))

    timer = sysd.is_active(paths.POLL_TIMER)
    out.append(Check("poll", "Git auto-deploy timer", timer, "active" if timer else "not installed",
                     fix_poll))

    for tool, why in (("git", "git sources"), ("node", "Node apps"), ("uv", "Python apps")):
        found = shutil.which(tool)
        out.append(Check(f"tool-{tool}", f"{tool}", bool(found),
                         found or f"not found (needed for {why})", required=False))
    return out


def is_ready(st: State | None = None) -> bool:
    return all(c.ok for c in checks(st) if c.required)


def run_setup(interactive: bool = True, out: Callable[[str], None] = print,
              ask: Callable[[str], bool] | None = None) -> bool:
    """Fix everything that can be fixed. Returns True when all required checks pass."""
    done: set[str] = set()
    for _ in range(2):
        pending = [c for c in checks() if not c.ok and c.fix and c.key not in done]
        if not pending:
            break
        for c in pending:
            done.add(c.key)
            if c.needs_sudo and (msg := try_without_sudo(c)):
                out(f"  ✓ {c.label}: {msg}")
                continue
            if c.needs_sudo and ask is not None and not ask(f"{c.label}: {c.detail}\n  Fix it now (uses sudo)?"):
                out(f"  - skipped: {c.label}")
                continue
            if c.needs_sudo and not interactive:
                out(f"  - skipped (needs sudo, run `demon setup`): {c.label}")
                continue
            try:
                out(f"  ✓ {c.label}: {c.fix()}")
                done.discard("proxy")  # later fixes (ports) may need the proxy updated again
            except DemonError as e:
                out(f"  ✗ {c.label}: {e}")
        # recompute: e.g. ports fix changes the proxy's wanted port
    final = checks()
    return all(c.ok for c in final if c.required)
