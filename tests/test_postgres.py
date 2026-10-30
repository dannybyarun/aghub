"""Optional integration tests for the Postgres hub backend.

These tests only run when ``AGHUB_TEST_POSTGRES_DSN`` points at a disposable
database. They are ignored by the default suite because the core install has
no database dependency and CI should not require a Postgres service.

Example::

    AGHUB_TEST_POSTGRES_DSN='postgresql://postgres@127.0.0.1:55432/aghub_test' \
      /tmp/opencode/aghub-pg-venv/bin/python -m unittest tests.test_postgres -v
"""

import os
import sys
import threading
import unittest
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from aghub import hubclient  # noqa: E402
from aghub.hub import build_server  # noqa: E402
from aghub.pgstore import PostgresStore  # noqa: E402
from aghub.util import ago_iso  # noqa: E402

DSN = os.environ.get("AGHUB_TEST_POSTGRES_DSN")

try:  # pragma: no cover - depends on the optional environment
    import psycopg  # noqa: F401
    from psycopg import sql
    from psycopg.conninfo import make_conninfo
except ImportError:  # pragma: no cover
    psycopg = None
    sql = None
    make_conninfo = None
    _HAVE_DRIVER = False
else:
    _HAVE_DRIVER = True

POSTGRES_READY = bool(DSN) and _HAVE_DRIVER
SKIP_REASON = (
    "set AGHUB_TEST_POSTGRES_DSN to run Postgres integration tests"
    if not DSN
    else "optional psycopg package is not installed"
)

TABLES = ("leases", "agents", "snapshots", "counters", "events", "board_events")


def snapshot(name, tasks=None):
    return {
        "project": {"name": name, "ttl_seconds": 60},
        "tasks": tasks or [],
        "agents": [],
        "evals": [],
    }


def project_name():
    return f"aghub-pg-test-{uuid.uuid4().hex[:12]}"


def clear(store, project):
    for table in TABLES:
        store.conn.execute(f"DELETE FROM {table} WHERE project=%s", (project,))
    store.conn.commit()


@unittest.skipUnless(POSTGRES_READY, SKIP_REASON)
class PostgresStoreTests(unittest.TestCase):
    def setUp(self):
        self.project = project_name()
        self.store = PostgresStore(DSN)
        self.store.publish(
            self.project,
            snapshot(self.project, [{"id": "T-1", "status": "ready"}]),
        )

    def tearDown(self):
        try:
            clear(self.store, self.project)
        finally:
            self.store.close()

    def test_snapshot_projects_history_and_overview(self):
        self.assertEqual(self.store.health()["backend"], "postgres")
        board = self.store.board(self.project)
        self.assertEqual(board["project"], self.project)
        self.assertEqual(board["snapshot"]["tasks"][0]["id"], "T-1")
        self.assertTrue(any(item["id"] == self.project for item in self.store.projects()))

        history = self.store.history(self.project, 0, 10)
        self.assertTrue(history)
        self.assertEqual(history[-1]["board"]["project"], self.project)

        overview = self.store.overview([self.project])
        self.assertEqual(overview["projects"][0]["id"], self.project)
        self.assertEqual(overview["projects"][0]["tasks"], 1)
        self.assertEqual(overview["projects"][0]["ready"], 1)

        # Republishing replaces the snapshot and advances the sequence.
        self.store.publish(self.project, snapshot(self.project, [{"id": "T-2"}]))
        self.assertEqual(self.store.board(self.project)["snapshot"]["tasks"][0]["id"], "T-2")
        refreshed = self.store.overview([self.project])["projects"][0]
        self.assertGreater(refreshed["seq"], overview["projects"][0]["seq"])

    def test_claims_path_conflicts_and_fencing(self):
        first = self.store.claim(self.project, "T-1", "a", ["src/api/**"], 60)
        self.assertEqual(first["status"], 200)
        conflict = self.store.claim(self.project, "T-1", "b", ["src/web/**"], 60)
        self.assertEqual(conflict["status"], 409)
        self.assertIn("leased by a", conflict["error"])

        path = self.store.claim(self.project, "T-2", "b", ["src/api/v2/**"], 60)
        self.assertEqual(path["status"], 409)
        self.assertIn("path conflict", path["error"])

        ok = self.store.claim(self.project, "T-3", "b", ["docs/**"], 60)
        self.assertEqual(ok["status"], 200)
        self.assertGreater(ok["fencing"], first["fencing"])

        leaves = {(item["task"], item["agent"]) for item in self.store.board(self.project)["leases"]}
        self.assertEqual(leaves, {("T-1", "a"), ("T-3", "b")})

    def test_heartbeat_release_and_expire(self):
        self.assertEqual(
            self.store.claim(self.project, "T-1", "a", ["src/api/**"], 60)["status"], 200
        )
        before = self.store.board(self.project)["leases"][0]["expires_at"]
        self.store.heartbeat(self.project, "a", "T-1", ttl=600)
        after = self.store.board(self.project)["leases"][0]["expires_at"]
        self.assertGreater(after, before)

        self.assertTrue(self.store.release(self.project, "T-1", "a")["released"])
        self.assertFalse(self.store.release(self.project, "T-1", "a")["released"])
        self.assertEqual(self.store.board(self.project)["leases"], [])

        self.store.claim(self.project, "T-1", "a", ["src/api/**"], 60)
        expired = self.store.expire(self.project, "a")
        self.assertEqual(expired["released"], 1)
        self.assertEqual(self.store.board(self.project)["leases"], [])

    def test_stale_lease_needs_steal_and_force_takes_live_lease(self):
        self.store.claim(self.project, "T-1", "a", ["src/api/**"], 60)
        self.store.conn.execute(
            "UPDATE leases SET expires_at=%s WHERE project=%s AND task=%s",
            (ago_iso(10), self.project, "T-1"),
        )
        self.store.conn.commit()

        refused = self.store.claim(self.project, "T-1", "b", ["src/api/**"], 60)
        self.assertEqual(refused["status"], 409)
        self.assertIn("stale lease", refused["error"])

        stolen = self.store.claim(self.project, "T-1", "b", ["src/api/**"], 60, steal=True)
        self.assertEqual(stolen["status"], 200)
        self.assertEqual(stolen["stolen_from"], "a")

        forced = self.store.claim(self.project, "T-1", "a", ["src/api/**"], 60, force=True)
        self.assertEqual(forced["status"], 200)
        self.assertEqual(forced["stolen_from"], "b")

    def test_audit_log_records_lease_mutations(self):
        self.store.claim(self.project, "T-1", "a", ["src/api/**"], 60)
        self.store.release(self.project, "T-1", "a")
        self.store.expire(self.project, "a")
        kinds = {item["type"] for item in self.store.audit(self.project)}
        self.assertIn("snapshot.publish", kinds)
        self.assertIn("lease.claim", kinds)
        self.assertIn("lease.release", kinds)
        self.assertIn("lease.expire", kinds)

    def _assert_project_lock_is_free(self, other):
        result = []
        thread = threading.Thread(
            target=lambda: result.append(
                other.claim(self.project, "T-LOCK", "probe", ["probe/**"], 60)
            ),
            daemon=True,
        )
        thread.start()
        thread.join(timeout=5)
        self.assertFalse(
            thread.is_alive(),
            "an early conflict return left the project transaction open",
        )
        self.assertEqual(result[0]["status"], 200)
        other.release(self.project, "T-LOCK", "probe")

    def test_early_conflict_returns_release_the_project_lock(self):
        # A conflict must commit its (empty) transaction before returning, or
        # the advisory lock stalls every other hub process on this project.
        self.assertEqual(
            self.store.claim(self.project, "T-1", "a", ["src/api/**"], 60)["status"], 200
        )
        self.assertEqual(
            self.store.claim(self.project, "T-1", "b", ["src/web/**"], 60)["status"], 409
        )

        other = PostgresStore(DSN)
        try:
            self._assert_project_lock_is_free(other)

            self.assertEqual(
                other.claim(self.project, "T-2", "c", ["docs/**"], 60)["status"], 200
            )
            release = self.store.release(self.project, "T-2", "a")
            self.assertEqual(release["status"], 409)
            self._assert_project_lock_is_free(other)
        finally:
            other.close()


@unittest.skipUnless(POSTGRES_READY, SKIP_REASON)
class PostgresStartupTests(unittest.TestCase):
    def test_concurrent_first_startup_serializes_schema_creation(self):
        schema = f"aghub_init_{uuid.uuid4().hex[:12]}"
        with psycopg.connect(DSN, autocommit=True) as conn:
            try:
                conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
            except psycopg.errors.InsufficientPrivilege as exc:
                self.skipTest(f"cannot create a scratch schema: {exc}")
        self.addCleanup(self._drop_schema, schema)
        scoped = make_conninfo(DSN, options=f"-csearch_path={schema}")

        barrier = threading.Barrier(12)
        errors = []
        errors_lock = threading.Lock()

        def worker():
            try:
                barrier.wait()
                PostgresStore(scoped).close()
            except BaseException as exc:  # captured and asserted in the main thread
                with errors_lock:
                    errors.append(repr(exc))

        threads = [threading.Thread(target=worker) for _ in range(12)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)
        self.assertFalse(
            any(thread.is_alive() for thread in threads), "schema startup deadlocked"
        )
        self.assertEqual(errors, [])

    @staticmethod
    def _drop_schema(schema):
        with psycopg.connect(DSN, autocommit=True) as conn:
            conn.execute(
                sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema))
            )


@unittest.skipUnless(POSTGRES_READY, SKIP_REASON)
class PostgresHttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server, cls.store = build_server(
            "127.0.0.1", 0, ":memory:", token="secret", postgres=DSN, rate_limit=0
        )
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.store.close()

    def setUp(self):
        self.project = project_name()

    def tearDown(self):
        clear(self.store, self.project)

    def test_health_publish_board_history_audit_and_overview(self):
        self.assertEqual(hubclient.health(self.base)["backend"], "postgres")
        hubclient.publish(
            self.base,
            self.project,
            snapshot(self.project, [{"id": "T-1", "status": "ready"}]),
            token="secret",
        )
        board = hubclient.board(self.base, self.project, token="secret")
        self.assertEqual(board["snapshot"]["tasks"][0]["id"], "T-1")

        history = hubclient.history(self.base, self.project, token="secret")
        self.assertTrue(history["events"])
        self.assertEqual(history["events"][-1]["board"]["project"], self.project)

        audit = hubclient.audit(self.base, self.project, token="secret")
        self.assertTrue(any(item["type"] == "snapshot.publish" for item in audit["events"]))

        overview = hubclient.overview(self.base, token="secret")
        project = next(item for item in overview["projects"] if item["id"] == self.project)
        self.assertEqual(project["ready"], 1)

    def test_http_claim_conflict_and_release(self):
        hubclient.publish(
            self.base,
            self.project,
            snapshot(self.project, [{"id": "T-1"}]),
            token="secret",
        )
        lease = hubclient.claim(
            self.base, self.project, "T-1", "http-a", ["src/api/**"], 60, token="secret"
        )
        self.assertGreaterEqual(lease["fencing"], 1)
        with self.assertRaises(hubclient.HubConflict):
            hubclient.claim(
                self.base, self.project, "T-1", "http-b", ["src/web/**"], 60, token="secret"
            )
        hubclient.release(self.base, self.project, "T-1", "http-a", token="secret")
        data = hubclient.board(self.base, self.project, token="secret")
        self.assertEqual(data["leases"], [])


@unittest.skipUnless(POSTGRES_READY, SKIP_REASON)
class PostgresRaceTests(unittest.TestCase):
    """Two connections must not both win a conflicting lease."""

    def setUp(self):
        self.project = project_name()
        self.store = PostgresStore(DSN)
        self.store.publish(self.project, snapshot(self.project))

    def tearDown(self):
        try:
            clear(self.store, self.project)
        finally:
            self.store.close()

    def _race(self, pairs):
        barrier = threading.Barrier(2)
        results = []
        results_lock = threading.Lock()

        def worker(agent, task, paths):
            store = PostgresStore(DSN)
            try:
                barrier.wait()
                result = store.claim(self.project, task, agent, paths, 60)
                with results_lock:
                    results.append(result)
            finally:
                store.close()

        threads = [threading.Thread(target=worker, args=item) for item in pairs]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        return sorted(item["status"] for item in results)

    def _clear_leases(self):
        for table in ("leases", "agents", "counters", "events"):
            self.store.conn.execute(
                f"DELETE FROM {table} WHERE project=%s", (self.project,)
            )
        self.store.conn.commit()

    def test_same_task_race_grants_one_lease(self):
        for _ in range(10):
            statuses = self._race(
                [("a", "T-RACE", ["src/api/**"]), ("b", "T-RACE", ["src/web/**"])]
            )
            self.assertEqual(statuses, [200, 409])
            self._clear_leases()

    def test_overlapping_path_race_grants_one_lease(self):
        for _ in range(10):
            statuses = self._race(
                [("a", "T-A", ["src/api/**"]), ("b", "T-B", ["src/api/v2/**"])]
            )
            self.assertEqual(statuses, [200, 409])
            self._clear_leases()


if __name__ == "__main__":
    unittest.main()
