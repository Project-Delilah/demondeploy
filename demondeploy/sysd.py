"""systemd --user: unit files, systemctl, systemd-run and journalctl."""

from __future__ import annotations

import os
import shlex
import subprocess
import time
from pathlib import Path

from . import paths
from .proc import DemonError, run


def systemctl(*args: str, check: bool = True, timeout: float = 60) -> subprocess.CompletedProcess[str]:
    return run(["systemctl", "--user", "--no-ask-password", *args], check=check, timeout=timeout)


def daemon_reload() -> None:
    systemctl("daemon-reload")


def is_active(unit: str) -> bool:
    return systemctl("is-active", "--quiet", unit, check=False).returncode == 0


def state(unit: str) -> str:
    """active / activating / inactive / failed / deactivating / unknown."""
    out = systemctl("show", "-p", "ActiveState", "--value", unit, check=False).stdout.strip()
    return out or "unknown"


def show(unit: str, *props: str) -> dict[str, str]:
    cp = systemctl("show", unit, *[f"-p{p}" for p in props], check=False)
    res = {}
    for line in cp.stdout.splitlines():
        k, _, v = line.partition("=")
        res[k] = v
    return res


def is_enabled(unit: str) -> bool:
    return systemctl("is-enabled", "--quiet", unit, check=False).returncode == 0


def enable(unit: str, now: bool = True) -> None:
    systemctl("enable", *(["--now"] if now else []), unit, timeout=120)


def start(unit: str) -> None:
    systemctl("start", unit, timeout=120)


def restart(unit: str) -> None:
    systemctl("restart", unit, timeout=120)


def stop_disable(unit: str) -> None:
    """Best effort: stop, disable and forget a unit."""
    systemctl("disable", "--now", unit, check=False, timeout=120)
    systemctl("reset-failed", unit, check=False)


def remove_unit(unit: str) -> None:
    stop_disable(unit)
    f = paths.unit_dir() / unit
    if f.exists():
        f.unlink()
    daemon_reload()


def write_unit(name: str, text: str) -> Path:
    path = paths.unit_dir() / name
    paths.atomic_write(path, text)
    return path


def list_unit_files(prefix: str) -> list[str]:
    d = paths.unit_dir()
    if not d.is_dir():
        return []
    return sorted(p.name for p in d.iterdir() if p.name.startswith(prefix) and p.suffix == ".service")


def q(arg: str | Path) -> str:
    """Quote a value for a systemd unit file line (spec quoting + % escaping)."""
    s = str(arg).replace("%", "%%")
    if any(c in s for c in ' \t"\'\\$;'):
        s = '"' + s.replace("\\", "\\\\").replace('"', '\\"').replace("$", "$$") + '"'
    return s


def app_unit(description: str, workdir: Path, env_file: Path, script: Path) -> str:
    return f"""# managed by demondeploy -- do not edit
[Unit]
Description={description}
After=network-online.target
StartLimitIntervalSec=120
StartLimitBurst=10

[Service]
Type=simple
WorkingDirectory={q(workdir)}
EnvironmentFile={q(env_file)}
ExecStart=/bin/sh {q(script)}
Restart=always
RestartSec=2
KillMode=mixed
KillSignal=SIGTERM
TimeoutStopSec=15
NoNewPrivileges=yes
PrivateTmp=yes
SyslogIdentifier={description.split()[0]}

[Install]
WantedBy=default.target
"""


def proxy_unit(caddy: Path, config: Path) -> str:
    me = " ".join(q(a) for a in paths.python_cmd())
    sock = paths.caddy_admin_sock()
    return f"""# managed by demondeploy -- do not edit
[Unit]
Description=demondeploy proxy (Caddy)
After=network-online.target
Wants=network-online.target

[Service]
Type=notify
ExecStartPre=-{me} _reconcile --boot
ExecStartPre=-/bin/rm -f {q(sock)}
ExecStart={q(caddy)} run --config {q(config)}
ExecReload={q(caddy)} reload --config {q(config)} --address unix/{q(sock)} --force
Restart=always
RestartSec=2
TimeoutStopSec=10
LimitNOFILE=1048576
Environment={q(f"XDG_DATA_HOME={paths.caddy_dir()}")} {q(f"XDG_CONFIG_HOME={paths.caddy_dir()}")}

[Install]
WantedBy=default.target
"""


def poll_units() -> tuple[str, str]:
    me = " ".join(q(a) for a in paths.python_cmd())
    service = f"""# managed by demondeploy -- do not edit
[Unit]
Description=demondeploy: check git sources for new commits

[Service]
Type=oneshot
ExecStart={me} _poll
TimeoutStartSec=300
"""
    timer = """# managed by demondeploy -- do not edit
[Unit]
Description=demondeploy: poll git sources every minute

[Timer]
OnBootSec=90
OnUnitActiveSec=60
AccuracySec=10

[Install]
WantedBy=timers.target
"""
    return service, timer


def run_transient(unit: str, cmd: list[str], *, workdir: Path, env: dict[str, str],
                  props: dict[str, str] | None = None) -> None:
    """Start `cmd` as a transient user service (detached; survives the terminal)."""
    args = ["systemd-run", "--user", "--no-ask-password", f"--unit={unit}", "--collect",
            "--quiet", f"--working-directory={workdir}", "-p", "KillMode=mixed",
            "-p", "TimeoutStopSec=30"]
    for k, v in (props or {}).items():
        args += ["-p", f"{k}={v}"]
    for k, v in env.items():
        args.append(f"--setenv={k}={v}")
    run([*args, "--", *cmd], timeout=30)


def journal(unit: str, lines: int = 200) -> str:
    cp = run(["journalctl", "--user", "-u", unit, "-n", str(lines), "-o", "short-iso",
              "--no-pager", "-q"], check=False, timeout=20)
    return cp.stdout


def journal_follow(unit: str, lines: int = 200) -> subprocess.Popen[str]:
    return subprocess.Popen(
        ["journalctl", "--user", "-u", unit, "-n", str(lines), "-f", "-o", "short-iso",
         "--no-pager", "-q"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, text=True,
        start_new_session=True)


def wait_inactive(unit: str, timeout: float = 30) -> None:
    end = time.monotonic() + timeout
    while time.monotonic() < end and state(unit) in ("active", "activating", "deactivating"):
        time.sleep(0.2)


def linger_enabled() -> bool:
    user = os.environ.get("USER") or os.getlogin()
    cp = run(["loginctl", "show-user", user, "-p", "Linger", "--value"], check=False, timeout=10)
    if cp.returncode == 0:
        return cp.stdout.strip() == "yes"
    return Path(f"/var/lib/systemd/linger/{user}").exists()


def user_manager_ok() -> str | None:
    """None when `systemctl --user` works, else an explanation."""
    try:
        cp = systemctl("is-system-running", check=False, timeout=10)
    except DemonError as e:
        return str(e)
    if cp.returncode != 0 and not cp.stdout.strip():
        return (cp.stderr or "systemd user manager unreachable").strip()
    return None


def cmdline(cmd: list[str]) -> str:
    return shlex.join(cmd)
