"""The front proxy: Caddy, configured with JSON generated from the state DB."""

from __future__ import annotations

import hashlib
import io
import json
import os
import platform
import shutil
import tarfile
import urllib.request
from pathlib import Path

from . import paths, sysd
from .model import STATIC, Project
from .proc import DemonError, run
from .state import State

CADDY_VERSION = "2.11.7"
LOCAL_TLD = ".localhost"
INTERNAL_TLDS = (".internal", ".lan", ".local", ".home.arpa", ".test")


# ------------------------------------------------------------------ caddy binary
def find_caddy() -> Path | None:
    own = paths.bin_dir() / "caddy"
    if own.is_file() and os.access(own, os.X_OK):
        return own
    sys_caddy = shutil.which("caddy")
    return Path(sys_caddy) if sys_caddy else None


def caddy_version(caddy: Path) -> str:
    cp = run([caddy, "version"], check=False, timeout=10)
    return cp.stdout.split()[0] if cp.returncode == 0 and cp.stdout else "?"


def download_caddy(progress=lambda msg: None) -> Path:
    arch = {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64",
            "armv7l": "armv7", "armv6l": "armv6"}.get(platform.machine())
    if not arch:
        raise DemonError(f"No Caddy build for CPU '{platform.machine()}'. Install caddy yourself.")
    base = f"https://github.com/caddyserver/caddy/releases/download/v{CADDY_VERSION}/"
    asset = f"caddy_{CADDY_VERSION}_linux_{arch}.tar.gz"
    progress(f"downloading {asset}")

    def fetch(url: str) -> bytes:
        req = urllib.request.Request(url, headers={"User-Agent": "demondeploy"})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.read()
        except OSError as e:
            raise DemonError(f"Download failed: {url}", str(e)) from None

    sums = fetch(base + f"caddy_{CADDY_VERSION}_checksums.txt").decode()
    expected = next((ln.split()[0] for ln in sums.splitlines() if ln.strip().endswith(asset)), None)
    if not expected:
        raise DemonError("Caddy checksum list does not mention " + asset)
    blob = fetch(base + asset)
    algo = {128: "sha512", 64: "sha256"}.get(len(expected))
    if not algo or hashlib.new(algo, blob).hexdigest() != expected.lower():
        raise DemonError("Caddy download failed its checksum check; not installing it.")
    progress("checksum ok, unpacking")
    with tarfile.open(fileobj=io.BytesIO(blob)) as tf:
        member = tf.getmember("caddy")
        data = tf.extractfile(member).read()
    dest = paths.bin_dir() / "caddy"
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".tmp")
    tmp.write_bytes(data)
    tmp.chmod(0o755)
    os.replace(tmp, dest)
    return dest


# ------------------------------------------------------------------ config
def http_port(st: State) -> int:
    return int(st.get_meta("http_port", "8080"))


def tls_enabled(st: State) -> bool:
    return http_port(st) == 80


def app_host(name: str) -> str:
    return f"{name}{LOCAL_TLD}"


def app_url(st: State, name: str) -> str:
    port = http_port(st)
    return f"http://{app_host(name)}" + ("" if port == 80 else f":{port}")


def domain_url(st: State, domain: str) -> str:
    if tls_enabled(st):
        return f"https://{domain}"
    return f"http://{domain}:{http_port(st)}"


def is_internal(domain: str) -> bool:
    return domain.endswith(INTERNAL_TLDS)


def _static_response(code: int, title: str, body: str) -> dict:
    html = (f"<!doctype html><meta charset=utf-8><title>{title}</title>"
            "<body style='font:16px system-ui;background:#1e1f29;color:#f8f8f2;display:grid;"
            "place-items:center;height:100vh;margin:0'><div style='text-align:center'>"
            f"<h1 style='color:#bd93f9'>{title}</h1><p>{body}</p>"
            "<p style='color:#6272a4'>demondeploy</p></div>")
    return {"handler": "static_response", "status_code": code, "body": html,
            "headers": {"Content-Type": ["text/html; charset=utf-8"], "Cache-Control": ["no-store"]}}


def _app_handlers(st: State, p: Project) -> list[dict]:
    dep = st.deployment(p.active_id) if p.active_id else None
    if dep is None:
        return [_static_response(503, "Not deployed yet", f"<b>{p.name}</b> has no live deployment.")]
    if p.stopped:
        return [_static_response(503, "App stopped", f"<b>{p.name}</b> is stopped. "
                                 f"Start it with <code>demon start {p.name}</code>.")]
    if dep.kind == STATIC:
        root = dep.static_dir
        return [{"handler": "subroute", "routes": [
            {"match": [{"file": {"root": root, "try_files": [
                "{http.request.uri.path}", "{http.request.uri.path}/index.html",
                "{http.request.uri.path}.html", "/index.html"]}}],
             "handle": [{"handler": "rewrite", "uri": "{http.matchers.file.relative}"}]},
            {"handle": [{"handler": "encode", "encodings": {"gzip": {}, "zstd": {}},
                         "prefer": ["zstd", "gzip"]},
                        {"handler": "file_server", "root": root}]},
        ]}]
    return [{"handler": "reverse_proxy", "upstreams": [{"dial": f"127.0.0.1:{dep.port}"}],
             "health_checks": {"passive": {"fail_duration": "2s"}},
             "load_balancing": {"retries": 3, "try_duration": "5s", "try_interval": "250ms"}}]


def build_config(st: State) -> dict:
    port = http_port(st)
    tls = tls_enabled(st)
    projects = st.projects()
    domains = st.domains()
    by_project: dict[str, list[str]] = {}
    for d, p in domains:
        by_project.setdefault(p, []).append(d)

    local_routes, public_routes = [], []
    for p in projects:
        handlers = _app_handlers(st, p)
        local_routes.append({"match": [{"host": [app_host(p.name)]}], "handle": handlers,
                             "terminal": True})
        hosts = by_project.get(p.name, [])
        if hosts:
            public_routes.append({"match": [{"host": hosts}], "handle": handlers, "terminal": True})

    fallback = {"handle": [_static_response(
        404, "Nothing here", "No app is deployed at this address. Run <code>demon</code> to see your apps.")]}

    servers: dict = {}
    if tls:
        redirect = []
        if domains:
            redirect = [{"match": [{"host": [d for d, _ in domains]}], "handle": [{
                "handler": "static_response", "status_code": 308,
                "headers": {"Location": ["https://{http.request.host}{http.request.uri}"]}}],
                "terminal": True}]
        servers["http"] = {"listen": [":80"], "routes": [*local_routes, *redirect, fallback],
                           "automatic_https": {"disable": True}}
        servers["https"] = {"listen": [":443"], "routes": [*public_routes, fallback],
                            "automatic_https": {"disable_redirects": True}}
    else:
        servers["http"] = {"listen": [f":{port}"],
                           "routes": [*local_routes, *public_routes, fallback],
                           "automatic_https": {"disable": True}}

    cfg: dict = {
        "admin": {"listen": f"unix/{paths.caddy_admin_sock()}", "config": {"persist": False}},
        "storage": {"module": "file_system", "root": str(paths.caddy_storage())},
        "logging": {"logs": {"default": {"level": "WARN"}}},
        "apps": {"http": {"http_port": 80 if tls else port, "https_port": 443, "servers": servers}},
    }
    internal = [d for d, _ in domains if is_internal(d)]
    if tls and domains:
        policies = []
        if internal:
            policies.append({"subjects": internal, "issuers": [{"module": "internal"}]})
        email = st.get_meta("acme_email")
        acme = {"module": "acme", **({"email": email} if email else {})}
        policies.append({"issuers": [acme, {**acme, "ca": "https://acme.zerossl.com/v2/DV90"}]
                         if email else [acme]})
        cfg["apps"]["tls"] = {"automation": {"policies": policies}}
    return cfg


def write_config(st: State) -> Path:
    path = paths.caddy_config()
    paths.atomic_write(path, json.dumps(build_config(st), indent=2) + "\n")
    return path


def reload(st: State) -> None:
    """Write the config and apply it gracefully, if the proxy is running."""
    path = write_config(st)
    if not sysd.is_active(paths.PROXY_UNIT):
        return
    caddy = find_caddy()
    if caddy is None:
        raise DemonError("Caddy is missing. Run `demon setup`.")
    run([caddy, "reload", "--config", path, "--address", f"unix/{paths.caddy_admin_sock()}", "--force"],
        timeout=60)


def install_unit(caddy: Path) -> None:
    write_config(State())
    sysd.write_unit(paths.PROXY_UNIT, sysd.proxy_unit(caddy, paths.caddy_config()))
    sysd.daemon_reload()


def cert_status(domain: str) -> str:
    """'issued', 'pending' (no cert yet)."""
    root = paths.caddy_storage() / "certificates"
    if root.is_dir():
        for issuer in root.iterdir():
            if (issuer / domain / f"{domain}.crt").is_file():
                return "issued"
    if is_internal(domain) and (paths.caddy_storage() / "pki").is_dir():
        return "internal CA"
    return "pending"
