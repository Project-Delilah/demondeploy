"""Filesystem locations. Everything lives under the user's XDG dirs.

DEMON_HOME overrides the data dir (used by tests); DEMON_UNIT_DIR overrides
where systemd unit files are written.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

UNIT_PREFIX = "demon-"
PROXY_UNIT = "demon-proxy.service"
POLL_SERVICE = "demon-poll.service"
POLL_TIMER = "demon-poll.timer"


def _xdg(var: str, fallback: str) -> Path:
    v = os.environ.get(var)
    return Path(v) if v else Path.home() / fallback


def data_dir() -> Path:
    v = os.environ.get("DEMON_HOME")
    return Path(v) if v else _xdg("XDG_DATA_HOME", ".local/share") / "demondeploy"


def unit_dir() -> Path:
    v = os.environ.get("DEMON_UNIT_DIR")
    return Path(v) if v else _xdg("XDG_CONFIG_HOME", ".config") / "systemd" / "user"


def db_path() -> Path:
    return data_dir() / "state.db"


def bin_dir() -> Path:
    return data_dir() / "bin"


def caddy_dir() -> Path:
    return data_dir() / "caddy"


def caddy_config() -> Path:
    return caddy_dir() / "config.json"


def caddy_storage() -> Path:
    return caddy_dir() / "data"


def caddy_admin_sock() -> Path:
    return caddy_dir() / "admin.sock"


def project_dir(name: str) -> Path:
    return data_dir() / "projects" / name


def deployment_dir(name: str, dep_id: int) -> Path:
    return project_dir(name) / "deployments" / str(dep_id)


def python_cmd() -> list[str]:
    """How systemd units should invoke demondeploy itself."""
    if getattr(sys, "frozen", False):  # standalone binary (PyInstaller)
        return [str(Path(sys.executable).resolve())]
    return [sys.executable, "-m", "demondeploy"]


def atomic_write(path: Path, text: str, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp{os.getpid()}")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
