"""The deploy pipeline and app lifecycle.

A deploy runs inside its own transient systemd unit (`demon-deploy-<app>-<id>`),
so it keeps going if the terminal is closed. It writes progress to the state DB
(`status`, `step`) and a log file that the TUI/CLI tail.

Going live is blue/green: the new version starts on a fresh port next to the
old one, must pass a health check, then the proxy is switched over gracefully
and only then is the old version stopped. Any failure leaves the old version
serving traffic.
"""

from __future__ import annotations

import fcntl
import http.client
import os
import random
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator

from . import gitsrc, paths, proxy, sysd
from .detect import apply_overrides, detect, resolve_static_dir
from .model import (ACTIVE, BUILDING, CANCELLED, FAILED, IN_PROGRESS, STARTING, STATIC, SUPERSEDED,
                    Deployment, Project)
from .proc import DemonError
from .state import State

PORT_RANGE = (20000, 29999)
KEEP_DEPLOYMENTS = 5
BUILD_TIMEOUT = 30 * 60
HEALTH_TIMEOUT = 90
LOG_NAME = "deploy.log"


class Cancelled(Exception):
    pass


# ================================================================== helpers
def deploy_unit(name: str, dep_id: int) -> str:
    return f"demon-deploy-{name}-{dep_id}.service"


def log_path(name: str, dep_id: int) -> Path:
    return paths.deployment_dir(name, dep_id) / LOG_NAME


@contextmanager
def file_lock(path: Path, wait: bool = True) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | (0 if wait else fcntl.LOCK_NB))
        except BlockingIOError:
            raise DemonError("Another deploy or change is in progress for this app.") from None
        yield
    finally:
        os.close(fd)


def project_lock(name: str, wait: bool = True):
    return file_lock(paths.project_dir(name) / ".lock", wait)


def runtime_path() -> str:
    """PATH for builds and apps: the user's interactive PATH, saved by the CLI."""
    st = State()
    saved = st.get_meta("path")
    return saved or os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin")


def remember_path(st: State) -> None:
    """Called from interactive commands so background deploys find node, uv, etc."""
    cur = os.environ.get("PATH", "")
    if cur and not os.environ.get("INVOCATION_ID") and st.get_meta("path") != cur:
        st.set_meta("path", cur)


def base_env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items()
           if k in ("HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "TZ", "SHELL",
                    "XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS", "SSH_AUTH_SOCK")}
    env["PATH"] = runtime_path()
    env.setdefault("HOME", str(Path.home()))
    env.setdefault("LANG", "C.UTF-8")
    return env


def _env_line(k: str, v: str) -> str:
    esc = v.replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$").replace("`", "\\`")
    return f'{k}="{esc}"\n'


def alloc_port(st: State) -> int:
    used = st.used_ports()
    lo, hi = PORT_RANGE
    with file_lock(paths.data_dir() / ".ports.lock"):
        for _ in range(500):
            port = random.randint(lo, hi)
            if port in used:
                continue
            ok = True
            for host in ("127.0.0.1", "0.0.0.0"):
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                    try:
                        s.bind((host, port))
                    except OSError:
                        ok = False
                        break
            if ok:
                return port
    raise DemonError("No free port found in 20000-29999.")


# ================================================================== logging
class Log:
    """Writes to the deployment's log file and stdout (the deploy unit's journal)."""

    def __init__(self, path: Path | None) -> None:
        self.f = open(path, "a", buffering=1) if path else None

    def __call__(self, line: str = "") -> None:
        for ln in line.rstrip("\n").split("\n"):
            if self.f:
                self.f.write(ln + "\n")
            print(ln, flush=True)

    def step(self, text: str) -> None:
        self(f"==> {text}")

    def ok(self, text: str) -> None:
        self(f"✓ {text}")

    def err(self, text: str) -> None:
        self(f"!! {text}")

    def close(self) -> None:
        if self.f:
            self.f.close()


# ================================================================== projects
def create_project(st: State, p: Project) -> Project:
    from .model import NAME_RE
    if not NAME_RE.match(p.name):
        raise DemonError(f"Invalid app name '{p.name}'",
                         "Use lowercase letters, digits and dashes (max 40 chars).")
    if st.project(p.name):
        raise DemonError(f"An app named '{p.name}' already exists.")
    st.add_project(p)
    paths.project_dir(p.name).mkdir(parents=True, exist_ok=True)
    return p


# ================================================================== queue / spawn
def queue_deploy(st: State, name: str) -> Deployment:
    """Create a deployment and run it in a transient systemd unit."""
    p = st.project(name)
    if p is None:
        raise DemonError(f"No app named '{name}'.")
    for d in st.in_progress(name):
        if sysd.is_active(deploy_unit(name, d.id)):
            raise DemonError(f"{name} is already deploying (#{d.id}).")
        st.update_deployment(d.id, status=FAILED, error="interrupted", finished=time.time())
    dep = st.new_deployment(name, kind=p.kind, status="queued", step="fetch")
    paths.deployment_dir(name, dep.id).mkdir(parents=True, exist_ok=True)
    env = base_env()
    for k in ("DEMON_HOME", "DEMON_UNIT_DIR", "DEMON_HEALTH_TIMEOUT"):
        if k in os.environ:
            env[k] = os.environ[k]
    env["PYTHONUNBUFFERED"] = "1"
    try:
        sysd.run_transient(deploy_unit(name, dep.id), [*paths.python_cmd(), "_run", str(dep.id)],
                           workdir=paths.data_dir(), env=env,
                           props={"RuntimeMaxSec": str(BUILD_TIMEOUT + 600), "Nice": "5"})
    except DemonError as e:
        st.update_deployment(dep.id, status=FAILED, error=str(e), finished=time.time())
        raise
    return dep


def cancel(st: State, dep_id: int) -> None:
    dep = st.deployment(dep_id)
    if dep and not dep.done:
        sysd.systemctl("stop", deploy_unit(dep.project, dep.id), check=False, timeout=60)
        dep = st.deployment(dep_id)
        if dep and not dep.done:
            st.update_deployment(dep_id, status=CANCELLED, error="cancelled", finished=time.time())


# ================================================================== the pipeline
def run_deployment(dep_id: int) -> int:
    """Entry point of `demon _run ID` (inside the transient unit). Returns exit code."""
    st = State()
    dep = st.deployment(dep_id)
    if dep is None:
        print(f"deployment {dep_id} not found", file=sys.stderr)
        return 2
    name = dep.project
    log = Log(log_path(name, dep_id))

    def on_term(*_):
        raise Cancelled()

    signal.signal(signal.SIGTERM, on_term)
    signal.signal(signal.SIGINT, on_term)
    try:
        with project_lock(name):
            _pipeline(st, dep, log)
        return 0
    except Cancelled:
        log.err("Deploy cancelled. The previous version is still live.")
        st.update_deployment(dep_id, status=CANCELLED, error="cancelled", finished=time.time())
        return 1
    except DemonError as e:
        log.err(e.message)
        if e.detail:
            log(e.detail)
        log.err("Deploy failed. The previous version is still live.")
        st.update_deployment(dep_id, status=FAILED, error=e.message, finished=time.time())
        return 1
    except Exception as e:  # never leave a deployment stuck "building"
        import traceback
        log(traceback.format_exc())
        st.update_deployment(dep_id, status=FAILED, error=f"internal error: {e}", finished=time.time())
        return 1
    finally:
        log.close()
        try:
            prune(st, name)
        except Exception:
            pass


def _pipeline(st: State, dep: Deployment, log: Log) -> None:
    p = st.project(dep.project)
    if p is None:
        raise DemonError("The app was deleted.")
    ddir = paths.deployment_dir(p.name, dep.id)
    src = ddir / "src"
    st.update_deployment(dep.id, status=BUILDING, step="fetch")

    # ---- fetch
    if p.source_type == "git":
        log.step(f"Fetching {p.source} ({p.branch})")
        sha, msg = gitsrc.checkout(p.source, p.branch, paths.project_dir(p.name) / "repo", src)
        st.update_project(p.name, last_sha=sha)
    else:
        log.step(f"Copying {p.source}")
        if not Path(p.source).is_dir():
            raise DemonError(f"Source folder is missing: {p.source}")
        sha, msg = gitsrc.local_info(Path(p.source))
        gitsrc.copy_tree(Path(p.source), src)
    st.update_deployment(dep.id, sha=sha, message=msg[:200])
    log.ok(f"Source ready{f' @ {sha[:7]}' if sha else ''}: {msg}")

    # ---- settings (project settings, overridden by demon.toml in the source)
    from .detect import Plan
    plan = apply_overrides(src, Plan(kind=p.kind, install=p.install_cmd, build=p.build_cmd,
                                     start=p.start_cmd, static_dir=p.static_dir,
                                     health=p.health_path or "/"))
    for n in plan.notes:
        log(f"   {n}")
    st.update_deployment(dep.id, kind=plan.kind, start_cmd=plan.start)

    env = base_env()
    env.update(st.env(p.name))
    env["CI"] = "1"

    # ---- install & build
    for step, cmd in (("install", plan.install), ("build", plan.build)):
        st.update_deployment(dep.id, step=step)
        if not cmd.strip():
            log.step(f"{step.capitalize()}: nothing to do")
            continue
        log.step(f"{'Installing dependencies' if step == 'install' else 'Building'}: {cmd}")
        t = time.monotonic()
        _run_logged(cmd, src, env, log)
        log.ok(f"{step.capitalize()} finished in {time.monotonic() - t:.1f}s")

    # ---- start & go live
    st.update_deployment(dep.id, status=STARTING, step="start")
    if plan.kind == STATIC:
        root = resolve_static_dir(src, plan.static_dir)
        if root is None:
            raise DemonError(f"No folder with an index.html to serve (looked for "
                             f"'{plan.static_dir or 'dist, build, out, public, .'}').")
        os.chmod(root, os.stat(root).st_mode | 0o755)
        st.update_deployment(dep.id, static_dir=str(root))
        log.ok(f"Serving static files from {root.relative_to(src.parent) if root != src else 'src'}")
    elif not plan.start.strip():
        raise DemonError("No start command. Set one in the app settings or demon.toml.")
    dep = st.deployment(dep.id)
    go_live(st, p, dep, log, health_path=plan.health)
    log.ok(f"Live at {proxy.app_url(st, p.name)}")
    for d, _ in st.domains(p.name):
        log.ok(f"Live at {proxy.domain_url(st, d)}")


def _run_logged(cmd: str, cwd: Path, env: dict[str, str], log: Log,
                timeout: float = BUILD_TIMEOUT) -> None:
    proc = subprocess.Popen(["/bin/sh", "-c", cmd], cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                            errors="replace", start_new_session=True, bufsize=1)
    timer = threading.Timer(timeout, lambda: _killpg(proc))
    timer.start()
    try:
        assert proc.stdout
        for line in proc.stdout:
            log(line.rstrip("\n"))
        rc = proc.wait()
    except BaseException:
        _killpg(proc)
        raise
    finally:
        timer.cancel()
    if rc != 0:
        raise DemonError(f"Command exited with code {rc}: {cmd}")


def _killpg(proc: subprocess.Popen) -> None:
    try:
        os.killpg(proc.pid, signal.SIGTERM)
        try:
            proc.wait(10)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


# ================================================================== going live
def write_runtime(st: State, p: Project, dep: Deployment, token: str, port: int) -> str:
    """Write env file, start script and unit for one instance; returns unit name."""
    ddir = paths.deployment_dir(p.name, dep.id)
    src = ddir / "src"
    env = base_env()
    env.update({"PORT": str(port), "HOST": "127.0.0.1", "PYTHONUNBUFFERED": "1"})
    if dep.kind == "node":
        env["NODE_ENV"] = "production"
    env.update(st.env(p.name))
    env["PORT"] = str(port)  # always ours
    env_file = ddir / f"run-{token}.env"
    paths.atomic_write(env_file, "".join(_env_line(k, v) for k, v in env.items()), mode=0o600)
    script = ddir / f"run-{token}.sh"
    paths.atomic_write(script, f"#!/bin/sh\n# demondeploy: {p.name} deployment #{dep.id}\n"
                               f"exec {dep.start_cmd}\n", mode=0o755)
    unit = f"demon-app-{p.name}-{dep.id}-{token}.service"
    sysd.write_unit(unit, sysd.app_unit(f"{p.name} (demondeploy #{dep.id})", src, env_file, script))
    return unit


def unit_token(unit: str) -> str:
    return unit.removesuffix(".service").rsplit("-", 1)[-1]


def remove_instance(name: str, unit: str) -> None:
    if not unit:
        return
    sysd.remove_unit(unit)
    m = unit.removesuffix(".service").split("-")
    try:
        dep_id, token = int(m[-2]), m[-1]
    except (ValueError, IndexError):
        return
    ddir = paths.deployment_dir(name, dep_id)
    for f in (ddir / f"run-{token}.env", ddir / f"run-{token}.sh"):
        f.unlink(missing_ok=True)


def health_check(unit: str, port: int, path: str, log: Log,
                 timeout: float | None = None) -> None:
    timeout = timeout or float(os.environ.get("DEMON_HEALTH_TIMEOUT", HEALTH_TIMEOUT))
    end = time.monotonic() + timeout
    last = ""
    path = path if path.startswith("/") else "/" + path
    while time.monotonic() < end:
        info = sysd.show(unit, "ActiveState", "NRestarts", "Result")
        if info.get("ActiveState") == "failed" or int(info.get("NRestarts") or 0) >= 2:
            raise DemonError("The app crashed while starting.", sysd.journal(unit, 40))
        try:
            c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            c.request("GET", path, headers={"Host": "localhost", "User-Agent": "demondeploy-health"})
            status = c.getresponse().status
            c.close()
            if status < 500:
                log.ok(f"Health check passed (HTTP {status} on {path})")
                return
            last = f"HTTP {status}"
        except (OSError, http.client.HTTPException) as e:
            last = type(e).__name__ if not str(e) else str(e)
        time.sleep(0.5)
    raise DemonError(f"The app did not answer on port {port} within {timeout:.0f}s ({last}).",
                     "Make sure it listens on the PORT environment variable.\n" + sysd.journal(unit, 40))


def go_live(st: State, p: Project, dep: Deployment, log: Log, health_path: str = "/") -> None:
    """Start `dep` next to the current version, health check, switch, stop the old one.

    Caller must hold the project lock."""
    p = st.project(p.name) or p
    old = st.deployment(p.active_id) if p.active_id else None
    old_unit = old.unit if old and old.kind != STATIC else ""
    old_port = old.port if old else None
    new_unit = ""

    if dep.kind != STATIC:
        port = alloc_port(st)
        token = secrets.token_hex(3)
        new_unit = write_runtime(st, p, dep, token, port)
        st.update_deployment(dep.id, step="start")
        log.step(f"Starting on port {port}: {dep.start_cmd}")
        try:
            sysd.daemon_reload()
            sysd.start(new_unit)
            st.update_deployment(dep.id, step="health")
            log.step("Waiting for the app to answer")
            health_check(new_unit, port, health_path, log)
        except BaseException:
            remove_instance(p.name, new_unit)
            raise
        st.update_deployment(dep.id, port=port, unit=new_unit)
    else:
        st.update_deployment(dep.id, step="health", port=None, unit="")

    # ---- switch traffic
    log.step("Switching traffic")
    st.activate(p.name, dep.id)
    try:
        proxy.reload(st)
    except BaseException:
        # put things back exactly as they were
        if old:
            st.activate(p.name, old.id)
            st.update_deployment(old.id, unit=old.unit, port=old_port)
            if old.id != dep.id:
                st.update_deployment(dep.id, status=FAILED, error="proxy reload failed",
                                     finished=time.time())
        else:
            st.update_project(p.name, active_id=None)
        remove_instance(p.name, new_unit)
        try:
            proxy.write_config(st)
        except Exception:
            pass
        raise
    if new_unit:
        sysd.enable(new_unit, now=False)

    # ---- retire the old version (after in-flight requests had a moment)
    if old_unit and old_unit != new_unit:
        time.sleep(1.5)
        remove_instance(p.name, old_unit)
        if old.id != dep.id:
            st.update_deployment(old.id, unit="", port=None)


# ================================================================== lifecycle
def rollback_target(st: State, name: str, dep_id: int | None = None) -> Deployment:
    p = st.project(name)
    if p is None:
        raise DemonError(f"No app named '{name}'.")
    cands = [d for d in st.deployments(name) if d.status == SUPERSEDED
             and (paths.deployment_dir(name, d.id) / "src").is_dir()]
    if dep_id is not None:
        for d in st.deployments(name):
            if d.id == dep_id:
                if d.status == ACTIVE:
                    raise DemonError(f"#{dep_id} is already live.")
                if d not in cands:
                    raise DemonError(f"#{dep_id} can't be restored (status {d.status}, or its files "
                                     "were cleaned up).")
                return d
        raise DemonError(f"{name} has no deployment #{dep_id}.")
    older = [d for d in cands if p.active_id is None or d.id < p.active_id] or cands
    if not older:
        raise DemonError(f"{name} has no earlier deployment to roll back to.")
    return older[0]


def rollback(st: State, name: str, dep_id: int | None = None,
             out: Callable[[str], None] = print) -> Deployment:
    target = rollback_target(st, name, dep_id)
    with project_lock(name, wait=False):
        log = Log(log_path(name, target.id))
        log.f and log.f.write(f"\n--- rollback to #{target.id} at {time.ctime()}\n")
        log_out = _Tee(log, out)
        go_live(st, st.project(name), target, log_out, st.project(name).health_path)
        log.close()
    return target


class _Tee(Log):
    def __init__(self, log: Log, out: Callable[[str], None]) -> None:
        self.f = log.f
        self.out = out

    def __call__(self, line: str = "") -> None:
        for ln in line.rstrip("\n").split("\n"):
            if self.f:
                self.f.write(ln + "\n")
            self.out(ln)


def restart(st: State, name: str, out: Callable[[str], None] = print) -> None:
    """Zero-downtime restart of the live version (picks up env var changes)."""
    p = st.project(name)
    if p is None or p.active_id is None:
        raise DemonError(f"{name} has no live deployment.")
    dep = st.deployment(p.active_id)
    if dep.kind == STATIC:
        proxy.reload(st)
        return
    with project_lock(name, wait=False):
        if p.stopped:
            start(st, name)
            return
        go_live(st, p, dep, _Tee(Log(None), out), p.health_path)


def stop(st: State, name: str) -> None:
    p = st.project(name)
    if p is None:
        raise DemonError(f"No app named '{name}'.")
    with project_lock(name, wait=False):
        st.update_project(name, stopped=True)
        proxy.reload(st)
        dep = st.deployment(p.active_id) if p.active_id else None
        if dep and dep.unit:
            sysd.systemctl("disable", "--now", dep.unit, check=False, timeout=120)


def start(st: State, name: str) -> None:
    p = st.project(name)
    if p is None:
        raise DemonError(f"No app named '{name}'.")
    dep = st.deployment(p.active_id) if p.active_id else None
    if dep is None:
        raise DemonError(f"{name} has no successful deployment to start. Deploy it first.")
    with project_lock(name, wait=False):
        if dep.kind != STATIC:
            if not dep.unit or not (paths.unit_dir() / dep.unit).exists():
                port = alloc_port(st)
                unit = write_runtime(st, p, dep, secrets.token_hex(3), port)
                st.update_deployment(dep.id, unit=unit, port=port)
                dep = st.deployment(dep.id)
            sysd.daemon_reload()
            sysd.systemctl("reset-failed", dep.unit, check=False)
            sysd.enable(dep.unit, now=True)
            health_check(dep.unit, dep.port, p.health_path, Log(None))
        st.update_project(name, stopped=False)
        proxy.reload(st)


def remove(st: State, name: str) -> None:
    p = st.project(name)
    if p is None:
        raise DemonError(f"No app named '{name}'.")
    for d in st.in_progress(name):
        cancel(st, d.id)
    with project_lock(name):
        for unit in sysd.list_unit_files(f"demon-app-{name}-"):
            if _unit_project(unit, {name}) == name:
                sysd.remove_unit(unit)
        st.delete_project(name)
        proxy.reload(st)
    shutil.rmtree(paths.project_dir(name), ignore_errors=True)


def prune(st: State, name: str) -> None:
    p = st.project(name)
    if p is None:
        return
    keep: set[int] = {p.active_id} if p.active_id else set()
    good = [d for d in st.deployments(name, limit=1000) if d.status == SUPERSEDED]
    keep |= {d.id for d in good[:KEEP_DEPLOYMENTS]}
    keep |= {d.id for d in st.in_progress(name)}
    root = paths.project_dir(name) / "deployments"
    if not root.is_dir():
        return
    for d in root.iterdir():
        if not d.name.isdigit() or int(d.name) in keep:
            continue
        # keep the log for history, drop everything else
        for child in d.iterdir():
            if child.name == LOG_NAME:
                continue
            if child.is_dir() and not child.is_symlink():
                shutil.rmtree(child, ignore_errors=True)
            else:
                child.unlink(missing_ok=True)


# ================================================================== reconcile & poll
def _unit_project(unit: str, names: set[str]) -> str | None:
    """Which project owns demon-app-<name>-<id>-<tok>.service (names may contain dashes)."""
    base = unit.removeprefix("demon-app-").removesuffix(".service")
    parts = base.rsplit("-", 2)
    return parts[0] if len(parts) == 3 and parts[0] in names else None


def reconcile(st: State, boot: bool = False, out: Callable[[str], None] = lambda s: None) -> list[str]:
    """Make systemd + proxy match the DB. Safe to run any time. Returns actions taken."""
    actions: list[str] = []

    def act(msg: str) -> None:
        actions.append(msg)
        out(msg)

    # deployments whose deploy process is gone
    for d in st.in_progress():
        if not sysd.is_active(deploy_unit(d.project, d.id)):
            st.update_deployment(d.id, status=FAILED, error="interrupted (deploy process stopped)",
                                 finished=time.time())
            act(f"{d.project}: marked interrupted deploy #{d.id} as failed")

    projects = {p.name: p for p in st.projects()}
    busy = {d.project for d in st.in_progress()}
    expected: set[str] = set()
    changed_units = False
    for p in projects.values():
        dep = st.deployment(p.active_id) if p.active_id else None
        if not dep or dep.kind == STATIC:
            continue
        if not (paths.deployment_dir(p.name, dep.id) / "src").is_dir():
            act(f"{p.name}: files of live deployment #{dep.id} are missing; redeploy needed")
            continue
        if not dep.unit or not dep.port:
            if p.stopped:
                continue
            port = alloc_port(st)
            unit = write_runtime(st, p, dep, secrets.token_hex(3), port)
            st.update_deployment(dep.id, unit=unit, port=port)
            dep = st.deployment(dep.id)
            changed_units = True
            act(f"{p.name}: recreated service {unit}")
        elif not (paths.unit_dir() / dep.unit).exists():
            write_runtime(st, p, dep, unit_token(dep.unit), dep.port)
            changed_units = True
            act(f"{p.name}: restored missing unit file {dep.unit}")
        expected.add(dep.unit)

    for unit in sysd.list_unit_files("demon-app-"):
        owner = _unit_project(unit, set(projects))
        if unit in expected or (owner in busy):
            continue
        remove_instance(owner or "", unit) if owner else sysd.remove_unit(unit)
        act(f"removed stray service {unit}")

    if changed_units:
        sysd.daemon_reload()

    for p in projects.values():
        dep = st.deployment(p.active_id) if p.active_id else None
        if not dep or not dep.unit or dep.unit not in expected:
            continue
        if p.stopped:
            if sysd.is_enabled(dep.unit) or sysd.is_active(dep.unit):
                sysd.systemctl("disable", "--now", dep.unit, check=False)
                act(f"{p.name}: stopped (app is marked stopped)")
            continue
        if not sysd.is_enabled(dep.unit):
            sysd.enable(dep.unit, now=False)
            act(f"{p.name}: enabled {dep.unit} for boot")
        if sysd.state(dep.unit) in ("inactive", "failed"):
            sysd.systemctl("reset-failed", dep.unit, check=False)
            sysd.systemctl("start", dep.unit, check=False, timeout=120)
            act(f"{p.name}: started {dep.unit}")

    if boot:
        proxy.write_config(st)
    else:
        try:
            proxy.reload(st)
        except DemonError as e:
            act(f"proxy reload failed: {e.message}")
    for name in projects:
        prune(st, name)
    return actions


def poll(st: State, out: Callable[[str], None] = print) -> None:
    for p in st.projects():
        if p.source_type != "git" or not p.autodeploy or p.stopped:
            continue
        if st.in_progress(p.name):
            continue
        try:
            sha = gitsrc.remote_sha(p.source, p.branch)
        except DemonError as e:
            out(f"{p.name}: {e.message}")
            continue
        if sha != p.last_sha:
            # don't retry the same failing commit forever
            recent = st.deployments(p.name, limit=1)
            if recent and recent[0].sha == sha and recent[0].status in (FAILED, CANCELLED):
                continue
            out(f"{p.name}: new commit {sha[:7]} on {p.branch}, deploying")
            st.update_project(p.name, last_sha=sha)
            try:
                queue_deploy(st, p.name)
            except DemonError as e:
                out(f"{p.name}: {e.message}")
