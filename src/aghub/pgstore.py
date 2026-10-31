"""Optional Postgres backend for hosted aghub hubs.

The default hub is a single SQLite file and needs no dependencies. Hosted
deployments can pass `--postgres postgresql://...`; the optional `psycopg`
package (psycopg 3) is imported only in that case. This keeps the core install
dependency-free while providing a real shared database for multi-process hubs.
"""

import json
import threading
import time
from contextlib import contextmanager

from .errors import AghubError
from .paths import overlaps
from .ulid import new_ulid
from .util import ago_iso, in_iso, now_iso

MAX_PACKET_BYTES = 4 * 1024 * 1024
MAX_REVIEW_NOTE = 4000
REVIEW_DECISIONS = ("approved", "changes_requested")
REVIEW_STATUSES = ("pending", "applying", "applied", "rejected", "cancelled")


def _packet_head(packet):
    for key in ("approval", "changes"):
        value = packet.get(key)
        if isinstance(value, dict) and value.get("head"):
            return str(value["head"])
    return packet.get("head") or None

SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshots (
  project TEXT PRIMARY KEY,
  snapshot TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  seq BIGINT NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY,
  value BIGINT NOT NULL
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
  fencing BIGINT NOT NULL,
  PRIMARY KEY (project, task)
);
CREATE TABLE IF NOT EXISTS counters (
  project TEXT NOT NULL,
  name TEXT NOT NULL,
  value BIGINT NOT NULL,
  PRIMARY KEY (project, name)
);
CREATE TABLE IF NOT EXISTS events (
  seq BIGSERIAL PRIMARY KEY,
  project TEXT NOT NULL,
  at TEXT NOT NULL,
  type TEXT NOT NULL,
  payload TEXT NOT NULL,
  actor TEXT
);
ALTER TABLE events ADD COLUMN IF NOT EXISTS actor TEXT;
CREATE TABLE IF NOT EXISTS quota_windows (
  actor TEXT NOT NULL,
  window_start BIGINT NOT NULL,
  used BIGINT NOT NULL,
  PRIMARY KEY (actor, window_start)
);
CREATE TABLE IF NOT EXISTS board_events (
  seq BIGINT PRIMARY KEY,
  project TEXT NOT NULL,
  at TEXT NOT NULL,
  payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS board_events_project
  ON board_events(project, seq);
CREATE INDEX IF NOT EXISTS events_project
  ON events(project, seq);
CREATE INDEX IF NOT EXISTS events_actor
  ON events(project, actor, seq);
CREATE TABLE IF NOT EXISTS review_packets (
  project TEXT NOT NULL,
  task TEXT NOT NULL,
  head TEXT,
  packet TEXT NOT NULL,
  published_at TEXT NOT NULL,
  actor TEXT,
  PRIMARY KEY (project, task)
);
CREATE TABLE IF NOT EXISTS review_requests (
  id TEXT PRIMARY KEY,
  project TEXT NOT NULL,
  task TEXT NOT NULL,
  decision TEXT NOT NULL,
  note TEXT NOT NULL DEFAULT '',
  head TEXT,
  actor TEXT,
  reviewer TEXT,
  status TEXT NOT NULL DEFAULT 'pending',
  worker TEXT,
  record_id TEXT,
  error TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS review_requests_board
  ON review_requests(project, status, created_at);
"""


def _driver():
    try:
        import psycopg
        from psycopg.rows import dict_row
    except ImportError as exc:  # pragma: no cover - depends on optional env
        raise AghubError(
            "the Postgres hub backend needs the optional psycopg package: "
            "pip install 'psycopg[binary]'"
        ) from exc
    return psycopg, dict_row


class PostgresStore:
    def __init__(self, dsn):
        psycopg, dict_row = _driver()
        self.dsn = dsn
        self.lock = threading.RLock()
        self.conn = psycopg.connect(dsn, row_factory=dict_row)
        with self.lock, self.conn.cursor() as cur:
            # CREATE TABLE IF NOT EXISTS is not safe under concurrent first
            # startup: Postgres can still raise a duplicate pg_type key. Hold
            # one database-wide schema lock so only one process creates it.
            cur.execute("SELECT pg_advisory_xact_lock(hashtext('aghub:schema'))")
            cur.execute(SCHEMA)
        self.conn.commit()

    def close(self):
        with self.lock:
            self.conn.close()

    def _fetchone(self, sql, params=()):
        with self.conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchone()

    def _fetchall(self, sql, params=()):
        with self.conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall()

    def _write(self, sql, params=()):
        with self.conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.rowcount

    def _lock_project(self, project):
        """Serialize lease mutations for one project across hub processes.

        The in-process RLock only protects one server. Hosted hubs can share a
        Postgres database across processes, so the read/decide/write sequence
        needs a database-level lock that is released with the transaction.
        """
        self._fetchone(
            "SELECT pg_advisory_xact_lock(hashtext(%s))",
            (str(project),),
        )

    @contextmanager
    def _project_transaction(self, project):
        """Run a lease mutation in one project-scoped transaction.

        Early conflict returns must commit (or roll back) before leaving the
        block; otherwise the transaction-scoped advisory lock would stay held
        by an idle connection and stall every other hub process.
        """
        with self.lock:
            try:
                self._lock_project(project)
                yield
            except BaseException:
                self.conn.rollback()
                raise
            else:
                self.conn.commit()

    def publish(self, project, snapshot, actor=None):
        stamp = now_iso()
        with self.lock:
            seq = self._next_seq()
            self._write(
                """
                INSERT INTO snapshots(project, snapshot, updated_at, seq) VALUES(%s,%s,%s,%s)
                ON CONFLICT(project) DO UPDATE SET
                  snapshot=excluded.snapshot,
                  updated_at=excluded.updated_at,
                  seq=excluded.seq
                """,
                (project, json.dumps(snapshot), stamp, seq),
            )
            self._write(
                "INSERT INTO board_events(seq, project, at, payload) VALUES(%s,%s,%s,%s)",
                (seq, project, stamp, json.dumps(self.board(project))),
            )
            self._event(project, "snapshot.publish", {"seq": seq}, actor=actor)
            self._write(
                """
                DELETE FROM board_events WHERE project=%s AND seq <= (
                  SELECT seq FROM board_events WHERE project=%s
                  ORDER BY seq DESC OFFSET 100 LIMIT 1
                )
                """,
                (project, project),
            )
            self.conn.commit()
        return stamp

    def _next_seq(self):
        self._write(
            "INSERT INTO meta(key, value) VALUES('snapshot_seq', 0) "
            "ON CONFLICT(key) DO NOTHING"
        )
        row = self._fetchone(
            "UPDATE meta SET value=value+1 WHERE key='snapshot_seq' RETURNING value"
        )
        return int(row["value"])

    def board(self, project=None):
        with self.lock:
            if project:
                row = self._fetchone(
                    "SELECT * FROM snapshots WHERE project=%s", (project,)
                )
            else:
                row = self._fetchone(
                    "SELECT * FROM snapshots ORDER BY seq DESC LIMIT 1"
                )
            if row is None:
                return None
            project = row["project"]
            agents = [
                dict(item)
                for item in self._fetchall(
                    "SELECT id, last_seen, current_task, branch, machine "
                    "FROM agents WHERE project=%s ORDER BY id",
                    (project,),
                )
            ]
            leases = [
                self._lease(item)
                for item in self._fetchall(
                    "SELECT * FROM leases WHERE project=%s ORDER BY task", (project,)
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
            rows = self._fetchall(
                "SELECT project, updated_at FROM snapshots ORDER BY seq DESC"
            )
            return [{"id": row["project"], "updated_at": row["updated_at"]} for row in rows]

    def overview(self, allowed=None):
        allowed = set(allowed) if allowed is not None else None
        with self.lock:
            rows = self._fetchall(
                "SELECT project, snapshot, updated_at, seq FROM snapshots ORDER BY seq DESC"
            )
            projects = []
            for row in rows:
                if allowed is not None and row["project"] not in allowed:
                    continue
                snapshot = json.loads(row["snapshot"])
                tasks = snapshot.get("tasks") or []
                trunk = snapshot.get("trunk")
                projects.append(
                    {
                        "id": row["project"],
                        "updated_at": row["updated_at"],
                        "seq": row["seq"],
                        "trunk": trunk.get("sha") if isinstance(trunk, dict) else trunk,
                        "agents": len(snapshot.get("agents") or []),
                        "tasks": len(tasks),
                        "ready": sum(1 for task in tasks if task.get("status") == "ready"),
                        "claimed": sum(1 for task in tasks if task.get("status") == "claimed"),
                        "done": sum(1 for task in tasks if task.get("status") == "done"),
                        "evals": len(snapshot.get("evals") or []),
                    }
                )
            return {"projects": projects}

    def history(self, project=None, after=0, limit=100):
        limit = max(1, min(int(limit or 100), 100))
        with self.lock:
            if project is None:
                row = self._fetchone(
                    "SELECT project FROM snapshots ORDER BY seq DESC LIMIT 1"
                )
                if row is None:
                    return []
                project = row["project"]
            rows = self._fetchall(
                "SELECT seq, at, payload FROM board_events "
                "WHERE project=%s AND seq>%s ORDER BY seq LIMIT %s",
                (project, int(after or 0), limit),
            )
            return [
                {"seq": row["seq"], "at": row["at"], "board": json.loads(row["payload"])}
                for row in rows
            ]

    # -- review workspace ------------------------------------------------

    def publish_review_packet(self, project, task, packet, actor=None):
        if not isinstance(packet, dict):
            raise AghubError("review packet must be an object")
        blob = json.dumps(packet)
        if len(blob.encode("utf-8")) > MAX_PACKET_BYTES:
            raise AghubError(
                f"review packet exceeds {MAX_PACKET_BYTES // (1024 * 1024)} MiB"
            )
        head = _packet_head(packet)
        stamp = now_iso()
        with self.lock:
            self._write(
                """
                INSERT INTO review_packets(project, task, head, packet, published_at, actor)
                VALUES(%s,%s,%s,%s,%s,%s)
                ON CONFLICT(project, task) DO UPDATE SET
                  head=excluded.head,
                  packet=excluded.packet,
                  published_at=excluded.published_at,
                  actor=excluded.actor
                """,
                (project, task, head, blob, stamp, actor),
            )
            self._event(
                project, "review.packet", {"task": task, "head": head}, actor=actor
            )
            self.conn.commit()
        return {
            "ok": True,
            "project": project,
            "task": task,
            "head": head,
            "published_at": stamp,
        }

    def review_packet(self, project, task):
        with self.lock:
            row = self._fetchone(
                "SELECT * FROM review_packets WHERE project=%s AND task=%s",
                (project, task),
            )
        if row is None:
            return None
        return {
            "project": row["project"],
            "task": row["task"],
            "head": row["head"],
            "published_at": row["published_at"],
            "actor": row["actor"],
            "packet": json.loads(row["packet"]),
        }

    def review_packet_heads(self, project):
        with self.lock:
            rows = self._fetchall(
                """
                SELECT task, head, published_at, actor FROM review_packets
                WHERE project=%s ORDER BY task
                """,
                (project,),
            )
            return [dict(row) for row in rows]

    def review_requests(self, project=None, status=None, limit=100):
        limit = max(1, min(int(limit or 100), 500))
        if status == "all":
            status = None
        with self.lock:
            clauses = []
            params = []
            if project:
                clauses.append("project=%s")
                params.append(project)
            if status:
                if status not in REVIEW_STATUSES:
                    raise AghubError(f"unknown review status '{status}'")
                clauses.append("status=%s")
                params.append(status)
            params.append(limit)
            where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
            rows = self._fetchall(
                "SELECT * FROM review_requests"
                + where
                + " ORDER BY created_at, id LIMIT %s",
                tuple(params),
            )
            return [self._review_request(row) for row in rows]

    def review_request(self, request_id):
        with self.lock:
            row = self._fetchone(
                "SELECT * FROM review_requests WHERE id=%s", (request_id,)
            )
            return self._review_request(row) if row else None

    def create_review_request(
        self, project, task, decision, note="", head=None, reviewer=None, actor=None
    ):
        if decision not in REVIEW_DECISIONS:
            raise AghubError("decision must be 'approved' or 'changes_requested'")
        note = str(note or "").strip()
        if len(note) > MAX_REVIEW_NOTE:
            raise AghubError(f"review note exceeds {MAX_REVIEW_NOTE} characters")
        if head is not None:
            head = str(head).strip()
            if head and not all(ch in "0123456789abcdef" for ch in head.lower()):
                raise AghubError("head must be a commit id")
        with self.lock:
            snapshot = self.board(project)
            if snapshot is None:
                raise AghubError(f"no board published for {project}")
            tasks = snapshot["snapshot"].get("tasks") or []
            task_row = next((item for item in tasks if item.get("id") == task), None)
            if task_row is None:
                raise AghubError(f"unknown task {task}")
            if not task_row.get("requires_approval"):
                raise AghubError(f"{task} does not require approval")
            owner = task_row.get("owner")
            if reviewer and owner and str(owner) == str(reviewer):
                raise AghubError(
                    f"{reviewer} owns {task}; review with a separate identity"
                )
            stamp = now_iso()
            request_id = "RR-" + new_ulid()
            if actor is None:
                superseded = self._write(
                    """
                    UPDATE review_requests SET status='cancelled', updated_at=%s,
                      error='superseded by a newer review request'
                    WHERE project=%s AND task=%s AND actor IS NULL AND status='pending'
                    """,
                    (stamp, project, task),
                )
            else:
                superseded = self._write(
                    """
                    UPDATE review_requests SET status='cancelled', updated_at=%s,
                      error='superseded by a newer review request'
                    WHERE project=%s AND task=%s AND actor=%s AND status='pending'
                    """,
                    (stamp, project, task, actor),
                )
            self._write(
                """
                INSERT INTO review_requests(
                  id, project, task, decision, note, head, actor, reviewer,
                  status, created_at, updated_at
                ) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,'pending',%s,%s)
                """,
                (
                    request_id,
                    project,
                    task,
                    decision,
                    note,
                    head or None,
                    actor,
                    reviewer,
                    stamp,
                    stamp,
                ),
            )
            if superseded:
                self._event(
                    project,
                    "review.supersede",
                    {"task": task, "count": superseded},
                    actor=actor,
                )
            self._event(
                project,
                "review.request",
                {
                    "id": request_id,
                    "task": task,
                    "decision": decision,
                    "head": head or None,
                },
                actor=actor,
            )
            self.conn.commit()
            return self.review_request(request_id)

    def claim_review_request(self, request_id, worker=None):
        stamp = now_iso()
        with self.lock:
            row = self._fetchone(
                "SELECT * FROM review_requests WHERE id=%s", (request_id,)
            )
            if row is None:
                raise AghubError(f"unknown review request {request_id}")
            current = self._review_request(row)
            if current["status"] == "applying":
                if current.get("updated_at", "") >= ago_iso(300):
                    return {"ok": True, "claimed": False, "request": current}
            elif current["status"] != "pending":
                return {"ok": True, "claimed": False, "request": current}
            changed = self._write(
                """
                UPDATE review_requests SET status='applying', worker=%s, updated_at=%s
                WHERE id=%s AND status=%s
                """,
                (worker, stamp, request_id, current["status"]),
            )
            if changed:
                self._event(
                    current["project"],
                    "review.claim",
                    {"id": request_id, "task": current["task"]},
                    actor=worker,
                )
                self.conn.commit()
            return {
                "ok": True,
                "claimed": bool(changed),
                "request": self.review_request(request_id),
            }

    def resolve_review_request(
        self, request_id, status, record_id=None, error=None, worker=None
    ):
        if status not in ("applied", "rejected"):
            raise AghubError("resolution must be 'applied' or 'rejected'")
        with self.lock:
            row = self._fetchone(
                "SELECT * FROM review_requests WHERE id=%s", (request_id,)
            )
            if row is None:
                raise AghubError(f"unknown review request {request_id}")
            current = self._review_request(row)
            if current["status"] in ("applied", "rejected"):
                return current
            if current["status"] == "cancelled":
                raise AghubError(f"review request {request_id} was cancelled")
            stamp = now_iso()
            self._write(
                """
                UPDATE review_requests SET status=%s, record_id=%s, error=%s,
                  worker=COALESCE(%s, worker), updated_at=%s
                WHERE id=%s
                """,
                (
                    status,
                    record_id,
                    (str(error) if error else None),
                    worker,
                    stamp,
                    request_id,
                ),
            )
            self._event(
                current["project"],
                "review." + status,
                {
                    "id": request_id,
                    "task": current["task"],
                    "record_id": record_id,
                    "error": str(error) if error else None,
                },
                actor=worker,
            )
            self.conn.commit()
            return self.review_request(request_id)

    def cancel_review_request(self, request_id, actor=None, admin=False):
        with self.lock:
            row = self._fetchone(
                "SELECT * FROM review_requests WHERE id=%s", (request_id,)
            )
            if row is None:
                raise AghubError(f"unknown review request {request_id}")
            current = self._review_request(row)
            if not admin and current.get("actor") != actor:
                raise AghubError("only the requester can cancel this review")
            if current["status"] != "pending":
                raise AghubError(f"review request is already {current['status']}")
            stamp = now_iso()
            self._write(
                "UPDATE review_requests SET status='cancelled', updated_at=%s, error=%s WHERE id=%s",
                (stamp, "cancelled by the requester", request_id),
            )
            self._event(
                current["project"],
                "review.cancelled",
                {"id": request_id, "task": current["task"]},
                actor=actor,
            )
            self.conn.commit()
            return self.review_request(request_id)

    @staticmethod
    def _review_request(row):
        return {
            "id": row["id"],
            "project": row["project"],
            "task": row["task"],
            "decision": row["decision"],
            "note": row["note"] or "",
            "head": row["head"],
            "actor": row["actor"],
            "reviewer": row["reviewer"],
            "status": row["status"],
            "worker": row["worker"],
            "record_id": row["record_id"],
            "error": row["error"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def heartbeat(self, project, agent, task=None, branch=None, machine=None, ttl=None):
        stamp = now_iso()
        with self._project_transaction(project):
            self._upsert_agent(project, agent, stamp, task, branch, machine)
            expires = None
            if task and ttl:
                lease = self._fetchone(
                    "SELECT 1 AS present FROM leases WHERE project=%s AND task=%s AND agent=%s",
                    (project, task, agent),
                )
                if lease:
                    expires = in_iso(ttl)
                    self._write(
                        "UPDATE leases SET expires_at=%s WHERE project=%s AND task=%s AND agent=%s",
                        (expires, project, task, agent),
                    )
        return {"ok": True, "expires_at": expires}

    def _upsert_agent(self, project, agent, stamp, task=None, branch=None, machine=None):
        self._write(
            """
            INSERT INTO agents(project, id, last_seen, current_task, branch, machine)
            VALUES(%s,%s,%s,%s,%s,%s)
            ON CONFLICT(project, id) DO UPDATE SET
              last_seen=excluded.last_seen,
              current_task=excluded.current_task,
              branch=COALESCE(excluded.branch, agents.branch),
              machine=COALESCE(excluded.machine, agents.machine)
            """,
            (project, agent, stamp, task, branch, machine),
        )

    def claim(self, project, task, agent, paths, ttl=600, steal=False, force=False, actor=None):
        ttl = max(1, min(int(ttl or 600), 86400))
        stamp = now_iso()
        with self._project_transaction(project):
            rows = self._fetchall("SELECT * FROM leases WHERE project=%s", (project,))
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
            self._write(
                "DELETE FROM leases WHERE project=%s AND expires_at <= %s",
                (project, stamp),
            )
            fencing = self._next_fencing(project)
            expires = in_iso(ttl)
            self._write(
                """
                INSERT INTO leases(project, task, agent, paths, acquired_at, expires_at, fencing)
                VALUES(%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT(project, task) DO UPDATE SET
                  agent=excluded.agent, paths=excluded.paths,
                  acquired_at=excluded.acquired_at, expires_at=excluded.expires_at,
                  fencing=excluded.fencing
                """,
                (project, task, agent, json.dumps(paths or []), stamp, expires, fencing),
            )
            self._upsert_agent(project, agent, stamp, task)
            self._event(
                project,
                "lease.claim",
                {
                    "task": task,
                    "agent": agent,
                    "paths": paths or [],
                    "fencing": fencing,
                    "stolen_from": stolen_from,
                },
                actor=actor,
            )
            return {
                "status": 200,
                "ok": True,
                "fencing": fencing,
                "expires_at": expires,
                "stolen_from": stolen_from,
            }

    def release(self, project, task, agent, force=False, actor=None):
        with self._project_transaction(project):
            row = self._fetchone(
                "SELECT * FROM leases WHERE project=%s AND task=%s", (project, task)
            )
            if row is None:
                return {"status": 200, "ok": True, "released": False}
            if row["agent"] != agent and not force:
                return {
                    "status": 409,
                    "error": f"{task} is leased by {row['agent']}",
                    "holder": dict(row),
                }
            self._write(
                "DELETE FROM leases WHERE project=%s AND task=%s", (project, task)
            )
            self._write(
                "UPDATE agents SET current_task=NULL, branch=NULL "
                "WHERE project=%s AND id=%s AND current_task=%s",
                (project, row["agent"], task),
            )
            self._event(
                project,
                "lease.release",
                {"task": task, "agent": agent, "owner": row["agent"]},
                actor=actor,
            )
            return {"status": 200, "ok": True, "released": True}

    def expire(self, project, agent, ttl=600, actor=None):
        stale = ago_iso(int(ttl) + 5)
        with self._project_transaction(project):
            self._write(
                "UPDATE agents SET last_seen=%s WHERE project=%s AND id=%s",
                (stale, project, agent),
            )
            released = self._write(
                "DELETE FROM leases WHERE project=%s AND agent=%s", (project, agent)
            )
            self._event(
                project,
                "lease.expire",
                {"agent": agent, "released": released},
                actor=actor,
            )
            return {
                "ok": True,
                "agent": agent,
                "released": released,
                "last_seen": stale,
            }

    def health(self):
        stamp = now_iso()
        with self.lock:
            projects = int(
                (self._fetchone("SELECT COUNT(*) AS n FROM snapshots") or {"n": 0})["n"]
            )
            leases = int(
                (
                    self._fetchone(
                        "SELECT COUNT(*) AS n FROM leases WHERE expires_at > %s",
                        (stamp,),
                    )
                    or {"n": 0}
                )["n"]
            )
            agents = int(
                (self._fetchone("SELECT COUNT(*) AS n FROM agents") or {"n": 0})["n"]
            )
        return {
            "ok": True,
            "version": __import__("aghub").__version__,
            "projects": projects,
            "live_leases": leases,
            "agents": agents,
            "backend": "postgres",
        }

    def _next_fencing(self, project):
        row = self._fetchone(
            "SELECT value FROM counters WHERE project=%s AND name='fencing'",
            (project,),
        )
        value = (int(row["value"]) if row else 0) + 1
        self._write(
            """
            INSERT INTO counters(project, name, value) VALUES(%s, 'fencing', %s)
            ON CONFLICT(project, name) DO UPDATE SET value=excluded.value
            """,
            (project, value),
        )
        return value

    def _event(self, project, kind, payload, actor=None):
        self._write(
            "INSERT INTO events(project, at, type, payload, actor) VALUES(%s,%s,%s,%s,%s)",
            (project, now_iso(), kind, json.dumps(payload), actor),
        )

    def audit(self, project=None, after=0, limit=100, actor=None):
        limit = max(1, min(int(limit or 100), 10000))
        with self.lock:
            clauses = ["seq>%s"]
            params = [int(after or 0)]
            if project:
                clauses.append("project=%s")
                params.append(project)
            if actor:
                clauses.append("actor=%s")
                params.append(actor)
            params.append(limit)
            rows = self._fetchall(
                "SELECT seq, project, at, type, payload, actor FROM events WHERE "
                + " AND ".join(clauses)
                + " ORDER BY seq LIMIT %s",
                params,
            )
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

        Quota rows live in Postgres, so several hubs sharing this database
        enforce one shared budget instead of one budget per process.
        """
        limit = int(limit or 0)
        window = int(window or 60)
        now = int(time.time())
        start = now - (now % window)
        retry_after = max(1, window - (now % window))
        if limit <= 0:
            return {"allowed": True, "remaining": 0, "retry_after": 0}
        with self.lock:
            self._write(
                "DELETE FROM quota_windows WHERE window_start < %s", (start,)
            )
            row = self._fetchone(
                """
                INSERT INTO quota_windows(actor, window_start, used) VALUES(%s,%s,1)
                ON CONFLICT(actor, window_start)
                DO UPDATE SET used=quota_windows.used+1
                RETURNING used
                """,
                (actor, start),
            )
            self.conn.commit()
        used = int(row["used"] if row else 1)
        return {
            "allowed": used <= limit,
            "remaining": max(0, limit - used),
            "retry_after": retry_after,
        }

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
