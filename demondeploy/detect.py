"""Work out how to install, build and run a project from the files in it.

An optional `demon.toml` at the project root overrides anything detected:

    kind = "node"            # node | python | static | command
    install = "npm ci"
    build = "npm run build"
    start = "npm start"
    static = "dist"          # directory to serve (static apps)
    health = "/healthz"      # path checked before going live
"""

from __future__ import annotations

import json
import re
import shutil
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .model import COMMAND, KINDS, NODE, PYTHON, STATIC

STATIC_CANDIDATES = ("dist", "build", "out", "public", "_site", "site", ".")


@dataclass(slots=True)
class Plan:
    kind: str = COMMAND
    framework: str = ""
    install: str = ""
    build: str = ""
    start: str = ""
    static_dir: str = ""
    health: str = "/"
    notes: list[str] = field(default_factory=list)

    @property
    def label(self) -> str:
        return f"{self.kind} ({self.framework})" if self.framework else self.kind


def detect(root: Path) -> Plan:
    root = Path(root)
    if (root / "package.json").is_file():
        plan = _node(root)
    elif any((root / f).is_file() for f in ("pyproject.toml", "requirements.txt", "manage.py")):
        plan = _python(root)
    elif (root / "index.html").is_file():
        plan = Plan(kind=STATIC, framework="html", static_dir=".")
    else:
        plan = Plan(kind=COMMAND, notes=["Could not detect the app type: enter the commands to "
                                         "build and run it. The app must listen on $PORT."])
    return apply_overrides(root, plan)


def apply_overrides(root: Path, plan: Plan) -> Plan:
    f = Path(root) / "demon.toml"
    if not f.is_file():
        return plan
    try:
        cfg = tomllib.loads(f.read_text())
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as e:
        plan.notes.append(f"demon.toml ignored: {e}")
        return plan
    if cfg.get("kind") in KINDS:
        plan.kind = cfg["kind"]
    for key, attr in (("install", "install"), ("build", "build"), ("start", "start"),
                      ("static", "static_dir"), ("health", "health")):
        if isinstance(cfg.get(key), str):
            setattr(plan, attr, cfg[key])
    plan.notes.append("settings from demon.toml")
    return plan


# ------------------------------------------------------------------ node
def _pm(root: Path) -> str:
    if (root / "pnpm-lock.yaml").exists():
        return "pnpm"
    if (root / "bun.lockb").exists() or (root / "bun.lock").exists():
        return "bun"
    if (root / "yarn.lock").exists():
        return "yarn"
    return "npm"


def _node(root: Path) -> Plan:
    try:
        pkg = json.loads((root / "package.json").read_text())
    except (json.JSONDecodeError, UnicodeDecodeError):
        return Plan(kind=COMMAND, notes=["package.json could not be parsed"])
    scripts: dict = pkg.get("scripts") or {}
    deps = {**(pkg.get("devDependencies") or {}), **(pkg.get("dependencies") or {})}
    pm = _pm(root)
    run = {"npm": "npm run", "pnpm": "pnpm run", "yarn": "yarn run", "bun": "bun run"}[pm]
    install = {
        "npm": "npm ci" if (root / "package-lock.json").exists() else "npm install",
        "pnpm": "pnpm install --frozen-lockfile",
        "yarn": "yarn install",
        "bun": "bun install",
    }[pm]
    plan = Plan(kind=NODE, install=install, build=f"{run} build" if "build" in scripts else "")
    if pm != "npm" and not shutil.which(pm):
        plan.notes.append(f"{pm} is not on PATH; install it or edit the commands")

    start = str(scripts.get("start", ""))
    if "next" in deps:
        plan.framework = "next"
        if _next_static_export(root):
            plan.kind, plan.static_dir = STATIC, "out"
        else:
            plan.start = f"{run} start" if start else "npx next start"
        return plan
    if "nuxt" in deps:
        plan.framework = "nuxt"
        plan.start = "node .output/server/index.mjs"
        return plan
    if "@sveltejs/kit" in deps:
        plan.framework = "sveltekit"
        if "@sveltejs/adapter-node" in deps:
            plan.start = "node build"
        else:
            plan.kind, plan.static_dir = STATIC, "build"
            plan.notes.append("no @sveltejs/adapter-node: serving the build/ folder as a static site")
        return plan
    if "@remix-run/serve" in deps or "@react-router/serve" in deps:
        plan.framework = "remix"
        plan.start = f"{run} start"
        return plan
    if "astro" in deps:
        plan.framework = "astro"
        if "@astrojs/node" in deps:
            plan.start = "node ./dist/server/entry.mjs"
        else:
            plan.kind, plan.static_dir = STATIC, "dist"
        return plan

    frontend = next((f for f in ("vite", "react-scripts", "@angular/core", "parcel", "gatsby")
                     if f in deps), "")
    dev_only_start = not start or re.match(r"^\s*(vite|react-scripts start|ng serve|parcel|gatsby develop)\b",
                                           start)
    if frontend and plan.build and dev_only_start:
        plan.framework = {"react-scripts": "create-react-app", "@angular/core": "angular"}.get(frontend, frontend)
        plan.kind = STATIC
        plan.static_dir = {"react-scripts": "build", "gatsby": "public"}.get(frontend, "dist")
        if frontend == "@angular/core":
            plan.static_dir = ""  # resolved after build (dist/<name>/browser)
        return plan

    for fw in ("express", "fastify", "koa", "hono", "@nestjs/core"):
        if fw in deps:
            plan.framework = fw.removeprefix("@nestjs/").replace("core", "nestjs")
            break
    if start:
        plan.start = f"{run} start"
    elif isinstance(pkg.get("main"), str) and (root / pkg["main"]).exists():
        plan.start = f"node {pkg['main']}"
    else:
        entry = next((f for f in ("server.js", "index.js", "app.js", "main.js", "server.mjs", "index.mjs")
                      if (root / f).exists()), None)
        if entry:
            plan.start = f"node {entry}"
        elif plan.build:
            plan.kind, plan.static_dir = STATIC, ""
            plan.notes.append("no start script: serving the build output as a static site")
        else:
            plan.notes.append("no start script found: enter a start command")
    return plan


def _next_static_export(root: Path) -> bool:
    for name in ("next.config.js", "next.config.mjs", "next.config.ts", "next.config.cjs"):
        f = root / name
        if f.is_file() and re.search(r"output\s*:\s*['\"]export['\"]", f.read_text(errors="ignore")):
            return True
    return False


# ------------------------------------------------------------------ python
_ASSIGN = r"^(\w+)\s*(?::\s*\w+\s*)?=\s*(?:\w+\.)?{cls}\("


def _find_app(root: Path, cls: str) -> tuple[str, str] | None:
    """Find `var = Cls(` in a top-level-ish module; returns (module path, var)."""
    pat = re.compile(_ASSIGN.format(cls=cls), re.M)
    candidates = ["main.py", "app.py", "server.py", "api.py", "wsgi.py", "asgi.py"]
    files = [root / c for c in candidates] + [root / "app" / c for c in ("main.py", "__init__.py")]
    files += [root / "src" / c for c in candidates]
    files += sorted(root.glob("*.py")) + sorted(root.glob("*/main.py")) + sorted(root.glob("src/*/main.py"))
    seen = set()
    for f in files:
        if f in seen or not f.is_file():
            continue
        seen.add(f)
        m = pat.search(f.read_text(errors="ignore"))
        if m:
            rel = f.relative_to(root).with_suffix("")
            parts = list(rel.parts)
            if parts[-1] == "__init__":
                parts.pop()
            app_dir = ""
            if parts[0] == "src" and len(parts) > 1:
                app_dir, parts = "src", parts[1:]
            return ".".join(parts) + (f"|{app_dir}" if app_dir else ""), m.group(1)
    return None


def _python(root: Path) -> Plan:
    reqs = ""
    for f in ("requirements.txt", "pyproject.toml"):
        if (root / f).is_file():
            reqs += (root / f).read_text(errors="ignore").lower()

    def has(pkg: str) -> bool:
        return re.search(rf"(^|[\s\"'\[,]){re.escape(pkg)}([\s\"'<>=~!\[;,]|$)", reqs, re.M) is not None

    uv = shutil.which("uv") is not None
    if (root / "uv.lock").exists() and uv:
        install = "uv sync --frozen --no-dev"
    elif (root / "requirements.txt").exists():
        install = ("uv venv --allow-existing .venv && uv pip install --python .venv/bin/python -r requirements.txt"
                   if uv else "python3 -m venv .venv && .venv/bin/pip install -r requirements.txt")
    elif (root / "pyproject.toml").exists():
        install = "uv sync --no-dev" if uv else "python3 -m venv .venv && .venv/bin/pip install ."
    else:
        install = "uv venv --allow-existing .venv" if uv else "python3 -m venv .venv"
    pip_add = ("uv pip install --python .venv/bin/python" if uv else ".venv/bin/pip install")

    plan = Plan(kind=PYTHON, install=install)
    bind = "127.0.0.1"

    def ensure(pkg: str) -> None:
        if not has(pkg):
            plan.install += f" && {pip_add} {pkg}"

    if has("fastapi") or has("starlette"):
        plan.framework = "fastapi" if has("fastapi") else "starlette"
        found = _find_app(root, "FastAPI" if has("fastapi") else "Starlette")
        target, extra = _target(found, "main:app")
        ensure("uvicorn")
        plan.start = f".venv/bin/uvicorn {target}{extra} --host {bind} --port $PORT --proxy-headers"
    elif has("django") or (root / "manage.py").exists():
        plan.framework = "django"
        wsgi = next(iter(sorted(root.glob("*/wsgi.py"))), None)
        mod = f"{wsgi.parent.name}.wsgi" if wsgi else "config.wsgi"
        ensure("gunicorn")
        plan.build = ".venv/bin/python manage.py collectstatic --noinput || true"
        plan.start = f".venv/bin/gunicorn {mod} --bind {bind}:$PORT --workers 2"
    elif has("flask"):
        plan.framework = "flask"
        found = _find_app(root, "Flask")
        target, extra = _target(found, "app:app")
        ensure("gunicorn")
        plan.start = f".venv/bin/gunicorn {target}{extra.replace('--app-dir', '--chdir')} --bind {bind}:$PORT --workers 2"
    else:
        entry = next((f for f in ("main.py", "app.py", "server.py") if (root / f).exists()), None)
        if entry:
            plan.start = f".venv/bin/python {entry}"
            plan.notes.append(f"runs {entry} directly: it must listen on the $PORT environment variable")
        else:
            plan.notes.append("no entry point found: enter a start command")
    return plan


def _target(found: tuple[str, str] | None, default: str) -> tuple[str, str]:
    if not found:
        return default, ""
    mod, var = found
    if "|" in mod:
        mod, app_dir = mod.split("|")
        return f"{mod}:{var}", f" --app-dir {app_dir}"
    return f"{mod}:{var}", ""


def resolve_static_dir(src: Path, configured: str) -> Path | None:
    """After a build, find the directory to serve."""
    if configured:
        d = (src / configured).resolve()
        return d if d.is_dir() and d.is_relative_to(src.resolve()) else None
    for c in STATIC_CANDIDATES:
        d = src / c
        if (d / "index.html").is_file():
            return d.resolve()
    # angular: dist/<name>/browser
    for d in sorted(src.glob("dist/*/browser")) + sorted(src.glob("dist/*")):
        if (d / "index.html").is_file():
            return d.resolve()
    return None
