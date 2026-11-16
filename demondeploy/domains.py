"""Custom domains: validation and DNS checks before Caddy takes over (certs are automatic)."""

from __future__ import annotations

import ipaddress
import re
import socket
import urllib.request
from dataclasses import dataclass, field

from . import proxy
from .proc import DemonError, run
from .state import State

_LABEL = r"(?!-)[a-z0-9-]{1,63}(?<!-)"
DOMAIN_RE = re.compile(rf"^(\*\.)?({_LABEL}\.)+[a-z][a-z0-9-]{{1,62}}$")


@dataclass
class Check:
    domain: str
    ok: list[str] = field(default_factory=list)
    warn: list[str] = field(default_factory=list)
    resolved: list[str] = field(default_factory=list)

    @property
    def good(self) -> bool:
        return not self.warn


def normalize(domain: str) -> str:
    d = domain.strip().lower().removeprefix("https://").removeprefix("http://").split("/")[0]
    d = d.rstrip(".")
    try:
        d = d.encode("idna").decode()
    except UnicodeError:
        raise DemonError(f"'{domain}' is not a valid domain name.") from None
    if not DOMAIN_RE.match(d) or len(d) > 253:
        raise DemonError(f"'{domain}' is not a valid domain name.")
    if d.endswith(proxy.LOCAL_TLD):
        raise DemonError("*.localhost names are automatic; add a real domain instead.")
    return d


def public_ip() -> str | None:
    for url in ("https://api.ipify.org", "https://ifconfig.me/ip", "https://icanhazip.com"):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "curl/8"})
            with urllib.request.urlopen(req, timeout=4) as r:
                ip = r.read().decode().strip()
            ipaddress.ip_address(ip)
            return ip
        except (OSError, ValueError):
            continue
    return None


def local_ips() -> set[str]:
    ips = {"127.0.0.1", "::1"}
    try:
        cp = run(["ip", "-j", "addr", "show"], check=False, timeout=5)
        if cp.returncode == 0:
            import json
            for iface in json.loads(cp.stdout):
                ips |= {a["local"] for a in iface.get("addr_info", []) if a.get("local")}
            return ips
    except (DemonError, ValueError):
        pass
    try:  # fallback: the address used for outbound traffic
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))
            ips.add(s.getsockname()[0])
    except OSError:
        pass
    return ips


def resolve(domain: str) -> list[str]:
    host = domain.removeprefix("*.")
    try:
        return sorted({ai[4][0] for ai in socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)})
    except socket.gaierror:
        return []


def check(st: State, domain: str) -> Check:
    c = Check(domain)
    if domain.startswith("*."):
        c.warn.append("Wildcard certificates need a DNS challenge; automatic HTTPS will not work "
                      "for this name. Plain HTTP routing still applies.")
    c.resolved = resolve(domain)
    mine = local_ips()
    pub = None if proxy.is_internal(domain) else public_ip()
    if pub:
        mine.add(pub)
    if not c.resolved:
        hint = f"an A record pointing to {pub}" if pub else "an A/AAAA record pointing to this machine"
        c.warn.append(f"{domain} does not resolve yet. Create {hint}.")
    elif set(c.resolved) & mine:
        c.ok.append(f"DNS points here ({', '.join(c.resolved)})")
    else:
        c.warn.append(f"{domain} resolves to {', '.join(c.resolved)}, which is not this machine"
                      + (f" (public IP {pub})" if pub else "") + ".")
    if not proxy.tls_enabled(st):
        c.warn.append("Ports 80/443 are not enabled (run `demon setup`), so the domain is served "
                      f"over plain HTTP on port {proxy.http_port(st)} without a certificate.")
    elif proxy.is_internal(domain):
        c.ok.append("Internal name: HTTPS with demondeploy's local certificate authority")
    else:
        c.ok.append("A Let's Encrypt certificate will be requested automatically once DNS points "
                    "here and ports 80/443 are reachable from the internet")
    return c


def add(st: State, project: str, domain: str) -> str:
    d = normalize(domain)
    for existing, owner in st.domains():
        if existing == d:
            raise DemonError(f"{d} is already attached to {owner}.")
    if st.project(project) is None:
        raise DemonError(f"No app named '{project}'.")
    st.add_domain(project, d)
    try:
        proxy.reload(st)
    except DemonError:
        st.remove_domain(d)
        proxy.write_config(st)
        raise
    return d


def remove(st: State, domain: str) -> None:
    d = normalize(domain) if not domain.startswith("*.") else domain
    if not st.remove_domain(d):
        raise DemonError(f"{d} is not attached to any app.")
    proxy.reload(st)
