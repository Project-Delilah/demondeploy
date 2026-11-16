"""One wrapper for every external command, so failures become readable errors."""

from __future__ import annotations

import shlex
import subprocess
from pathlib import Path
from typing import Mapping, Sequence


class DemonError(Exception):
    """An error meant to be shown to the user (no traceback)."""

    def __init__(self, message: str, detail: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail.strip()

    def __str__(self) -> str:
        return f"{self.message}\n{self.detail}" if self.detail else self.message


def run(cmd: Sequence[str | Path], *, check: bool = True, timeout: float | None = 60,
        cwd: Path | str | None = None, env: Mapping[str, str] | None = None,
        input: str | None = None) -> subprocess.CompletedProcess[str]:
    args = [str(c) for c in cmd]
    try:
        cp = subprocess.run(args, capture_output=True, text=True, timeout=timeout,
                            cwd=cwd, env=dict(env) if env is not None else None,
                            input=input, stdin=None if input is not None else subprocess.DEVNULL)
    except FileNotFoundError:
        raise DemonError(f"Command not found: {args[0]}") from None
    except subprocess.TimeoutExpired:
        raise DemonError(f"Timed out after {timeout:.0f}s: {shlex.join(args)}") from None
    if check and cp.returncode != 0:
        raise DemonError(f"Command failed ({cp.returncode}): {shlex.join(args)}",
                         (cp.stderr or cp.stdout)[-2000:])
    return cp
