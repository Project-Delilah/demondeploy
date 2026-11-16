"""Plain data types and formatting helpers."""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field

# deployment states
QUEUED, BUILDING, STARTING, ACTIVE, SUPERSEDED, FAILED, CANCELLED = (
    "queued", "building", "starting", "active", "superseded", "failed", "cancelled")
IN_PROGRESS = (QUEUED, BUILDING, STARTING)

# app kinds
NODE, PYTHON, STATIC, COMMAND = "node", "python", "static", "command"
KINDS = (NODE, PYTHON, STATIC, COMMAND)

# pipeline steps, in order (shown in the deploy screen)
STEPS = ("fetch", "install", "build", "start", "health", "live")

NAME_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,38}[a-z0-9])?$")


@dataclass(slots=True)
class Project:
    name: str
    source_type: str          # "dir" | "git"
    source: str               # absolute path or git URL
    branch: str = ""
    autodeploy: bool = False
    kind: str = COMMAND
    install_cmd: str = ""
    build_cmd: str = ""
    start_cmd: str = ""
    static_dir: str = ""
    health_path: str = "/"
    active_id: int | None = None
    stopped: bool = False
    last_sha: str = ""
    created: float = field(default_factory=time.time)

    @property
    def is_static(self) -> bool:
        return self.kind == STATIC


@dataclass(slots=True)
class Deployment:
    id: int
    project: str
    status: str
    step: str = ""
    kind: str = COMMAND
    start_cmd: str = ""
    static_dir: str = ""      # absolute path of the served dir (static apps)
    port: int | None = None
    unit: str = ""
    sha: str = ""
    message: str = ""
    error: str = ""
    created: float = 0.0
    finished: float | None = None

    @property
    def done(self) -> bool:
        return self.status not in IN_PROGRESS


def slugify(s: str) -> str:
    s = re.sub(r"[^a-z0-9-]+", "-", s.lower()).strip("-")
    s = re.sub(r"-{2,}", "-", s)[:40].strip("-")
    return s or "app"


def human_size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def human_time(seconds: float | None) -> str:
    if seconds is None or seconds < 0:
        return "--:--"
    s = int(seconds)
    h, rem = divmod(s, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def ago(ts: float | None, now: float | None = None) -> str:
    if not ts:
        return "never"
    d = (now or time.time()) - ts
    if d < 60:
        return "just now"
    for size, unit in ((86400 * 365, "y"), (86400 * 30, "mo"), (86400, "d"), (3600, "h"), (60, "m")):
        if d >= size:
            return f"{int(d // size)}{unit} ago"
    return "just now"


def plural(n: int, word: str) -> str:
    return f"{n:,} {word}{'' if n == 1 else 's'}"
