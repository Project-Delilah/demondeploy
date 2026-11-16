import json
from pathlib import Path

from demondeploy.detect import detect, resolve_static_dir


def make(root: Path, files: dict) -> Path:
    for name, content in files.items():
        f = root / name
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(content if isinstance(content, str) else json.dumps(content))
    return root


def test_next(tmp_path):
    make(tmp_path, {"package.json": {"scripts": {"build": "next build", "start": "next start"},
                                     "dependencies": {"next": "15"}},
                    "package-lock.json": "{}"})
    p = detect(tmp_path)
    assert (p.kind, p.framework) == ("node", "next")
    assert p.install == "npm ci" and p.build == "npm run build" and p.start == "npm run start"


def test_next_static_export(tmp_path):
    make(tmp_path, {"package.json": {"scripts": {"build": "next build"}, "dependencies": {"next": "15"}},
                    "next.config.mjs": "export default { output: 'export' }"})
    p = detect(tmp_path)
    assert p.kind == "static" and p.static_dir == "out"


def test_vite_spa_is_static(tmp_path):
    make(tmp_path, {"package.json": {"scripts": {"dev": "vite", "build": "vite build"},
                                     "devDependencies": {"vite": "6"}},
                    "pnpm-lock.yaml": ""})
    p = detect(tmp_path)
    assert p.kind == "static" and p.static_dir == "dist"
    assert p.install.startswith("pnpm install")


def test_express(tmp_path):
    make(tmp_path, {"package.json": {"main": "server.js", "dependencies": {"express": "5"}},
                    "server.js": ""})
    p = detect(tmp_path)
    assert p.kind == "node" and p.start == "node server.js" and p.framework == "express"


def test_fastapi(tmp_path):
    make(tmp_path, {"requirements.txt": "fastapi\n", "main.py": "from fastapi import FastAPI\napi = FastAPI()\n"})
    p = detect(tmp_path)
    assert p.framework == "fastapi"
    assert "uvicorn main:api" in p.start and "$PORT" in p.start
    assert "uvicorn" in p.install


def test_flask_src_layout(tmp_path):
    make(tmp_path, {"pyproject.toml": '[project]\ndependencies = ["flask"]\n',
                    "src/app.py": "from flask import Flask\napp = Flask(__name__)\n"})
    p = detect(tmp_path)
    assert p.framework == "flask"
    assert "gunicorn app:app --chdir src" in p.start


def test_django(tmp_path):
    make(tmp_path, {"requirements.txt": "Django>=5\n", "manage.py": "", "mysite/wsgi.py": ""})
    p = detect(tmp_path)
    assert p.framework == "django" and "mysite.wsgi" in p.start


def test_plain_html(tmp_path):
    make(tmp_path, {"index.html": "<h1>hi</h1>"})
    p = detect(tmp_path)
    assert p.kind == "static" and p.static_dir == "."


def test_unknown_and_override(tmp_path):
    make(tmp_path, {"main.go": "package main"})
    assert detect(tmp_path).kind == "command"
    make(tmp_path, {"demon.toml": 'kind = "command"\nbuild = "go build -o app"\nstart = "./app"\nhealth = "/hz"\n'})
    p = detect(tmp_path)
    assert (p.build, p.start, p.health) == ("go build -o app", "./app", "/hz")


def test_resolve_static_dir(tmp_path):
    make(tmp_path, {"dist/index.html": "x"})
    assert resolve_static_dir(tmp_path, "") == (tmp_path / "dist").resolve()
    assert resolve_static_dir(tmp_path, "../..") is None
    assert resolve_static_dir(tmp_path, "nope") is None
