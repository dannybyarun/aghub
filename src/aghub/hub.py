"""The aghub hub: authoritative leases and live presence.

Git remains the durable log (tasks, contracts, handoffs, merges). The hub owns
the two things git is bad at: high-frequency presence and a single arbiter for
concurrent claims. Agents publish a snapshot after every board transaction, so
the web view never has to touch a repository.
"""

import csv
import hashlib
import hmac
import io
import json
import os
import sqlite3
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import __version__
from .errors import AghubError
from .paths import overlaps
from .util import ago_iso, in_iso, now_iso

MAX_BODY_BYTES = 16 * 1024 * 1024

SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshots (
  project TEXT PRIMARY KEY,
  snapshot TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  seq INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY,
  value INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS agents (
  project TEXT NOT NULL,
  id TEXT NOT NULL,
  last_seen TEXT NOT NULL,
  current_task TEXT,
  branch TEXT,
  machine TEXT,
  PRIMARY KEY (project, id)
);
CREATE TABLE IF NOT EXISTS leases (
  project TEXT NOT NULL,
  task TEXT NOT NULL,
  agent TEXT NOT NULL,
  paths TEXT NOT NULL,
  acquired_at TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  fencing INTEGER NOT NULL,
  PRIMARY KEY (project, task)
);
CREATE TABLE IF NOT EXISTS counters (
  project TEXT NOT NULL,
  name TEXT NOT NULL,
  value INTEGER NOT NULL,
  PRIMARY KEY (project, name)
);
CREATE TABLE IF NOT EXISTS events (
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  project TEXT NOT NULL,
  at TEXT NOT NULL,
  type TEXT NOT NULL,
  payload TEXT NOT NULL,
  actor TEXT
);
CREATE TABLE IF NOT EXISTS quota_windows (
  actor TEXT NOT NULL,
  window_start INTEGER NOT NULL,
  used INTEGER NOT NULL,
  PRIMARY KEY (actor, window_start)
);
CREATE TABLE IF NOT EXISTS board_events (
  seq INTEGER PRIMARY KEY,
  project TEXT NOT NULL,
  at TEXT NOT NULL,
  payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS board_events_project
  ON board_events(project, seq);
"""


class HubStore:
    def __init__(self, path):
        self.path = path
        if path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        with self.lock:
            self.conn.executescript(SCHEMA)
            columns = {
                row["name"] for row in self.conn.execute("PRAGMA table_info(snapshots)")
            }
            if "seq" not in columns:
                self.conn.execute(
                    "ALTER TABLE snapshots ADD COLUMN seq INTEGER NOT NULL DEFAULT 0"
                )
                self.conn.execute("UPDATE snapshots SET seq=rowid WHERE seq=0")
                self.conn.execute(
                    """
                    INSERT INTO meta(key, value)
                    SELECT 'snapshot_seq', COALESCE(MAX(seq), 0) FROM snapshots
                    WHERE true
                    ON CONFLICT(key) DO UPDATE SET value=excluded.value
                    """
                )
            event_columns = {
                row["name"] for row in self.conn.execute("PRAGMA table_info(events)")
            }
            if "actor" not in event_columns:
                self.conn.execute("ALTER TABLE events ADD COLUMN actor TEXT")
            self.conn.execute(
                "CREATE INDEX IF NOT EXISTS events_actor ON events(project, actor, seq)"
            )
            self.conn.commit()

    def close(self):
        with self.lock:
            self.conn.close()

    # -- snapshots -------------------------------------------------------

    def publish(self, project, snapshot, actor=None):
        stamp = now_iso()
        with self.lock:
            seq = self._next_seq()
            self.conn.execute(
                """
                INSERT INTO snapshots(project, snapshot, updated_at, seq) VALUES(?,?,?,?)
                ON CONFLICT(project) DO UPDATE SET
                  snapshot=excluded.snapshot,
                  updated_at=excluded.updated_at,
                  seq=excluded.seq
                """,
                (project, json.dumps(snapshot), stamp, seq),
            )
            self.conn.execute(
                "INSERT INTO board_events(seq, project, at, payload) VALUES(?,?,?,?)",
                (seq, project, stamp, json.dumps(self.board(project))),
            )
            self._event(project, "snapshot.publish", {"seq": seq}, actor=actor)
            oldest = self.conn.execute(
                """
                SELECT seq FROM board_events WHERE project=?
                ORDER BY seq DESC LIMIT 1 OFFSET 100
                """,
                (project,),
            ).fetchone()
            if oldest is not None:
                self.conn.execute(
                    "DELETE FROM board_events WHERE project=? AND seq <= ?",
                    (project, oldest["seq"]),
                )
            self.conn.commit()
        return stamp

    def _next_seq(self):
        row = self.conn.execute(
            "SELECT value FROM meta WHERE key='snapshot_seq'"
        ).fetchone()
        value = (row["value"] if row else 0) + 1
        self.conn.execute(
            """
            INSERT INTO meta(key, value) VALUES('snapshot_seq', ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value
            """,
            (value,),
        )
        return value

    def board(self, project=None):
        with self.lock:
            if project:
                row = self.conn.execute(
                    "SELECT * FROM snapshots WHERE project=?", (project,)
                ).fetchone()
            else:
                row = self.conn.execute(
                    "SELECT * FROM snapshots ORDER BY seq DESC LIMIT 1"
                ).fetchone()
            if row is None:
                return None
            project = row["project"]
            agents = [
                dict(item)
                for item in self.conn.execute(
                    "SELECT id, last_seen, current_task, branch, machine FROM agents WHERE project=? ORDER BY id",
                    (project,),
                )
            ]
            leases = [
                self._lease(item)
                for item in self.conn.execute(
                    "SELECT * FROM leases WHERE project=? ORDER BY task", (project,)
                )
            ]
            return {
                "project": project,
                "seq": row["seq"],
                "snapshot": json.loads(row["snapshot"]),
                "updated_at": row["updated_at"],
                "agents": agents,
                "leases": [lease for lease in leases if lease["active"]],
            }

    def projects(self):
        with self.lock:
            rows = self.conn.execute(
                "SELECT project, updated_at FROM snapshots ORDER BY seq DESC"
            )
            return [{"id": row["project"], "updated_at": row["updated_at"]} for row in rows]

    def overview(self, allowed=None):
        """Aggregate multiple repositories/projects for a hosted hub landing page."""
        allowed = set(allowed) if allowed is not None else None
        with self.lock:
            rows = self.conn.execute(
                "SELECT project, snapshot, updated_at, seq FROM snapshots ORDER BY seq DESC"
            ).fetchall()
            projects = []
            for row in rows:
                if allowed is not None and row["project"] not in allowed:
                    continue
                snapshot = json.loads(row["snapshot"])
                tasks = snapshot.get("tasks") or []
                projects.append(
                    {
                        "id": row["project"],
                        "updated_at": row["updated_at"],
                        "seq": row["seq"],
                        "trunk": (snapshot.get("trunk") or {}).get("sha")
                        if isinstance(snapshot.get("trunk"), dict)
                        else snapshot.get("trunk"),
                        "agents": len(snapshot.get("agents") or []),
                        "tasks": len(tasks),
                        "ready": sum(1 for task in tasks if task.get("status") == "ready"),
                        "claimed": sum(1 for task in tasks if task.get("status") == "claimed"),
                        "done": sum(1 for task in tasks if task.get("status") == "done"),
                        "evals": len(snapshot.get("evals") or []),
                    }
                )
            return {"projects": projects}

    def audit(self, project=None, after=0, limit=100, actor=None):
        """Immutable-ish hub operation log for operators and hosted deployments."""
        limit = max(1, min(int(limit or 100), 10000))
        with self.lock:
            clauses = ["seq>?"]
            params = [int(after or 0)]
            if project:
                clauses.append("project=?")
                params.append(project)
            if actor:
                clauses.append("actor=?")
                params.append(actor)
            params.append(limit)
            rows = self.conn.execute(
                """
                SELECT seq, project, at, type, payload, actor FROM events
                WHERE """
                + " AND ".join(clauses)
                + " ORDER BY seq LIMIT ?",
                params,
            ).fetchall()
            return [
                {
                    "seq": row["seq"],
                    "project": row["project"],
                    "at": row["at"],
                    "actor": row["actor"],
                    "type": row["type"],
                    "payload": json.loads(row["payload"]),
                }
                for row in rows
            ]

    def consume_quota(self, actor, limit, window=60):
        """Count one request for an actor in a fixed window.

        The counter lives in the shared store, so a token cannot multiply its
        quota by connecting through several hubs.
        """
        limit = int(limit or 0)
        now = int(time.time())
        start = now - (now % int(window or 60))
        retry_after = max(1, int(window or 60) - (now % int(window or 60)))
        if limit <= 0:
            return {"allowed": True, "remaining": 0, "retry_after": 0}
        with self.lock:
            self.conn.execute(
                "DELETE FROM quota_windows WHERE window_start < ?", (start,)
            )
            self.conn.execute(
                """
                INSERT INTO quota_windows(actor, window_start, used) VALUES(?,?,1)
                ON CONFLICT(actor, window_start) DO UPDATE SET used=used+1
                """,
                (actor, start),
            )
            row = self.conn.execute(
                "SELECT used FROM quota_windows WHERE actor=? AND window_start=?",
                (actor, start),
            ).fetchone()
            self.conn.commit()
        used = int(row["used"] if row else 1)
        return {
            "allowed": used <= limit,
            "remaining": max(0, limit - used),
            "retry_after": retry_after,
        }

    def history(self, project=None, after=0, limit=100):
        """Bounded board history, oldest first. Used for SSE replay and the API."""
        limit = max(1, min(int(limit or 100), 100))
        with self.lock:
            if project is None:
                row = self.conn.execute(
                    "SELECT project FROM snapshots ORDER BY seq DESC LIMIT 1"
                ).fetchone()
                if row is None:
                    return []
                project = row["project"]
            rows = self.conn.execute(
                """
                SELECT seq, at, payload FROM board_events
                WHERE project=? AND seq>? ORDER BY seq LIMIT ?
                """,
                (project, int(after or 0), limit),
            ).fetchall()
            return [
                {
                    "seq": row["seq"],
                    "at": row["at"],
                    "board": json.loads(row["payload"]),
                }
                for row in rows
            ]

    # -- presence --------------------------------------------------------

    def heartbeat(self, project, agent, task=None, branch=None, machine=None, ttl=None):
        stamp = now_iso()
        with self.lock:
            self._upsert_agent(project, agent, stamp, task, branch, machine)
            expires = None
            if task and ttl:
                lease = self.conn.execute(
                    "SELECT 1 FROM leases WHERE project=? AND task=? AND agent=?",
                    (project, task, agent),
                ).fetchone()
                if lease:
                    expires = in_iso(ttl)
                    self.conn.execute(
                        "UPDATE leases SET expires_at=? WHERE project=? AND task=? AND agent=?",
                        (expires, project, task, agent),
                    )
            self.conn.commit()
        return {"ok": True, "expires_at": expires}

    def _upsert_agent(self, project, agent, stamp, task=None, branch=None, machine=None):
        self.conn.execute(
            """
            INSERT INTO agents(project, id, last_seen, current_task, branch, machine)
            VALUES(?,?,?,?,?,?)
            ON CONFLICT(project, id) DO UPDATE SET
              last_seen=excluded.last_seen,
              current_task=excluded.current_task,
              branch=COALESCE(excluded.branch, agents.branch),
              machine=COALESCE(excluded.machine, agents.machine)
            """,
            (project, agent, stamp, task, branch, machine),
        )

    # -- leases ----------------------------------------------------------

    def claim(self, project, task, agent, paths, ttl=600, steal=False, force=False, actor=None):
        ttl = max(1, min(int(ttl or 600), 86400))
        stamp = now_iso()
        with self.lock:
            rows = self.conn.execute(
                "SELECT * FROM leases WHERE project=?", (project,)
            ).fetchall()
            current = next((row for row in rows if row["task"] == task), None)
            stolen_from = None

            if current is not None and current["agent"] != agent:
                if current["expires_at"] > stamp and not force:
                    return {
                        "status": 409,
                        "error": f"{task} is leased by {current['agent']} until {current['expires_at']}",
                        "holder": dict(current),
                    }
                if current["expires_at"] <= stamp and not (steal or force):
                    return {
                        "status": 409,
                        "error": f"{task} has a stale lease from {current['agent']} — use --steal",
                        "holder": dict(current),
                    }
                stolen_from = current["agent"]

            for row in rows:
                if row["task"] == task or row["agent"] == agent:
                    continue
                if row["expires_at"] <= stamp:
                    continue
                theirs = json.loads(row["paths"] or "[]")
                for mine in paths or []:
                    for other in theirs:
                        if overlaps(mine, other):
                            return {
                                "status": 409,
                                "error": (
                                    f"path conflict: {mine} overlaps {other}, leased by "
                                    f"{row['agent']} on {row['task']}"
                                ),
                                "holder": dict(row),
                            }

            self.conn.execute(
                "DELETE FROM leases WHERE project=? AND expires_at <= ?", (project, stamp)
            )
            fencing = self._next_fencing(project)
            expires = in_iso(ttl)
            self.conn.execute(
                """
                INSERT OR REPLACE INTO leases(project, task, agent, paths, acquired_at, expires_at, fencing)
                VALUES(?,?,?,?,?,?,?)
                """,
                (project, task, agent, json.dumps(paths or []), stamp, expires, fencing),
            )
            self._upsert_agent(project, agent, stamp, task)
            self._event(
                project,
                "lease.claim",
                {"task": task, "agent": agent, "paths": paths or [], "fencing": fencing, "stolen_from": stolen_from},
                actor=actor,
            )
            self.conn.commit()
            return {
                "status": 200,
                "ok": True,
                "fencing": fencing,
                "expires_at": expires,
                "stolen_from": stolen_from,
            }

    def release(self, project, task, agent, force=False, actor=None):
        with self.lock:
            row = self.conn.execute(
                "SELECT * FROM leases WHERE project=? AND task=?", (project, task)
            ).fetchone()
            if row is None:
                return {"status": 200, "ok": True, "released": False}
            if row["agent"] != agent and not force:
                return {
                    "status": 409,
                    "error": f"{task} is leased by {row['agent']}",
                    "holder": dict(row),
                }
            self.conn.execute(
                "DELETE FROM leases WHERE project=? AND task=?", (project, task)
            )
            self.conn.execute(
                "UPDATE agents SET current_task=NULL, branch=NULL WHERE project=? AND id=? AND current_task=?",
                (project, row["agent"], task),
            )
            self._event(project, "lease.release", {"task": task, "agent": agent, "owner": row["agent"]}, actor=actor)
            self.conn.commit()
            return {"status": 200, "ok": True, "released": True}

    def expire(self, project, agent, ttl=600, actor=None):
        stale = ago_iso(int(ttl) + 5)
        with self.lock:
            self.conn.execute(
                "UPDATE agents SET last_seen=? WHERE project=? AND id=?", (stale, project, agent)
            )
            released = self.conn.execute(
                "DELETE FROM leases WHERE project=? AND agent=?", (project, agent)
            ).rowcount
            self._event(project, "lease.expire", {"agent": agent, "released": released}, actor=actor)
            self.conn.commit()
            return {"ok": True, "agent": agent, "released": released, "last_seen": stale}

    def health(self):
        stamp = now_iso()
        with self.lock:
            projects = self.conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]
            leases = self.conn.execute(
                "SELECT COUNT(*) FROM leases WHERE expires_at > ?", (stamp,)
            ).fetchone()[0]
            agents = self.conn.execute("SELECT COUNT(*) FROM agents").fetchone()[0]
        return {
            "ok": True,
            "version": __version__,
            "backend": "sqlite",
            "projects": projects,
            "live_leases": leases,
            "agents": agents,
        }

    # -- internals -------------------------------------------------------

    def _next_fencing(self, project):
        row = self.conn.execute(
            "SELECT value FROM counters WHERE project=? AND name='fencing'", (project,)
        ).fetchone()
        value = (row["value"] if row else 0) + 1
        self.conn.execute(
            "INSERT OR REPLACE INTO counters(project, name, value) VALUES(?, 'fencing', ?)",
            (project, value),
        )
        return value

    def _event(self, project, kind, payload, actor=None):
        self.conn.execute(
            "INSERT INTO events(project, at, type, payload, actor) VALUES(?,?,?,?,?)",
            (project, now_iso(), kind, json.dumps(payload), actor),
        )

    @staticmethod
    def _lease(row):
        return {
            "task": row["task"],
            "agent": row["agent"],
            "paths": json.loads(row["paths"] or "[]"),
            "acquired_at": row["acquired_at"],
            "expires_at": row["expires_at"],
            "fencing": row["fencing"],
            "active": row["expires_at"] > now_iso(),
        }


class HubServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class Handler(BaseHTTPRequestHandler):
    server_version = "aghub-hub/" + __version__
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        if getattr(self.server, "verbose", False):
            super().log_message(fmt, *args)

    def do_GET(self):
        self._route("GET")

    def do_POST(self):
        self._route("POST")

    # -- routing ---------------------------------------------------------

    def _route(self, method):
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)

        if path in ("/", "/index.html"):
            return self._html(PAGE)

        if path == "/api/health":
            try:
                return self._json(200, self.server.store.health())
            except Exception as exc:
                return self._json(
                    503,
                    {"ok": False, "error": f"store unavailable: {exc}"},
                    headers={"Retry-After": "1"},
                )

        if not path.startswith("/api/"):
            return self._json(404, {"error": "not found"})

        if path != "/api/health" and self._rate_limited():
            return self._json(
                429,
                {"error": "rate limit exceeded; retry shortly"},
                headers={"Retry-After": "1"},
            )

        identity = self._identity(query)
        if identity is None:
            return self._json(
                401,
                {"error": "unauthorized — pass ?token= or Authorization: Bearer"},
            )

        quota = identity.get("quota") or {}
        limit = int(quota.get("requests_per_minute") or 0)
        if limit > 0:
            decision = self.server.store.consume_quota(
                identity.get("actor") or "anonymous", limit
            )
            if not decision["allowed"]:
                return self._json(
                    429,
                    {"error": "token quota exceeded; retry shortly"},
                    headers={
                        "Retry-After": str(decision["retry_after"]),
                        "X-Quota-Limit": str(limit),
                        "X-Quota-Remaining": "0",
                    },
                )

        if method == "GET":
            needed = "admin" if path == "/api/audit" else "reader"
        elif method == "POST":
            needed = "admin" if path.startswith("/api/debug/") else "writer"
        else:
            return self._json(405, {"error": "method not allowed"})
        if not self._role_allows(identity, needed):
            return self._json(
                403, {"error": f"{needed} token required"}
            )
        projects = identity.get("projects")
        if projects is not None and not projects:
            return self._json(403, {"error": "token has no project scope"})

        try:
            if method == "GET":
                return self._get(path, query, identity)
            if method == "POST":
                return self._post(path, identity)
            return self._json(405, {"error": "method not allowed"})
        except AghubError as exc:
            return self._json(400, {"error": str(exc)})
        except Exception as exc:  # keep the daemon alive on bad input
            return self._json(500, {"error": f"internal error: {exc}"})

    def _get(self, path, query, identity):
        project = query.get("project", [None])[0]
        allowed = identity.get("projects")
        if project and allowed is not None and project not in allowed:
            return self._json(403, {"error": f"token cannot read project {project}"})
        if project is None and allowed is not None:
            project = allowed[0] if allowed else None
        if path == "/api/projects":
            projects = self.server.store.projects()
            if allowed is not None:
                projects = [item for item in projects if item["id"] in allowed]
            return self._json(200, {"projects": projects})
        if path == "/api/overview":
            return self._json(200, self.server.store.overview(allowed))
        if path == "/api/audit":
            return self._audit(query, identity)
        if path == "/api/audit/export":
            return self._audit_export(query, identity)
        if path == "/api/events":
            return self._events(
                project,
                self.headers.get("Last-Event-ID")
                or query.get("last_event_id", [None])[0],
            )
        if path == "/api/history":
            return self._history(project, query)
        if path == "/api/board":
            data = self.server.store.board(project)
            if data is None:
                return self._json(404, {"error": "no board has been published yet"})
            return self._json(200, data)
        return self._json(404, {"error": "not found"})

    def _events(self, project, last_event_id=None):
        """Server-sent board updates. One thread per browser connection."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-transform")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        self.wfile.write(b"retry: 2000\n\n")
        self.wfile.flush()

        last = object()
        if last_event_id:
            try:
                after = int(last_event_id)
            except (TypeError, ValueError):
                after = 0
            for item in self.server.store.history(project, after, 100):
                self._sse("board", item["board"], event_id=item["seq"])
                last = json.dumps(
                    item["board"], sort_keys=True, separators=(",", ":")
                )

        last_ping = time.monotonic()
        try:
            while True:
                data = self.server.store.board(project)
                signature = (
                    json.dumps(data, sort_keys=True, separators=(",", ":"))
                    if data is not None
                    else None
                )
                if signature != last:
                    last = signature
                    if data is None:
                        self._sse("waiting", {"project": project})
                    else:
                        self._sse("board", data, event_id=data.get("seq"))
                now = time.monotonic()
                if now - last_ping >= 15:
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
                    last_ping = now
                time.sleep(1)
        except (OSError, sqlite3.ProgrammingError):
            pass
        finally:
            self.close_connection = True

    def _sse(self, event, payload, event_id=None):
        body = json.dumps(payload, separators=(",", ":"))
        prefix = f"id: {event_id}\n" if event_id is not None else ""
        self.wfile.write(
            f"{prefix}event: {event}\ndata: {body}\n\n".encode("utf-8")
        )
        self.wfile.flush()

    def _audit(self, query, identity):
        project = query.get("project", [None])[0]
        allowed = identity.get("projects")
        if project and allowed is not None and project not in allowed:
            return self._json(403, {"error": f"token cannot read project {project}"})
        actor = query.get("actor", [None])[0] or None
        try:
            after = int(query.get("after", ["0"])[0] or 0)
            limit = int(query.get("limit", ["100"])[0] or 100)
        except (TypeError, ValueError):
            return self._json(400, {"error": "after and limit must be integers"})
        limit = max(1, min(limit, 500))
        records = self.server.store.audit(project, after, limit, actor=actor)
        if allowed is not None:
            records = [item for item in records if item["project"] in allowed]
        return self._json(200, {"events": records})

    def _audit_export(self, query, identity):
        """Export audit records as JSONL or CSV, scoped to the calling token.

        Admins can export any actor. Everyone else is pinned to their own
        actor label, so a reader token cannot enumerate another tenant's
        operations by guessing its name.
        """
        project = query.get("project", [None])[0]
        allowed = identity.get("projects")
        if project and allowed is not None and project not in allowed:
            return self._json(403, {"error": f"token cannot read project {project}"})
        actor = query.get("actor", [None])[0] or None
        if identity.get("role") != "admin":
            if actor and actor != identity.get("actor"):
                return self._json(403, {"error": "token cannot export another actor"})
            actor = identity.get("actor")
        fmt = (query.get("format", ["jsonl"])[0] or "jsonl").lower()
        if fmt not in ("jsonl", "csv"):
            return self._json(400, {"error": "format must be jsonl or csv"})
        try:
            after = int(query.get("after", ["0"])[0] or 0)
            limit = int(query.get("limit", ["1000"])[0] or 1000)
        except (TypeError, ValueError):
            return self._json(400, {"error": "after and limit must be integers"})
        limit = max(1, min(limit, 10000))
        records = self.server.store.audit(project, after, limit, actor=actor)
        if allowed is not None:
            records = [item for item in records if item["project"] in allowed]
        if fmt == "csv":
            stream = io.StringIO()
            writer = csv.writer(stream)
            writer.writerow(["seq", "at", "project", "actor", "type", "payload"])
            for item in records:
                writer.writerow(
                    [
                        item["seq"],
                        item["at"],
                        item["project"],
                        item["actor"] or "",
                        item["type"],
                        json.dumps(item["payload"], separators=(",", ":")),
                    ]
                )
            body = stream.getvalue()
            content_type = "text/csv; charset=utf-8"
        else:
            body = "".join(
                json.dumps(item, separators=(",", ":")) + "\n" for item in records
            )
            content_type = "application/x-ndjson; charset=utf-8"
        stamp = now_iso().replace("-", "").replace(":", "")
        return self._raw(
            200,
            body,
            content_type,
            {
                "Content-Disposition": (
                    f'attachment; filename="aghub-audit-{stamp}.{fmt}"'
                ),
                "X-Audit-Records": str(len(records)),
            },
        )

    def _history(self, project, query):
        try:
            after = int(query.get("after", ["0"])[0] or 0)
            limit = int(query.get("limit", ["50"])[0] or 50)
        except (TypeError, ValueError):
            return self._json(400, {"error": "after and limit must be integers"})
        return self._json(
            200,
            {"events": self.server.store.history(project, after, limit)},
        )

    def _post(self, path, identity):
        body = self._body(identity)
        store = self.server.store
        project = body.get("project")
        if not project and isinstance(body.get("snapshot"), dict):
            project = (body["snapshot"].get("project") or {}).get("name")
        allowed = identity.get("projects")
        if project and allowed is not None and project not in allowed:
            return self._json(403, {"error": f"token cannot write project {project}"})
        if path == "/api/snapshot":
            snapshot = body.get("snapshot")
            if not isinstance(snapshot, dict):
                raise AghubError("snapshot must be an object")
            project = body.get("project") or (snapshot.get("project") or {}).get("name")
            if not project:
                raise AghubError("project is required")
            stamp = store.publish(project, snapshot, actor=identity.get("actor"))
            return self._json(200, {"ok": True, "project": project, "updated_at": stamp})

        if path == "/api/heartbeat":
            project, agent = self._require(body, "project", "agent")
            return self._json(
                200,
                store.heartbeat(
                    project,
                    agent,
                    body.get("task"),
                    body.get("branch"),
                    body.get("machine"),
                    body.get("ttl"),
                ),
            )

        if path == "/api/claim":
            project, task, agent = self._require(body, "project", "task", "agent")
            result = store.claim(
                project,
                task,
                agent,
                body.get("paths") or [],
                body.get("ttl") or 600,
                bool(body.get("steal")),
                bool(body.get("force")),
                actor=identity.get("actor"),
            )
            return self._json(result.pop("status"), result)

        if path == "/api/release":
            project, task, agent = self._require(body, "project", "task", "agent")
            result = store.release(
                project, task, agent, bool(body.get("force")), actor=identity.get("actor")
            )
            return self._json(result.pop("status"), result)

        if path == "/api/debug/expire":
            project, agent = self._require(body, "project", "agent")
            return self._json(
                200,
                store.expire(
                    project,
                    agent,
                    body.get("ttl") or 600,
                    actor=identity.get("actor"),
                ),
            )

        return self._json(404, {"error": "not found"})

    # -- helpers ---------------------------------------------------------

    def _require(self, body, *fields):
        values = []
        for field in fields:
            value = body.get(field)
            if not value:
                raise AghubError(f"{field} is required")
            values.append(value)
        return values

    def _body(self, identity=None):
        raw_length = self.headers.get("Content-Length") or "0"
        try:
            length = int(raw_length)
        except (TypeError, ValueError):
            raise AghubError("invalid Content-Length")
        if length < 0:
            raise AghubError("invalid Content-Length")
        if not length:
            return {}
        maximum = int(getattr(self.server, "max_body", MAX_BODY_BYTES))
        quota = (identity or {}).get("quota") or {}
        token_max = int(quota.get("max_request_bytes") or 0)
        if token_max > 0:
            maximum = min(maximum, token_max)
        if length > maximum:
            raise AghubError(f"request body exceeds {maximum} bytes")
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except ValueError:
            raise AghubError("request body must be JSON")

    def _identity(self, query):
        tokens = getattr(self.server, "tokens", {}) or {}
        if not tokens:
            return {"role": "admin", "projects": None, "token": None}
        header = self.headers.get("Authorization", "")
        supplied = header[7:] if header.startswith("Bearer ") else query.get("token", [None])[0]
        if not supplied:
            return None
        for token, identity in tokens.items():
            if hmac.compare_digest(str(token), str(supplied)):
                return identity
        return None

    @staticmethod
    def _role_allows(identity, needed):
        levels = {"reader": 0, "writer": 1, "admin": 2}
        return levels.get(identity.get("role"), -1) >= levels.get(needed, 0)

    def _rate_limited(self):
        limit = int(getattr(self.server, "rate_limit", 0) or 0)
        if limit <= 0:
            return False
        key = self.client_address[0] if self.client_address else "unknown"
        now = time.monotonic()
        with self.server.rate_lock:
            bucket = self.server.rate_buckets.setdefault(key, deque())
            while bucket and now - bucket[0] > 60:
                bucket.popleft()
            if len(bucket) >= limit:
                return True
            bucket.append(now)
        return False

    def _json(self, status, payload, headers=None):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _raw(self, status, text, content_type, headers=None):
        body = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _html(self, text):
        body = text.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


ROLES = {"reader", "writer", "admin"}
QUOTA_KEYS = {"requests_per_minute", "max_request_bytes"}


def _token_actor(token, name=None):
    name = str(name).strip() if name else ""
    if name:
        return name
    return "token-" + hashlib.sha256(str(token).encode("utf-8")).hexdigest()[:10]


def _token_quota(value):
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise AghubError("token quota must be an object")
    quota = {}
    for key in ("requests_per_minute", "max_request_bytes"):
        if key in value:
            try:
                number = int(value[key])
            except (TypeError, ValueError):
                raise AghubError(f"token quota '{key}' must be an integer")
            if number < 0:
                raise AghubError(f"token quota '{key}' must be >= 0")
            quota[key] = number
    unknown = sorted(set(value) - QUOTA_KEYS)
    if unknown:
        raise AghubError(f"unknown token quota '{unknown[0]}'")
    return quota


def _token_projects(value):
    if value is None:
        return None
    if not isinstance(value, list):
        raise AghubError("token projects must be a list")
    return [str(item) for item in value]


def _identity(token, role, projects=None, name=None, quota=None, actor=None):
    role = role or "writer"
    if role not in ROLES:
        raise AghubError(f"unknown token role '{role}'")
    return {
        "role": role,
        "projects": _token_projects(projects),
        "actor": actor or _token_actor(token, name),
        "quota": _token_quota(quota),
    }


def load_tokens(path):
    """Load hosted-hub token definitions from JSON.

    Accepted shape::

        {"tokens": {"TOKEN": {"role": "writer", "projects": ["web"],
                              "name": "frontend-ci",
                              "quota": {"requests_per_minute": 120,
                                        "max_request_bytes": 1048576}}}}

    or a plain mapping of token -> role string. The optional `name` is the
    audit actor (defaults to a short token fingerprint), and quota limits are
    enforced per actor in the shared store.
    """
    with open(os.path.expanduser(path), "r", encoding="utf-8") as handle:
        data = json.load(handle)
    raw = data.get("tokens", data) if isinstance(data, dict) else data
    tokens = {}
    if isinstance(raw, list):
        raw = {item["token"]: item for item in raw if item.get("token")}
    if not isinstance(raw, dict):
        raise AghubError("tokens file must contain an object or list")
    for token, value in raw.items():
        if isinstance(value, str):
            tokens[str(token)] = _identity(token, value)
        elif isinstance(value, dict):
            tokens[str(token)] = _identity(
                token,
                value.get("role") or "writer",
                value.get("projects"),
                value.get("name"),
                value.get("quota"),
            )
        else:
            raise AghubError(f"token '{token}' must be a role string or object")
    return tokens


def build_server(
    host="127.0.0.1",
    port=8787,
    db=":memory:",
    token=None,
    verbose=False,
    read_token=None,
    tokens=None,
    rate_limit=600,
    postgres=None,
):
    if postgres:
        from .pgstore import PostgresStore

        store = PostgresStore(postgres)
    else:
        store = HubStore(db)
    server = HubServer((host, int(port)), Handler)
    server.store = store
    identities = {}
    if token:
        identities[str(token)] = _identity(token, "admin", None, "admin")
    if read_token:
        identities[str(read_token)] = _identity(read_token, "reader", None, "reader")
    for key, value in (tokens or {}).items():
        if isinstance(value, str):
            identities[str(key)] = _identity(key, value)
        elif isinstance(value, dict):
            identities[str(key)] = _identity(
                key,
                value.get("role") or "writer",
                value.get("projects"),
                value.get("name"),
                value.get("quota"),
                actor=value.get("actor"),
            )
        else:
            raise AghubError(f"token '{key}' must be a role string or object")
    server.token = token
    server.tokens = identities
    server.verbose = verbose
    server.rate_limit = int(rate_limit or 0)
    server.rate_buckets = {}
    server.rate_lock = threading.Lock()
    return server, store


def serve(
    host="127.0.0.1",
    port=8787,
    db="~/.aghub/hub.db",
    token=None,
    verbose=False,
    read_token=None,
    tokens=None,
    rate_limit=600,
    postgres=None,
):
    db = os.path.expanduser(db)
    if host not in ("127.0.0.1", "localhost", "::1") and not token and not tokens:
        raise AghubError("refusing to listen on a public interface without --token")
    server, store = build_server(
        host,
        port,
        db,
        token,
        verbose,
        read_token=read_token,
        tokens=tokens,
        rate_limit=rate_limit,
        postgres=postgres,
    )
    shown_host = server.server_address[0]
    if shown_host in ("0.0.0.0", "::"):
        shown_host = "127.0.0.1"
    shown_port = server.server_address[1]
    print(f"aghub hub listening on http://{shown_host}:{shown_port}", flush=True)
    print(f"  board  http://{shown_host}:{shown_port}/", flush=True)
    print(f"  db     {postgres or db}", flush=True)
    print(
        f"  tokens {len(server.tokens) or '(none — local only)'}"
        f" · rate {server.rate_limit or 'off'}/min",
        flush=True,
    )
    print("  ctrl-c to stop", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping hub", flush=True)
    finally:
        server.server_close()
        store.close()


PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>aghub</title>
<style>
  :root {
    color-scheme: dark;
    --bg: #0d1117; --panel: #161b22; --panel2: #1c2128; --line: #30363d;
    --text: #e6edf3; --dim: #8b949e; --green: #3fb950; --yellow: #d29922;
    --red: #f85149; --blue: #58a6ff; --purple: #bc8cff;
  }
  * { box-sizing: border-box; }
  body { margin: 0; background: var(--bg); color: var(--text);
    font: 14px/1.45 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }
  header { display: flex; align-items: center; gap: 16px; flex-wrap: wrap;
    padding: 14px 18px; border-bottom: 1px solid var(--line); background: #010409; }
  .brand { font-weight: 700; letter-spacing: .5px; }
  .brand span { color: var(--dim); font-weight: 400; }
  .meta { display: flex; align-items: center; gap: 10px; margin-left: auto;
    color: var(--dim); font-size: 12px; }
  select { background: var(--panel); color: var(--text); border: 1px solid var(--line);
    border-radius: 6px; padding: 4px 8px; font: inherit; }
  .dot { width: 8px; height: 8px; border-radius: 50%; background: var(--dim); display: inline-block; }
  .dot.live { background: var(--green); box-shadow: 0 0 8px var(--green); }
  .dot.offline { background: var(--red); }
  main { display: grid; grid-template-columns: minmax(0, 1fr) 340px; gap: 16px; padding: 16px; }
  @media (max-width: 980px) { main { grid-template-columns: 1fr; } }
  .tasks { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 12px; align-content: start; }
  @media (max-width: 760px) { .tasks { grid-template-columns: 1fr; } }
  h2 { margin: 0 0 8px; font-size: 12px; letter-spacing: .8px; text-transform: uppercase; color: var(--dim); }
  h2 .n { color: var(--text); margin-left: 6px; }
  .col { min-width: 0; }
  .card { background: var(--panel); border: 1px solid var(--line); border-left: 3px solid var(--line);
    border-radius: 8px; padding: 10px 12px; margin-bottom: 8px; cursor: pointer; }
  .card:hover { background: var(--panel2); }
  .card.ready { border-left-color: var(--blue); }
  .card.claimed { border-left-color: var(--yellow); }
  .card.stale { border-left-color: var(--red); }
  .card.blocked { border-left-color: var(--purple); }
  .card h3 { margin: 4px 0 6px; font-size: 13px; font-weight: 600; }
  .row { display: flex; justify-content: space-between; gap: 8px; align-items: center; }
  .id { color: var(--dim); font-size: 11px; }
  .owner { font-size: 11px; color: var(--yellow); }
  .paths { color: var(--dim); font-size: 11px; }
  .paths code { background: var(--panel2); padding: 1px 4px; border-radius: 4px; }
  .tags { margin-top: 6px; display: flex; gap: 4px; flex-wrap: wrap; }
  .tag { font-size: 10px; border: 1px solid var(--line); border-radius: 999px; padding: 1px 7px; color: var(--blue); }
  .why { margin-top: 6px; font-size: 11px; color: var(--purple); }
  aside .panel { background: var(--panel); border: 1px solid var(--line); border-radius: 8px;
    padding: 12px; margin-bottom: 12px; }
  .line { display: flex; justify-content: space-between; gap: 8px; padding: 3px 0; font-size: 12px;
    border-bottom: 1px dotted #21262d; }
  .line:last-child { border-bottom: 0; }
  .badge { font-size: 10px; padding: 0 6px; border-radius: 999px; border: 1px solid var(--line); color: var(--dim); }
  .badge.green { color: var(--green); border-color: #238636; }
  .badge.yellow { color: var(--yellow); border-color: #9e6a03; }
  .badge.red { color: var(--red); border-color: #da3633; }
  .agent { padding: 6px 0; border-bottom: 1px dotted #21262d; }
  .agent:last-child { border-bottom: 0; }
  .sub { color: var(--dim); font-size: 11px; }
  .handoff { padding: 6px 0; border-bottom: 1px dotted #21262d; font-size: 12px; }
  .handoff:last-child { border-bottom: 0; }
  .empty { color: var(--dim); font-size: 12px; padding: 8px 0; }
  footer { color: var(--dim); font-size: 11px; padding: 10px 18px 24px; }
  code { font-family: inherit; }
  .modal { position: fixed; inset: 0; background: rgba(1, 4, 9, .78); display: flex;
    align-items: flex-start; justify-content: center; padding: 6vh 16px; z-index: 20; }
  .modal.hidden { display: none; }
  .modal-card { width: min(760px, 94vw); max-height: 86vh; overflow: auto;
    background: var(--panel); border: 1px solid var(--line); border-radius: 10px;
    padding: 18px 20px; box-shadow: 0 20px 60px rgba(0,0,0,.5); }
  .modal-head { display: flex; align-items: flex-start; gap: 12px; }
  .modal-head h2 { margin: 0; font-size: 16px; letter-spacing: 0; text-transform: none; color: var(--text); }
  .modal-head button { margin-left: auto; background: var(--panel2); color: var(--text);
    border: 1px solid var(--line); border-radius: 6px; padding: 2px 9px; font: inherit; cursor: pointer; }
  .kv { display: grid; grid-template-columns: 110px minmax(0, 1fr); gap: 4px 12px;
    margin-top: 14px; font-size: 12px; }
  .kv dt { color: var(--dim); }
  .kv dd { margin: 0; overflow-wrap: anywhere; }
  .modal h3 { margin: 18px 0 6px; font-size: 12px; letter-spacing: .6px;
    text-transform: uppercase; color: var(--dim); }
  .modal pre { margin: 0; padding: 10px; background: #010409; border: 1px solid var(--line);
    border-radius: 6px; white-space: pre-wrap; overflow-wrap: anywhere; font-size: 11px; max-height: 260px; }
</style>
</head>
<body>
<header>
  <div class="brand">aghub <span>hub</span></div>
  <div class="meta">
    <select id="project"></select>
    <span id="dot" class="dot offline"></span>
    <span id="status">connecting</span>
    <span id="updated"></span>
  </div>
</header>
<main>
  <section class="tasks">
    <div class="col"><h2>Ready <span class="n" id="n-ready">0</span></h2><div id="ready"></div></div>
    <div class="col"><h2>In flight <span class="n" id="n-flight">0</span></h2><div id="flight"></div></div>
    <div class="col"><h2>Blocked <span class="n" id="n-blocked">0</span></h2><div id="blocked"></div></div>
  </section>
  <aside>
    <div class="panel"><h2>Agents</h2><div id="agents"></div></div>
    <div class="panel"><h2>Leases</h2><div id="leases"></div></div>
    <div class="panel"><h2>Evals</h2><div id="evals"></div></div>
    <div class="panel"><h2>Graph</h2><div id="graph"></div></div>
    <div class="panel"><h2>Contracts</h2><div id="contracts"></div></div>
    <div class="panel"><h2>Contract diff</h2><div id="contract-diff"></div></div>
    <div class="panel"><h2>Merge queue</h2><div id="queue"></div></div>
    <div class="panel"><h2>Reviews</h2><div id="approvals"></div></div>
    <div class="panel"><h2>Handoffs</h2><div id="handoffs"></div></div>
    <div class="panel"><h2>Replay</h2>
      <input id="replay" type="range" min="0" max="0" value="0" style="width:100%">
      <input id="replay-filter" placeholder="filter task id or title" style="width:100%;margin:6px 0;background:var(--panel2);color:var(--text);border:1px solid var(--line);border-radius:5px;padding:4px 7px;font:inherit">
      <div id="replay-meta" class="sub">loading history…</div>
      <div id="replay-tasks"></div>
    </div>
    <div class="panel"><h2>Projects</h2><div id="projects"></div></div>
  </aside>
</main>
<div id="modal" class="modal hidden">
  <div class="modal-card">
    <div class="modal-head">
      <h2 id="modal-title"></h2>
      <button id="modal-close" type="button">close</button>
    </div>
    <div id="modal-body"></div>
  </div>
</div>
<footer>git is the durable log · the hub is the live lease authority · live stream (SSE) with polling fallback</footer>
<script>
const params = new URLSearchParams(location.search);
let token = params.get("token") || "";
let project = params.get("project") || "";
let current = null;
let openTaskId = null;

const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (ch) => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
}[ch]));

const secs = (stamp) => {
  if (!stamp) return Infinity;
  const value = Date.parse(stamp);
  return Number.isFinite(value) ? Math.max(0, (Date.now() - value) / 1000) : Infinity;
};
const ago = (stamp) => {
  const s = secs(stamp);
  if (!Number.isFinite(s)) return "never";
  if (s < 60) return Math.floor(s) + "s";
  if (s < 3600) return Math.floor(s / 60) + "m";
  if (s < 86400) return Math.floor(s / 3600) + "h";
  return Math.floor(s / 86400) + "d";
};
const remaining = (stamp) => {
  if (!stamp) return "—";
  const value = Date.parse(stamp);
  if (!Number.isFinite(value)) return "—";
  const s = Math.max(0, Math.round((value - Date.now()) / 1000));
  if (s < 60) return s + "s left";
  if (s < 3600) return Math.floor(s / 60) + "m left";
  return Math.floor(s / 3600) + "h left";
};
const ttlOf = (snap) => Number((snap && snap.project && snap.project.ttl_seconds) || 600);

function contractFor(snap, ref) {
  const [name, version] = String(ref).split("@");
  return (snap.contracts || []).find((c) => c.name === name && String(c.version) === version);
}
function blockers(task, snap) {
  const out = [];
  for (const ref of task.contracts || []) {
    const meta = contractFor(snap, ref);
    if (!meta) out.push(ref + " missing");
    else if (meta.status !== "frozen") out.push(ref + " " + meta.status);
  }
  for (const dep of task.depends_on || []) {
    const other = (snap.tasks || []).find((t) => t.id === dep);
    if (!other || other.status !== "done") out.push("waiting on " + dep);
  }
  return out;
}
function statusOf(task, snap, agents) {
  if (task.status === "done") return "done";
  if (task.status === "claimed" && task.owner) {
    const agent = agents.find((a) => a.id === task.owner);
    const age = agent ? secs(agent.last_seen) : Infinity;
    return age <= ttlOf(snap) ? "claimed" : "stale";
  }
  return blockers(task, snap).length ? "blocked" : "ready";
}

function reviewOf(taskId, snap) {
  const records = (snap.approvals || []).filter((item) => item.task === taskId);
  records.sort((a, b) =>
    String(a.at || "").localeCompare(String(b.at || ""))
    || String(a.id || "").localeCompare(String(b.id || ""))
  );
  return records.length ? records[records.length - 1] : null;
}

function isApproved(taskId, snap) {
  const record = reviewOf(taskId, snap);
  return !!record && (record.decision || "approved") === "approved";
}

function approvalLabel(taskId, snap) {
  const record = reviewOf(taskId, snap);
  if (record && record.decision === "changes_requested") return "changes requested";
  return isApproved(taskId, snap) ? "approved" : "needs approval";
}

function card(task, snap, agents) {
  const status = statusOf(task, snap, agents);
  const agent = task.owner ? agents.find((a) => a.id === task.owner) : null;
  const owner = task.owner
    ? '<span class="owner">' + esc(task.owner) + " · " + ago(agent && agent.last_seen) + "</span>"
    : "";
  const paths = (task.paths || []).map((p) => "<code>" + esc(p) + "</code>").join(" ");
  const source = task.source && task.source.task ? task.source.task : "";
  const fixTag = source ? '<span class="tag">fix ' + esc(source) + "</span>" : "";
  const evalTags = (task.evals || []).map((e) => '<span class="tag">eval ' + esc(e) + "</span>").join("");
  const depTags = (task.depends_on || []).map((d) => '<span class="tag">dep ' + esc(d) + "</span>").join("");
  const approvalTag = task.requires_approval
    ? '<span class="tag">' + esc(approvalLabel(task.id, snap)) + "</span>" : "";
  const sandboxTag = task.require_sandbox ? '<span class="tag">sandbox required</span>' : "";
  const tags = (task.contracts || []).map((c) => '<span class="tag">' + esc(c) + "</span>").join("")
    + evalTags + depTags + fixTag + approvalTag + sandboxTag;
  const why = status === "blocked"
    ? '<div class="why">' + esc(blockers(task, snap).join("; ")) + "</div>" : "";
  const trace = task.trace
    ? '<div class="why">trace ' + esc(source || task.trace.kind || "") + ": "
      + esc(task.trace.reason || task.trace.kind || "") + "</div>"
    : "";
  return '<article class="card ' + status + '" data-task="' + esc(task.id) + '">'
    + '<div class="row"><span class="id">' + esc(task.id) + "</span>" + owner + "</div>"
    + "<h3>" + esc(task.title) + "</h3>"
    + '<div class="paths">' + paths + "</div>"
    + (tags ? '<div class="tags">' + tags + "</div>" : "")
    + why + trace + "</article>";
}

function evalTone(status) {
  if (status === "ok") return "green";
  if (status === "STALE" || status === "UNSIGNED") return "yellow";
  return "red";
}

function graphRows(tasks) {
  const edges = [];
  for (const task of tasks) {
    for (const dep of task.depends_on || []) {
      edges.push({ from: dep, to: task.id, kind: "depends" });
    }
    const source = task.source && task.source.task;
    if (source) edges.push({ from: source, to: task.id, kind: "fix" });
  }
  if (!edges.length) return '<div class="empty">no dependencies or fix links</div>';
  return edges.map((edge) => '<div class="line"><span>'
    + esc(edge.from) + " → " + esc(edge.to) + "</span>"
    + '<span class="badge ' + (edge.kind === "fix" ? "yellow" : "") + '">'
    + esc(edge.kind) + "</span></div>").join("");
}

function contractDiffRows(contracts) {
  const latest = contracts.slice().reverse().find((item) => item.diff);
  if (!latest) return '<div class="empty">no frozen version pair to diff</div>';
  const diff = latest.diff;
  const rows = [];
  const add = (label, value, tone) => rows.push(
    '<div class="line"><span>' + esc(label) + " " + esc(value) + "</span>"
    + '<span class="badge ' + (tone || "") + '">' + esc(label) + "</span></div>"
  );
  for (const route of diff.routes_added || []) add("+ route", route, "green");
  for (const route of diff.routes_removed || []) add("- route", route, "red");
  for (const op of diff.operations_added || []) add("+ op", op.method + " " + op.path, "green");
  for (const op of diff.operations_removed || []) add("- op", op.method + " " + op.path, "red");
  for (const op of diff.operations_changed || []) {
    add("~ op", op.method + " " + op.path + " (" + (op.fields || []).join(", ") + ")", "yellow");
  }
  for (const name of diff.schemas_added || []) add("+ schema", name, "green");
  for (const name of diff.schemas_removed || []) add("- schema", name, "red");
  for (const [name, detail] of Object.entries(diff.schemas_changed || {})) {
    const parts = [];
    const fields = [].concat(
      (detail.fields_added || []).map((f) => "+" + f),
      (detail.fields_removed || []).map((f) => "-" + f),
      (detail.fields || []).map((f) => "~" + f)
    ).join(", ");
    const props = [].concat(
      (detail.properties_added || []).map((f) => "+" + f),
      (detail.properties_removed || []).map((f) => "-" + f),
      (detail.properties_changed || []).map((f) => "~" + f)
    ).join(", ");
    if (fields) parts.push("fields " + fields);
    if (props) parts.push("props " + props);
    add("~ schema", name + (parts.length ? " (" + parts.join("; ") + ")" : ""), "yellow");
  }
  const header = '<div class="sub">' + esc(latest.name) + "@" + esc(latest.diff_from)
    + " → " + esc(latest.version) + "</div>";
  return header + (rows.length ? rows.join("") : '<div class="empty">no structural changes</div>');
}

function agentRows(agents, snap) {
  if (!agents.length) return '<div class="empty">no agents seen</div>';
  const evals = (snap.evals || []).slice().reverse();
  return agents.slice().sort((a, b) => a.id.localeCompare(b.id)).map((agent) => {
    const stale = secs(agent.last_seen) > ttlOf(snap);
    const ev = evals.find((item) => item.agent === agent.id);
    const evalBadge = ev
      ? ' <span class="badge ' + evalTone(ev.status) + '">'
        + esc(ev.suite) + " " + esc(ev.status) + "</span>"
      : "";
    const profile = [agent.model, (agent.skills || []).join(", ")].filter(Boolean).join(" · ");
    const keyLine = agent.key_id
      ? '<div class="sub">key ' + esc(agent.key_alg || "ed25519") + " " + esc(agent.key_id) + "</div>"
      : "";
    return '<div class="agent"><div><strong>' + esc(agent.id) + "</strong>"
      + (stale ? ' <span class="badge red">stale</span>' : "")
      + '</div><div class="sub">' + esc(agent.current_task || "—") + " · " + ago(agent.last_seen)
      + '</div><div class="sub">' + esc(profile) + evalBadge
      + '</div><div class="sub">' + esc(agent.machine || "") + "</div>"
      + keyLine + "</div>";
  }).join("");
}

function render(data) {
  current = data;
  const snap = data.snapshot || {};
  const agents = data.agents || [];
  if (!project && data.project) project = data.project;

  const groups = { ready: [], claimed: [], stale: [], blocked: [], done: [] };
  for (const task of snap.tasks || []) groups[statusOf(task, snap, agents)].push(task);

  const fill = (id, items) => {
    document.getElementById(id).innerHTML = items.length
      ? items.map((task) => card(task, snap, agents)).join("")
      : '<div class="empty">nothing here</div>';
  };
  document.getElementById("n-ready").textContent = groups.ready.length;
  document.getElementById("n-flight").textContent = groups.claimed.length + groups.stale.length;
  document.getElementById("n-blocked").textContent = groups.blocked.length;
  fill("ready", groups.ready);
  fill("flight", groups.claimed.concat(groups.stale));
  fill("blocked", groups.blocked);

  document.getElementById("agents").innerHTML = agentRows(agents, snap);
  document.getElementById("graph").innerHTML = graphRows(snap.tasks || []);

  const leases = data.leases || [];
  document.getElementById("leases").innerHTML = leases.length
    ? leases.map((lease) => '<div class="line"><span>' + esc(lease.task) + " "
        + '<span class="sub">' + esc(lease.agent) + "</span></span>"
        + '<span class="sub">f' + esc(lease.fencing) + " · " + remaining(lease.expires_at) + "</span></div>"
        + '<div class="sub">' + (lease.paths || []).map((p) => "<code>" + esc(p) + "</code>").join(" ") + "</div>").join("")
    : '<div class="empty">no live leases</div>';

  const evals = (snap.evals || []).slice(-5).reverse();
  document.getElementById("evals").innerHTML = evals.length
    ? evals.map((record) => '<div class="line"><span>' + esc(record.suite) + " "
        + '<span class="sub">' + esc(record.agent) + "</span></span>"
        + '<span class="badge ' + evalTone(record.status) + '">'
        + esc(record.status || (record.passed ? "ok" : "FAIL")) + "</span></div>"
        + '<div class="sub">' + esc(record.command || "") + " · " + esc(record.machine || "")
        + " · " + ago(record.at) + "</div>").join("")
    : '<div class="empty">no evals yet</div>';

  const contracts = snap.contracts || [];
  document.getElementById("contracts").innerHTML = contracts.length
    ? contracts.map((c) => '<div class="line"><span>' + esc(c.name) + "@" + esc(c.version) + "</span>"
        + '<span class="badge ' + (c.status === "frozen" ? "green" : "yellow") + '">' + esc(c.status) + "</span></div>").join("")
    : '<div class="empty">no contracts</div>';
  document.getElementById("contract-diff").innerHTML = contractDiffRows(contracts);

  const queue = (snap.queue || []).slice(-5).reverse();
  document.getElementById("queue").innerHTML = queue.length
    ? queue.map((entry) => '<div class="line"><span>' + esc(entry.task) + " "
        + '<span class="sub">' + esc(entry.agent) + "</span></span>"
        + '<span class="badge ' + (entry.status === "merged" ? "green" : entry.status === "rejected" ? "red" : "yellow") + '">'
        + esc(entry.status) + "</span></div>"
        + (entry.reason ? '<div class="sub">' + esc(entry.reason) + "</div>" : "")).join("")
    : '<div class="empty">queue empty</div>';

  const approvals = (snap.approvals || []).slice(-5).reverse();
  document.getElementById("approvals").innerHTML = approvals.length
    ? approvals.map((record) => {
        const decision = record.decision || "approved";
        const label = decision === "changes_requested"
          ? "changes requested" : (record.attestation ? "approved" : "unsigned");
        const tone = decision === "changes_requested" ? "red" : (record.attestation ? "green" : "yellow");
        return '<div class="line"><span>' + esc(record.task) + " "
          + '<span class="sub">' + esc(record.agent) + "</span></span>"
          + '<span class="badge ' + tone + '">' + esc(label) + "</span></div>"
          + (record.note ? '<div class="sub">' + esc(record.note) + "</div>" : "");
      }).join("")
    : '<div class="empty">no reviews yet</div>';

  const handoffs = (snap.handoffs || []).slice(-8).reverse();
  document.getElementById("handoffs").innerHTML = handoffs.length
    ? handoffs.map((h) => '<div class="handoff"><div class="sub"><strong>' + esc(h.agent) + "</strong> · "
        + esc(h.task) + " · " + ago(h.at) + "</div>"
        + (h.next ? "<div>→ " + esc(h.next) + "</div>" : "")
        + (h.note ? '<div class="sub">' + esc(h.note) + "</div>" : "")
        + (h.diff ? '<div class="sub">' + esc(h.diff) + "</div>" : "")
        + "</div>").join("")
    : '<div class="empty">no handoffs yet</div>';

  const done = groups.done.length;
  document.getElementById("updated").textContent = "snapshot " + ago(snap.generated_at)
    + (done ? " · " + done + " done" : "");

  if (openTaskId) showTask(openTaskId);
}

function closeTask() {
  openTaskId = null;
  document.getElementById("modal").classList.add("hidden");
}

function showTask(taskId) {
  if (!current) return;
  const snap = current.snapshot || {};
  const agents = current.agents || [];
  const task = (snap.tasks || []).find((item) => item.id === taskId);
  if (!task) {
    closeTask();
    return;
  }
  openTaskId = taskId;
  const status = statusOf(task, snap, agents);
  const badge = (text, tone) => '<span class="badge ' + (tone || "") + '">' + esc(text) + "</span>";
  const list = (items) => (items && items.length)
    ? items.map((item) => "<code>" + esc(item) + "</code>").join(" ")
    : "—";
  const rows = [];
  const kv = (label, value) => rows.push(
    "<dt>" + esc(label) + "</dt><dd>" + value + "</dd>"
  );
  kv("status", badge(status, status === "done" ? "green" : status === "stale" ? "red" : status === "blocked" ? "yellow" : ""));
  kv("owner", task.owner ? esc(task.owner) : "—");
  kv("paths", list(task.paths));
  kv("contracts", list(task.contracts));
  kv("depends", list(task.depends_on));
  kv("evals", list(task.evals));
  kv("branch", task.branch ? "<code>" + esc(task.branch) + "</code>" : "—");
  kv("commit", task.commit ? "<code>" + esc(task.commit) + "</code>" : "—");
  kv("created", esc(task.created_at || "—") + " by " + esc(task.created_by || "—"));
  kv("updated", esc(task.updated_at || "—"));
  if (task.require_sandbox) kv("sandbox", badge("bubblewrap required", "yellow"));
  if (task.requires_approval) {
    const label = approvalLabel(task.id, snap);
    const tone = label === "approved" ? "green" : (label === "changes requested" ? "red" : "yellow");
    kv("approval", badge(label, tone));
  }
  if (task.acceptance) kv("acceptance", esc(task.acceptance));

  let body = '<dl class="kv">' + rows.join("") + "</dl>";

  if (task.source && task.source.task) {
    body += "<h3>Fix source</h3><dl class=\"kv\">"
      + "<dt>task</dt><dd><code>" + esc(task.source.task) + "</code></dd>"
      + "<dt>agent</dt><dd>" + esc(task.source.agent || "—") + "</dd>"
      + "<dt>queue</dt><dd>" + esc(task.source.queue || "—") + "</dd>"
      + "<dt>attempts</dt><dd>" + esc(task.attempts || 0) + "</dd></dl>";
  }

  if (task.trace) {
    const trace = task.trace;
    body += "<h3>Failure trace</h3><dl class=\"kv\">"
      + "<dt>kind</dt><dd>" + esc(trace.kind || "—") + "</dd>"
      + "<dt>reason</dt><dd>" + esc(trace.reason || "—") + "</dd>"
      + "<dt>command</dt><dd><code>" + esc(trace.command || "—") + "</code></dd>"
      + "<dt>exit</dt><dd>" + esc(trace.exit_code ?? "—") + "</dd>"
      + "<dt>changed</dt><dd>" + list(trace.changed) + "</dd></dl>";
    for (const stream of ["stdout", "stderr"]) {
      if (trace[stream]) {
        body += "<h3>" + esc(stream) + "</h3><pre>" + esc(trace[stream]) + "</pre>";
      }
    }
  }

  const approvals = (snap.approvals || []).filter((item) => item.task === task.id);
  if (approvals.length) {
    body += "<h3>Reviews</h3>";
    for (const record of approvals) {
      const decision = record.decision || "approved";
      const label = decision === "changes_requested"
        ? "changes requested" : (record.attestation ? "approved" : "unsigned");
      const tone = decision === "changes_requested" ? "red" : (record.attestation ? "green" : "yellow");
      body += '<div class="line"><span>' + esc(record.agent) + " "
        + '<span class="sub">' + esc(record.note || record.commit || "") + "</span></span>"
        + badge(label, tone)
        + "</div>";
    }
  }

  const suites = task.evals || [];
  const evals = (snap.evals || []).filter((item) =>
    suites.includes(item.suite) && (!task.owner || item.agent === task.owner)
  ).slice(-4).reverse();
  if (evals.length) {
    body += "<h3>Eval history</h3>";
    for (const record of evals) {
      const sandbox = record.sandbox && record.sandbox.backend
        ? " · " + esc(record.sandbox.backend) : "";
      body += '<div class="line"><span>' + esc(record.suite) + " "
        + '<span class="sub">' + esc(record.agent) + " · " + esc(record.command || "")
        + " · " + ago(record.at) + sandbox + "</span></span>"
        + badge(record.status || (record.passed ? "ok" : "FAIL"), evalTone(record.status)) + "</div>";
    }
  }

  document.getElementById("modal-title").textContent = task.id + " · " + task.title;
  document.getElementById("modal-body").innerHTML = body;
  document.getElementById("modal").classList.remove("hidden");
}

document.addEventListener("click", (event) => {
  const cardElement = event.target.closest("[data-task]");
  if (cardElement) showTask(cardElement.dataset.task);
  const projectRow = event.target.closest("[data-project]");
  if (projectRow) {
    const next = new URLSearchParams({ project: projectRow.dataset.project });
    if (token) next.set("token", token);
    location.search = next.toString();
  }
});
document.getElementById("modal-close").addEventListener("click", closeTask);
document.getElementById("modal").addEventListener("click", (event) => {
  if (event.target.id === "modal") closeTask();
});
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape") closeTask();
});

function setLive(live, why) {
  const dot = document.getElementById("dot");
  dot.className = "dot " + (live ? "live" : "offline");
  document.getElementById("status").textContent = why || (live ? "live" : "offline");
}

async function tick() {
  try {
    const base = "/api/board" + (project ? "?project=" + encodeURIComponent(project) : "");
    const url = base + (token ? (base.includes("?") ? "&" : "?") + "token=" + encodeURIComponent(token) : "");
    const response = await fetch(url);

    if (response.status === 404) {
      setLive(false, "no board yet");
      document.getElementById("ready").innerHTML = '<div class="empty">waiting for the first snapshot</div>';
      return;
    }
    if (!response.ok) throw new Error("HTTP " + response.status);
    render(await response.json());
    setLive(true, streamOpen ? "" : "polling");
  } catch (error) {
    setLive(false, "hub unreachable");
  }
}

let source = null;
let pollTimer = null;
let streamOpen = false;

function streamUrl() {
  const query = new URLSearchParams();
  if (project) query.set("project", project);
  if (token) query.set("token", token);
  const suffix = query.toString();
  return "/api/events" + (suffix ? "?" + suffix : "");
}

function startPolling() {
  if (pollTimer) return;
  tick();
  pollTimer = setInterval(tick, 3000);
}

function stopPolling() {
  if (!pollTimer) return;
  clearInterval(pollTimer);
  pollTimer = null;
}

function startStream() {
  if (!("EventSource" in window)) {
    startPolling();
    return;
  }
  if (source) source.close();
  source = new EventSource(streamUrl());

  source.addEventListener("board", (event) => {
    try {
      render(JSON.parse(event.data));
    } catch (error) {
      return;
    }
    setLive(true);
  });
  source.addEventListener("waiting", () => {
    setLive(true, "waiting for board");
    document.getElementById("ready").innerHTML = '<div class="empty">waiting for the first snapshot</div>';
  });
  source.onopen = () => {
    streamOpen = true;
    setLive(true);
    stopPolling();
  };
  source.onerror = () => {
    streamOpen = false;
    startPolling();
  };
}

let historyEvents = [];

function renderReplay() {
  const event = historyEvents[Number(document.getElementById("replay").value || 0)];
  const meta = document.getElementById("replay-meta");
  const list = document.getElementById("replay-tasks");
  if (!event) {
    meta.textContent = "no replay history yet";
    list.innerHTML = "";
    return;
  }
  const board = event.board || {};
  const snap = board.snapshot || {};
  const needle = (document.getElementById("replay-filter").value || "").toLowerCase();
  const tasks = (snap.tasks || []).filter((task) =>
    !needle || String(task.id).toLowerCase().includes(needle)
      || String(task.title).toLowerCase().includes(needle));
  const leases = board.leases || [];
  meta.innerHTML = "seq " + esc(event.seq) + " · " + esc(ago(event.at))
    + " · " + (snap.tasks || []).length + " tasks · "
    + leases.length + " leases · " + (snap.evals || []).length + " evals";
  list.innerHTML = tasks.slice(-12).map((task) =>
    '<div class="line"><span>' + esc(task.id) + " " + esc(task.title) + "</span>"
    + '<span class="badge">' + esc(task.status) + "</span></div>").join("")
    || '<div class="empty">no matching tasks</div>';
}

async function loadHistory() {
  try {
    const query = new URLSearchParams({ limit: "100" });
    if (project) query.set("project", project);
    if (token) query.set("token", token);
    const response = await fetch("/api/history?" + query.toString());
    if (!response.ok) return;
    const data = await response.json();
    historyEvents = data.events || [];
    const slider = document.getElementById("replay");
    slider.max = String(Math.max(0, historyEvents.length - 1));
    slider.value = slider.max;
    renderReplay();
  } catch (error) {
    document.getElementById("replay-meta").textContent = "replay unavailable";
  }
}

async function loadOverview() {
  try {
    const url = "/api/overview" + (token ? "?token=" + encodeURIComponent(token) : "");
    const response = await fetch(url);
    if (!response.ok) return;
    const data = await response.json();
    const projects = data.projects || [];
    document.getElementById("projects").innerHTML = projects.length
      ? projects.map((item) =>
        '<div class="line" data-project="' + esc(item.id) + '" style="cursor:pointer"><span>'
        + esc(item.id) + '</span><span class="sub">' + item.ready + " ready · "
        + item.claimed + " flight · " + item.done + " done</span></div>").join("")
      : '<div class="empty">no projects published</div>';
  } catch (error) { /* optional aggregation */ }
}

document.getElementById("replay").addEventListener("input", renderReplay);
document.getElementById("replay-filter").addEventListener("input", renderReplay);

async function loadProjects() {
  try {
    const url = "/api/projects" + (token ? "?token=" + encodeURIComponent(token) : "");
    const response = await fetch(url);
    if (!response.ok) return;
    const data = await response.json();
    const select = document.getElementById("project");
    select.innerHTML = (data.projects || []).map((p) =>
      '<option value="' + esc(p.id) + '"' + (p.id === project ? " selected" : "") + ">" + esc(p.id) + "</option>").join("");
    if (!project && data.projects && data.projects.length) {
      project = data.projects[0].id;
      select.value = project;
    }
    select.onchange = () => {
      const next = new URLSearchParams({ project: select.value });
      if (token) next.set("token", token);
      location.search = next.toString();
    };
  } catch (error) { /* the tick indicator already reports offline */ }
}

loadProjects().then(() => {
  tick();
  startStream();
  loadHistory();
  loadOverview();
});
</script>
</body>
</html>
"""
