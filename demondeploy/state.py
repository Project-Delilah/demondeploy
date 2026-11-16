"""Persistent state in SQLite (WAL). Short-lived connections, safe across processes."""

from __future__ import annotations

import os
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import fields
from pathlib import Path
from typing import Iterator

from . import paths
from .model import ACTIVE, IN_PROGRESS, Deployment, Project

SCHEMA_VERSION = 1
SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    name        TEXT PRIMARY KEY,
    source_type TEXT NOT NULL,
    source      TEXT NOT NULL,
    branch      TEXT NOT NULL DEFAULT '',
    autodeploy  INTEGER NOT NULL DEFAULT 0,
    kind        TEXT NOT NULL,
    install_cmd TEXT NOT NULL DEFAULT '',
    build_cmd   TEXT NOT NULL DEFAULT '',
    start_cmd   TEXT NOT NULL DEFAULT '',
    static_dir  TEXT NOT NULL DEFAULT '',
    health_path TEXT NOT NULL DEFAULT '/',
    active_id   INTEGER,
    stopped     INTEGER NOT NULL DEFAULT 0,
    last_sha    TEXT NOT NULL DEFAULT '',
    created     REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS deployments (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    project    TEXT NOT NULL REFERENCES projects(name) ON DELETE CASCADE,
    status     TEXT NOT NULL,
    step       TEXT NOT NULL DEFAULT '',
    kind       TEXT NOT NULL DEFAULT '',
    start_cmd  TEXT NOT NULL DEFAULT '',
    static_dir TEXT NOT NULL DEFAULT '',
    port       INTEGER,
    unit       TEXT NOT NULL DEFAULT '',
    sha        TEXT NOT NULL DEFAULT '',
    message    TEXT NOT NULL DEFAULT '',
    error      TEXT NOT NULL DEFAULT '',
    created    REAL NOT NULL,
    finished   REAL
);
CREATE INDEX IF NOT EXISTS dep_project ON deployments(project, id);
CREATE TABLE IF NOT EXISTS env (
    project TEXT NOT NULL REFERENCES projects(name) ON DELETE CASCADE,
    key     TEXT NOT NULL,
    value   TEXT NOT NULL,
    PRIMARY KEY (project, key)
);
CREATE TABLE IF NOT EXISTS domains (
    domain  TEXT PRIMARY KEY,
    project TEXT NOT NULL REFERENCES projects(name) ON DELETE CASCADE,
    added   REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""

_P_FIELDS = [f.name for f in fields(Project)]
_D_FIELDS = [f.name for f in fields(Deployment)]
_BOOL_P = {"autodeploy", "stopped"}


def _project(row: sqlite3.Row) -> Project:
    d = {k: row[k] for k in _P_FIELDS}
    for k in _BOOL_P:
        d[k] = bool(d[k])
    return Project(**d)


def _deployment(row: sqlite3.Row) -> Deployment:
    return Deployment(**{k: row[k] for k in _D_FIELDS})


class State:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or paths.db_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        c = sqlite3.connect(self.path, timeout=15, isolation_level=None)
        try:
            c.execute("PRAGMA busy_timeout=15000")
            c.execute("PRAGMA journal_mode=WAL")
            c.executescript(SCHEMA)
            c.execute("INSERT OR IGNORE INTO meta VALUES ('schema', ?)", (str(SCHEMA_VERSION),))
        finally:
            c.close()
        try:
            os.chmod(self.path, 0o600)  # env vars may hold secrets
        except OSError:
            pass

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        c = sqlite3.connect(self.path, timeout=15, isolation_level=None)
        try:
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA foreign_keys=ON")
            c.execute("PRAGMA busy_timeout=15000")
            c.execute("BEGIN IMMEDIATE")
            try:
                yield c
            except BaseException:
                c.execute("ROLLBACK")
                raise
            else:
                c.execute("COMMIT")
        finally:
            c.close()

    # ------------------------------------------------------------ meta
    def get_meta(self, key: str, default: str = "") -> str:
        with self._conn() as c:
            r = c.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return r["value"] if r else default

    def set_meta(self, key: str, value: str) -> None:
        with self._conn() as c:
            c.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (key, value))

    # ------------------------------------------------------------ projects
    def projects(self) -> list[Project]:
        with self._conn() as c:
            return [_project(r) for r in c.execute("SELECT * FROM projects ORDER BY name")]

    def project(self, name: str) -> Project | None:
        with self._conn() as c:
            r = c.execute("SELECT * FROM projects WHERE name=?", (name,)).fetchone()
        return _project(r) if r else None

    def project_by_source(self, source: str) -> Project | None:
        with self._conn() as c:
            r = c.execute("SELECT * FROM projects WHERE source=? ORDER BY created LIMIT 1",
                          (source,)).fetchone()
        return _project(r) if r else None

    def add_project(self, p: Project) -> None:
        cols = ",".join(_P_FIELDS)
        q = ",".join("?" * len(_P_FIELDS))
        with self._conn() as c:
            c.execute(f"INSERT INTO projects ({cols}) VALUES ({q})",
                      [getattr(p, k) for k in _P_FIELDS])

    def update_project(self, name: str, **kw) -> None:
        bad = set(kw) - set(_P_FIELDS) - {"name"}
        if bad:
            raise KeyError(bad)
        sets = ",".join(f"{k}=?" for k in kw)
        with self._conn() as c:
            c.execute(f"UPDATE projects SET {sets} WHERE name=?", [*kw.values(), name])

    def delete_project(self, name: str) -> None:
        with self._conn() as c:
            c.execute("DELETE FROM projects WHERE name=?", (name,))

    # ------------------------------------------------------------ deployments
    def new_deployment(self, project: str, **kw) -> Deployment:
        kw.setdefault("status", "queued")
        kw["created"] = time.time()
        cols = ",".join(["project", *kw])
        with self._conn() as c:
            cur = c.execute(f"INSERT INTO deployments ({cols}) VALUES ({','.join('?' * (len(kw) + 1))})",
                            [project, *kw.values()])
            r = c.execute("SELECT * FROM deployments WHERE id=?", (cur.lastrowid,)).fetchone()
        return _deployment(r)

    def deployment(self, dep_id: int) -> Deployment | None:
        with self._conn() as c:
            r = c.execute("SELECT * FROM deployments WHERE id=?", (dep_id,)).fetchone()
        return _deployment(r) if r else None

    def deployments(self, project: str | None = None, limit: int = 100) -> list[Deployment]:
        with self._conn() as c:
            if project is None:
                rows = c.execute("SELECT * FROM deployments ORDER BY id DESC LIMIT ?", (limit,))
            else:
                rows = c.execute("SELECT * FROM deployments WHERE project=? ORDER BY id DESC LIMIT ?",
                                 (project, limit))
            return [_deployment(r) for r in rows]

    def in_progress(self, project: str | None = None) -> list[Deployment]:
        marks = ",".join("?" * len(IN_PROGRESS))
        with self._conn() as c:
            if project is None:
                rows = c.execute(f"SELECT * FROM deployments WHERE status IN ({marks})", IN_PROGRESS)
            else:
                rows = c.execute(f"SELECT * FROM deployments WHERE project=? AND status IN ({marks})",
                                 (project, *IN_PROGRESS))
            return [_deployment(r) for r in rows]

    def update_deployment(self, dep_id: int, **kw) -> None:
        bad = set(kw) - set(_D_FIELDS)
        if bad:
            raise KeyError(bad)
        sets = ",".join(f"{k}=?" for k in kw)
        with self._conn() as c:
            c.execute(f"UPDATE deployments SET {sets} WHERE id=?", [*kw.values(), dep_id])

    def activate(self, project: str, dep_id: int) -> int | None:
        """Atomically make dep_id the live deployment; returns the previous one."""
        with self._conn() as c:
            prev = c.execute("SELECT active_id FROM projects WHERE name=?", (project,)).fetchone()
            prev_id = prev["active_id"] if prev else None
            if prev_id is not None and prev_id != dep_id:
                c.execute("UPDATE deployments SET status='superseded' WHERE id=?", (prev_id,))
            c.execute("UPDATE deployments SET status=?, step='live', finished=COALESCE(finished, ?) "
                      "WHERE id=?", (ACTIVE, time.time(), dep_id))
            c.execute("UPDATE projects SET active_id=?, stopped=0 WHERE name=?", (dep_id, project))
        return prev_id if prev_id != dep_id else None

    def used_ports(self) -> set[int]:
        with self._conn() as c:
            rows = c.execute("SELECT port FROM deployments WHERE port IS NOT NULL AND status IN "
                             "('active','starting','building','queued')")
            return {r["port"] for r in rows}

    # ------------------------------------------------------------ env
    def env(self, project: str) -> dict[str, str]:
        with self._conn() as c:
            return {r["key"]: r["value"] for r in
                    c.execute("SELECT key, value FROM env WHERE project=? ORDER BY key", (project,))}

    def set_env(self, project: str, key: str, value: str) -> None:
        with self._conn() as c:
            c.execute("INSERT OR REPLACE INTO env VALUES (?, ?, ?)", (project, key, value))

    def unset_env(self, project: str, key: str) -> bool:
        with self._conn() as c:
            return c.execute("DELETE FROM env WHERE project=? AND key=?", (project, key)).rowcount > 0

    # ------------------------------------------------------------ domains
    def domains(self, project: str | None = None) -> list[tuple[str, str]]:
        with self._conn() as c:
            if project is None:
                rows = c.execute("SELECT domain, project FROM domains ORDER BY domain")
            else:
                rows = c.execute("SELECT domain, project FROM domains WHERE project=? ORDER BY domain",
                                 (project,))
            return [(r["domain"], r["project"]) for r in rows]

    def add_domain(self, project: str, domain: str) -> None:
        with self._conn() as c:
            c.execute("INSERT INTO domains VALUES (?, ?, ?)", (domain, project, time.time()))

    def remove_domain(self, domain: str) -> bool:
        with self._conn() as c:
            return c.execute("DELETE FROM domains WHERE domain=?", (domain,)).rowcount > 0
