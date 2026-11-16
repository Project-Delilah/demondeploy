"""Fetching sources: git repositories and local folders."""

from __future__ import annotations

import os
import re
import shutil
from pathlib import Path

from .proc import DemonError, run

GIT_ENV = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "/bin/true",
           "GIT_SSH_COMMAND": os.environ.get("GIT_SSH_COMMAND", "ssh -o BatchMode=yes -o ConnectTimeout=15")}

COPY_IGNORE = shutil.ignore_patterns(
    ".git", "node_modules", ".venv", "venv", "__pycache__", "*.pyc", ".next", ".nuxt", ".output",
    ".svelte-kit", ".turbo", ".cache", ".parcel-cache", ".pytest_cache", ".mypy_cache", ".DS_Store")

_URL = re.compile(r"^(https?://|ssh://|git://|file://|git@[\w.-]+:|[\w.-]+@[\w.-]+:)")


def looks_like_git_url(s: str) -> bool:
    return bool(_URL.match(s)) or (s.endswith(".git") and not Path(s).exists())


def repo_name(url: str) -> str:
    tail = url.rstrip("/").rsplit("/", 1)[-1].rsplit(":", 1)[-1]
    return tail.removesuffix(".git") or "app"


def git(*args: str | Path, cwd: Path | None = None, timeout: float = 300) -> str:
    return run(["git", *map(str, args)], cwd=cwd, env=GIT_ENV, timeout=timeout).stdout.strip()


def remote_sha(url: str, branch: str) -> str:
    ref = f"refs/heads/{branch}" if branch else "HEAD"
    out = git("ls-remote", url, ref, timeout=60)
    if not out:
        raise DemonError(f"Branch '{branch or 'HEAD'}' not found in {url}")
    return out.split()[0]


def default_branch(url: str) -> str:
    out = git("ls-remote", "--symref", url, "HEAD", timeout=60)
    m = re.search(r"ref: refs/heads/(\S+)\s+HEAD", out)
    return m.group(1) if m else "main"


def checkout(url: str, branch: str, cache: Path, dest: Path) -> tuple[str, str]:
    """Update the clone in `cache` to the tip of `branch` and copy it to `dest`.

    Returns (sha, commit subject)."""
    if not (cache / ".git").is_dir():
        if cache.exists():
            shutil.rmtree(cache)
        cache.parent.mkdir(parents=True, exist_ok=True)
        git("clone", "--depth", "1", "--branch", branch, "--single-branch",
            "--no-tags", url, cache, timeout=900)
    else:
        git("remote", "set-url", "origin", url, cwd=cache)
        git("fetch", "--depth", "1", "--no-tags", "origin", branch, cwd=cache, timeout=900)
        git("reset", "--hard", "FETCH_HEAD", cwd=cache)
        git("clean", "-ffdx", cwd=cache)
    if (cache / ".gitmodules").exists():
        git("submodule", "update", "--init", "--recursive", "--depth", "1", cwd=cache, timeout=900)
    sha = git("rev-parse", "HEAD", cwd=cache)
    subject = git("log", "-1", "--format=%s", cwd=cache)
    copy_tree(cache, dest)
    return sha, subject


def copy_tree(src: Path, dest: Path) -> None:
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(src, dest, symlinks=True, ignore=COPY_IGNORE)


def local_info(path: Path) -> tuple[str, str]:
    """(sha, message) for a local folder; sha is empty when it is not a git checkout."""
    try:
        sha = git("rev-parse", "HEAD", cwd=path, timeout=10)
        subject = git("log", "-1", "--format=%s", cwd=path, timeout=10)
        dirty = git("status", "--porcelain", cwd=path, timeout=20)
        return sha, subject + (" (+ uncommitted changes)" if dirty else "")
    except DemonError:
        return "", "local folder"
