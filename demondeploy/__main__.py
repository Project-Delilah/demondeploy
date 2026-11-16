"""`demon` command line. With no arguments it opens the TUI dashboard."""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

from . import __version__

EPILOG = """\
commands:
  demon                          open the dashboard (TUI)
  demon deploy [PATH|GIT_URL|APP] deploy a folder or repo, or redeploy an app
  demon ls                       list apps
  demon status APP               details of one app
  demon logs APP [-f] [--build]  app output (or the last build log)
  demon rollback APP [ID]        go back to the previous (or a given) deployment
  demon restart|stop|start APP   zero-downtime restart / stop / start
  demon env ls|set|rm APP ...    environment variables
  demon domains ls|add|rm ...    custom domains (HTTPS is automatic)
  demon rm APP                   delete an app
  demon setup                    first-run setup (systemd, proxy, ports)
  demon doctor                   check everything and repair what drifted
"""

TTY = sys.stdout.isatty()


def c(text: str, color: str) -> str:
    if not TTY:
        return text
    codes = {"green": "38;2;80;250;123", "red": "38;2;255;85;85", "purple": "38;2;189;147;249",
             "cyan": "38;2;139;233;253", "yellow": "38;2;241;250;140", "dim": "38;2;98;114;164",
             "orange": "38;2;255;184;108", "bold": "1"}
    return f"\033[{codes[color]}m{text}\033[0m"


def fmt_log_line(line: str) -> str:
    if line.startswith("==> "):
        return c(line, "purple")
    if line.startswith("✓ "):
        return c(line, "green")
    if line.startswith("!! "):
        return c(line, "red")
    return line


def die(msg: str, code: int = 1) -> None:
    print(c("error: ", "red") + msg, file=sys.stderr)
    sys.exit(code)


def ask(question: str, default: bool = True) -> bool:
    if not sys.stdin.isatty():
        return default
    suffix = " [Y/n] " if default else " [y/N] "
    try:
        ans = input(question + suffix).strip().lower()
    except EOFError:
        return default
    return default if not ans else ans.startswith("y")


# ------------------------------------------------------------------ helpers
def _state():
    from .deploy import remember_path
    from .state import State
    st = State()
    remember_path(st)
    return st


def ensure_setup(st) -> None:
    from . import setup
    if setup.is_ready(st):
        return
    print(c("demondeploy is not fully set up yet. Running setup…", "yellow"))
    if not setup.run_setup(interactive=sys.stdin.isatty(), ask=ask if sys.stdin.isatty() else None):
        die("setup is incomplete; run `demon doctor` for details")


def follow(st, name: str, dep_id: int) -> int:
    """Print a deployment's log until it finishes. Ctrl-C detaches."""
    from .deploy import log_path
    from .model import ACTIVE
    path = log_path(name, dep_id)
    pos = 0
    try:
        while True:
            if path.exists():
                with open(path, errors="replace") as f:
                    f.seek(pos)
                    chunk = f.read()
                    pos = f.tell()
                for line in chunk.splitlines():
                    print(fmt_log_line(line), flush=True)
            dep = st.deployment(dep_id)
            if dep is None:
                die("deployment disappeared")
            if dep.done:
                time.sleep(0.2)
                if path.exists():
                    with open(path, errors="replace") as f:
                        f.seek(pos)
                        for line in f.read().splitlines():
                            print(fmt_log_line(line), flush=True)
                if dep.status == ACTIVE:
                    return 0
                print(c(f"Deployment #{dep_id} {dep.status}: {dep.error}", "red"))
                return 1
            time.sleep(0.25)
    except KeyboardInterrupt:
        print(c(f"\nDetached. The deploy keeps running; see `demon logs {name} --build -f`.", "dim"))
        return 130


# ------------------------------------------------------------------ commands
def cmd_deploy(a) -> int:
    from . import gitsrc
    from .deploy import create_project, queue_deploy, run_deployment
    from .detect import detect
    from .model import Project, slugify
    st = _state()
    target = a.target or "."

    project = st.project(target) if not os.path.exists(target) else None
    if project is None:
        if gitsrc.looks_like_git_url(target):
            source, stype = target, "git"
        else:
            p = Path(target).expanduser().resolve()
            if not p.is_dir():
                die(f"'{target}' is not a folder, git URL, or app name")
            source, stype = str(p), "dir"
        project = st.project_by_source(source)
        if project and a.name and project.name != a.name:
            project = None

    interactive = TTY and sys.stdin.isatty() and not a.yes
    if project is None and interactive:
        from .app import DemonApp
        DemonApp(new_source=source, new_name=a.name, new_branch=a.branch).run()
        return 0

    ensure_setup(st)
    if project is None:
        branch = a.branch or ""
        if stype == "git":
            print(c(f"==> Checking {source}", "purple"))
            branch = branch or gitsrc.default_branch(source)
            gitsrc.remote_sha(source, branch)
            import tempfile
            with tempfile.TemporaryDirectory() as tmp:
                gitsrc.checkout(source, branch, Path(tmp) / "repo", Path(tmp) / "src")
                plan = detect(Path(tmp) / "src")
            name = a.name or slugify(gitsrc.repo_name(source))
        else:
            plan = detect(Path(source))
            name = a.name or slugify(Path(source).name)
        for attr, val in (("install", a.install), ("build", a.build), ("start", a.start),
                          ("static_dir", a.static)):
            if val is not None:
                setattr(plan, attr, val)
        print(c(f"==> Detected {plan.label}", "purple"))
        for k, v in (("install", plan.install), ("build", plan.build), ("start", plan.start),
                     ("serve", plan.static_dir)):
            if v:
                print(f"    {k:8} {v}")
        for n in plan.notes:
            print(c(f"    note: {n}", "yellow"))
        project = create_project(st, Project(
            name=name, source_type=stype, source=source, branch=branch,
            autodeploy=bool(a.auto) if stype == "git" else False, kind=plan.kind,
            install_cmd=plan.install, build_cmd=plan.build, start_cmd=plan.start,
            static_dir=plan.static_dir, health_path=plan.health))
        print(c(f"✓ Created app {name}", "green"))

    if a.foreground:
        dep = st.new_deployment(project.name, kind=project.kind)
        from . import paths
        paths.deployment_dir(project.name, dep.id).mkdir(parents=True, exist_ok=True)
        return run_deployment(dep.id)
    dep = queue_deploy(st, project.name)
    print(c(f"==> Deploying {project.name} (#{dep.id})", "bold"))
    if a.detach:
        print(f"    follow with: demon logs {project.name} --build -f")
        return 0
    return follow(st, project.name, dep.id)


def cmd_ls(a) -> int:
    from . import proxy, sysd
    from .model import ago
    st = _state()
    ps = st.projects()
    if not ps:
        print("No apps yet. Deploy one with: demon deploy ./my-app")
        return 0
    rows = []
    for p in ps:
        rows.append((p.name, app_status(st, p), proxy.app_url(st, p.name),
                     ago(st.deployment(p.active_id).created if p.active_id else None)))
    w = [max(len(r[i]) for r in rows) for i in range(4)]
    print(c(f"{'APP':{w[0]}}  {'STATUS':{w[1]}}  {'URL':{w[2]}}  DEPLOYED", "dim"))
    for r in rows:
        color = {"running": "green", "failed": "red", "deploying": "purple"}.get(r[1], "dim")
        print(f"{r[0]:{w[0]}}  {c(f'{r[1]:{w[1]}}', color)}  {c(f'{r[2]:{w[2]}}', 'cyan')}  {r[3]}")
    return 0


def app_status(st, p) -> str:
    """running / deploying / stopped / failed / crashed / new."""
    from . import sysd
    from .model import STATIC
    if st.in_progress(p.name):
        return "deploying"
    if p.active_id is None:
        last = st.deployments(p.name, limit=1)
        return "failed" if last else "new"
    if p.stopped:
        return "stopped"
    dep = st.deployment(p.active_id)
    if dep.kind == STATIC:
        return "running"
    s = sysd.state(dep.unit) if dep.unit else "inactive"
    return {"active": "running", "activating": "restarting", "failed": "crashed"}.get(s, s)


def cmd_status(a) -> int:
    from . import proxy, sysd
    from .model import ago, human_size
    st = _state()
    p = st.project(a.app) or die(f"no app named '{a.app}'")
    print(c(p.name, "bold") + "  " + app_status(st, p))
    print(f"  source   {p.source}" + (f" ({p.branch}{', auto-deploy' if p.autodeploy else ''})"
                                      if p.source_type == "git" else ""))
    print(f"  type     {p.kind}")
    for k, v in (("install", p.install_cmd), ("build", p.build_cmd), ("start", p.start_cmd),
                 ("serve", p.static_dir)):
        if v:
            print(f"  {k:8} {v}")
    print(f"  url      {c(proxy.app_url(st, p.name), 'cyan')}")
    for d, _ in st.domains(p.name):
        print(f"  domain   {c(proxy.domain_url(st, d), 'cyan')}  ({proxy.cert_status(d)})")
    if p.active_id:
        dep = st.deployment(p.active_id)
        if dep.unit:
            info = sysd.show(dep.unit, "MemoryCurrent", "ActiveEnterTimestamp", "MainPID")
            mem = info.get("MemoryCurrent", "")
            print(f"  service  {dep.unit} (pid {info.get('MainPID')}, port {dep.port}"
                  + (f", {human_size(int(mem))}" if mem.isdigit() else "") + ")")
    print(c("  deployments", "dim"))
    for d in st.deployments(p.name, limit=8):
        mark = "●" if d.id == p.active_id else " "
        print(f"  {mark} #{d.id:<4} {d.status:10} {ago(d.created):10} {d.sha[:7]:7} {d.message[:50]}"
              + (c(f"  {d.error}", "red") if d.error else ""))
    return 0


def cmd_logs(a) -> int:
    import subprocess
    from .deploy import log_path
    st = _state()
    p = st.project(a.app) or die(f"no app named '{a.app}'")
    if a.build is not None or p.active_id is None:
        deps = st.deployments(p.name, limit=50)
        if not deps:
            die("no deployments yet")
        dep = next((d for d in deps if d.id == a.build), None) if a.build else deps[0]
        if dep is None:
            die(f"no deployment #{a.build}")
        if a.follow and not dep.done:
            return follow(st, p.name, dep.id)
        path = log_path(p.name, dep.id)
        if path.exists():
            for line in path.read_text(errors="replace").splitlines()[-a.lines:]:
                print(fmt_log_line(line))
        return 0
    dep = st.deployment(p.active_id)
    if not dep.unit:
        print(c("Static site: there is no running process. Showing the last build log.", "dim"))
        a.build = dep.id
        return cmd_logs(a)
    cmd = ["journalctl", "--user", "-u", dep.unit, "-n", str(a.lines), "-o", "short-iso", "--no-pager"]
    if a.follow:
        cmd.append("-f")
    try:
        return subprocess.call(cmd)
    except KeyboardInterrupt:
        return 0


def cmd_rollback(a) -> int:
    from .deploy import rollback
    st = _state()
    t = rollback(st, a.app, a.id, out=lambda s: print(fmt_log_line(s)))
    print(c(f"✓ {a.app} is now serving #{t.id} ({(t.sha[:7] + ' ') if t.sha else ''}{t.message})", "green"))
    return 0


def cmd_simple(a) -> int:
    from . import deploy
    st = _state()
    fn = {"restart": deploy.restart, "stop": deploy.stop, "start": deploy.start}[a.cmd]
    if a.cmd == "restart":
        fn(st, a.app, out=lambda s: print(fmt_log_line(s)))
    else:
        fn(st, a.app)
    print(c(f"✓ {a.app}: {a.cmd} done", "green"))
    return 0


def cmd_env(a) -> int:
    st = _state()
    st.project(a.app) or die(f"no app named '{a.app}'")
    if a.action == "ls":
        for k, v in st.env(a.app).items():
            print(f"{k}={v if a.reveal else '•' * min(len(v), 8)}")
        return 0
    if a.action == "set":
        for kv in a.items:
            k, sep, v = kv.partition("=")
            if not sep or not k.replace("_", "").isalnum() or k[0].isdigit():
                die(f"expected KEY=VALUE, got '{kv}'")
            st.set_env(a.app, k, v)
            print(c(f"✓ set {k}", "green"))
    else:
        for k in a.items:
            print(c(f"✓ removed {k}", "green") if st.unset_env(a.app, k) else f"{k} was not set")
    if a.restart:
        from .deploy import restart
        restart(st, a.app, out=lambda s: print(fmt_log_line(s)))
    else:
        print(c(f"Applies on the next deploy, or now with: demon restart {a.app} "
                "(redeploy for build-time variables)", "dim"))
    return 0


def cmd_domains(a) -> int:
    from . import domains, proxy
    st = _state()
    if a.action == "ls":
        rows = st.domains(a.app if a.app else None)
        if not rows:
            print("No custom domains.")
        for d, p in rows:
            print(f"{c(proxy.domain_url(st, d), 'cyan')}  → {p}  ({proxy.cert_status(d)})")
        return 0
    if a.action == "add":
        if not a.app or not a.domain:
            die("usage: demon domains add APP DOMAIN")
        ensure_setup(st)
        d = domains.normalize(a.domain)
        print(c(f"==> Checking {d}", "purple"))
        chk = domains.check(st, d)
        for m in chk.ok:
            print(c(f"  ✓ {m}", "green"))
        for m in chk.warn:
            print(c(f"  ! {m}", "yellow"))
        if chk.warn and not a.yes and not ask("Add it anyway?", default=True):
            return 1
        domains.add(st, a.app, d)
        print(c(f"✓ {proxy.domain_url(st, d)} → {a.app}", "green"))
        return 0
    domains.remove(st, a.domain or a.app)
    print(c("✓ removed", "green"))
    return 0


def cmd_rm(a) -> int:
    from .deploy import remove
    st = _state()
    st.project(a.app) or die(f"no app named '{a.app}'")
    if not a.yes and not ask(f"Delete {a.app}, its deployments, env vars and domains?", default=False):
        return 1
    remove(st, a.app)
    print(c(f"✓ deleted {a.app}", "green"))
    return 0


def cmd_setup(a) -> int:
    from . import setup
    _state()
    if TTY and sys.stdin.isatty() and not a.yes:
        from .app import DemonApp
        DemonApp(setup_only=True).run()
        return 0
    ok = setup.run_setup(interactive=sys.stdin.isatty(), ask=None if a.yes else ask)
    return cmd_doctor(a, repair=False) or (0 if ok else 1)


def cmd_doctor(a, repair: bool = True) -> int:
    from . import setup
    from .deploy import reconcile
    st = _state()
    bad = 0
    for chk in setup.checks(st):
        mark = c("✓", "green") if chk.ok else (c("✗", "red") if chk.required else c("!", "yellow"))
        print(f" {mark} {chk.label:42} {c(chk.detail, 'dim')}")
        bad += (not chk.ok and chk.required)
    if repair and not bad:
        actions = reconcile(st)
        print(c("✓ apps match their saved state" if not actions else "repaired:", "green"))
        for x in actions:
            print(f"   - {x}")
    elif bad:
        print(c("Run `demon setup` to fix the items above.", "yellow"))
    return 1 if bad else 0


# ------------------------------------------------------------------ parser
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="demon", description="Deploy apps on your own machine.",
                                epilog=EPILOG, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("-V", "--version", action="version", version=f"demondeploy {__version__}")
    sub = p.add_subparsers(dest="cmd", metavar="COMMAND")

    d = sub.add_parser("deploy", help="deploy a folder, git URL, or existing app")
    d.add_argument("target", nargs="?", help="folder (default .), git URL, or app name")
    d.add_argument("--name", help="app name (default: folder/repo name)")
    d.add_argument("--branch", help="git branch (default: the repo's default branch)")
    d.add_argument("--auto", action=argparse.BooleanOptionalAction, default=True,
                   help="redeploy automatically on new commits (git sources)")
    d.add_argument("--install", help="override install command")
    d.add_argument("--build", help="override build command")
    d.add_argument("--start", help="override start command")
    d.add_argument("--static", help="serve this folder as a static site")
    d.add_argument("-y", "--yes", action="store_true", help="no prompts, no TUI")
    d.add_argument("--detach", action="store_true", help="don't wait for the deploy to finish")
    d.add_argument("--foreground", action="store_true", help=argparse.SUPPRESS)
    d.set_defaults(fn=cmd_deploy)

    sub.add_parser("ls", help="list apps").set_defaults(fn=cmd_ls)
    s = sub.add_parser("status", help="show one app")
    s.add_argument("app")
    s.set_defaults(fn=cmd_status)

    lg = sub.add_parser("logs", help="app output, or a build log with --build")
    lg.add_argument("app")
    lg.add_argument("-f", "--follow", action="store_true")
    lg.add_argument("-n", "--lines", type=int, default=200)
    lg.add_argument("--build", nargs="?", type=int, const=0, default=None, metavar="ID",
                    help="show the build log of the latest (or given) deployment")
    lg.set_defaults(fn=cmd_logs)

    r = sub.add_parser("rollback", help="serve an earlier deployment again")
    r.add_argument("app")
    r.add_argument("id", nargs="?", type=int)
    r.set_defaults(fn=cmd_rollback)

    for name, help_ in (("restart", "zero-downtime restart (applies env changes)"),
                        ("stop", "stop an app"), ("start", "start a stopped app")):
        x = sub.add_parser(name, help=help_)
        x.add_argument("app")
        x.set_defaults(fn=cmd_simple)

    e = sub.add_parser("env", help="environment variables")
    e.add_argument("action", choices=["ls", "set", "rm"])
    e.add_argument("app")
    e.add_argument("items", nargs="*", metavar="KEY=VALUE|KEY")
    e.add_argument("--reveal", action="store_true", help="show values")
    e.add_argument("--restart", action="store_true", help="restart the app to apply now")
    e.set_defaults(fn=cmd_env)

    dm = sub.add_parser("domains", help="custom domains")
    dm.add_argument("action", choices=["ls", "add", "rm"])
    dm.add_argument("app", nargs="?")
    dm.add_argument("domain", nargs="?")
    dm.add_argument("-y", "--yes", action="store_true")
    dm.set_defaults(fn=cmd_domains)

    rm = sub.add_parser("rm", help="delete an app")
    rm.add_argument("app")
    rm.add_argument("-y", "--yes", action="store_true")
    rm.set_defaults(fn=cmd_rm)

    su = sub.add_parser("setup", help="first-run setup")
    su.add_argument("-y", "--yes", action="store_true", help="non-interactive")
    su.set_defaults(fn=cmd_setup)
    sub.add_parser("doctor", help="check and repair").set_defaults(fn=cmd_doctor)

    # internal (used by systemd units)
    ri = sub.add_parser("_run")
    ri.add_argument("id", type=int)
    rc = sub.add_parser("_reconcile")
    rc.add_argument("--boot", action="store_true")
    sub.add_parser("_poll")
    return p


def main(argv: list[str] | None = None) -> None:
    from .proc import DemonError
    a = build_parser().parse_args(argv)
    try:
        if a.cmd == "_run":
            from .deploy import run_deployment
            sys.exit(run_deployment(a.id))
        if a.cmd == "_reconcile":
            from .deploy import reconcile
            from .state import State
            reconcile(State(), boot=a.boot, out=print)
            return
        if a.cmd == "_poll":
            from .deploy import poll
            from .state import State
            poll(State())
            return
        if a.cmd is None:
            _state()
            from .app import DemonApp
            DemonApp().run()
            return
        sys.exit(a.fn(a))
    except DemonError as e:
        die(str(e))
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
