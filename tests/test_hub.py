import json
import socket
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from aghub import hubclient  # noqa: E402
from aghub.errors import AghubError  # noqa: E402
from aghub.hub import HubStore, build_server, load_tokens  # noqa: E402
from aghub.util import ago_iso  # noqa: E402


class HubStoreTests(unittest.TestCase):
    def setUp(self):
        self.store = HubStore(":memory:")
        self.store.publish("p", {"project": {"name": "p", "ttl_seconds": 60}})

    def tearDown(self):
        self.store.close()

    def test_snapshot_roundtrip_and_latest_project(self):
        self.store.publish("alpha", {"project": {"name": "alpha"}})
        self.store.publish("beta", {"project": {"name": "beta"}})
        self.assertEqual(self.store.board("alpha")["snapshot"]["project"]["name"], "alpha")
        self.assertEqual(self.store.board()["project"], "beta")
        # Republishing within the same second must still move a project to the front.
        self.store.publish("alpha", {"project": {"name": "alpha", "v": 2}})
        self.assertEqual(self.store.board()["project"], "alpha")
        projects = [item["id"] for item in self.store.projects()]
        self.assertEqual(projects[:3], ["alpha", "beta", "p"])

    def test_live_claim_conflict(self):
        first = self.store.claim("p", "T-1", "a", ["src/api/**"], 60)
        self.assertEqual(first["status"], 200)
        second = self.store.claim("p", "T-1", "b", ["src/web/**"], 60)
        self.assertEqual(second["status"], 409)
        self.assertIn("leased by a", second["error"])

    def test_path_overlap_is_rejected(self):
        self.assertEqual(self.store.claim("p", "T-1", "a", ["src/**"], 60)["status"], 200)
        conflict = self.store.claim("p", "T-2", "b", ["src/web/**"], 60)
        self.assertEqual(conflict["status"], 409)
        self.assertIn("path conflict", conflict["error"])
        self.assertEqual(self.store.claim("p", "T-3", "b", ["docs/**"], 60)["status"], 200)

    def test_stale_lease_needs_steal(self):
        self.assertEqual(self.store.claim("p", "T-1", "a", ["src/api/**"], 60)["status"], 200)
        self.store.conn.execute(
            "UPDATE leases SET expires_at=? WHERE project=? AND task=?", (ago_iso(10), "p", "T-1")
        )
        self.store.conn.commit()
        refused = self.store.claim("p", "T-1", "b", ["src/api/**"], 60)
        self.assertEqual(refused["status"], 409)
        self.assertIn("stale lease", refused["error"])
        taken = self.store.claim("p", "T-1", "b", ["src/api/**"], 60, steal=True)
        self.assertEqual(taken["status"], 200)
        self.assertEqual(taken["stolen_from"], "a")

    def test_force_takes_a_live_lease(self):
        self.store.claim("p", "T-1", "a", ["src/api/**"], 60)
        taken = self.store.claim("p", "T-1", "b", ["src/api/**"], 60, force=True)
        self.assertEqual(taken["status"], 200)
        self.assertEqual(taken["stolen_from"], "a")

    def test_heartbeat_extends_the_lease(self):
        self.store.claim("p", "T-1", "a", ["src/api/**"], 60)
        before = self.store.board("p")["leases"][0]["expires_at"]
        self.store.heartbeat("p", "a", "T-1", ttl=600)
        after = self.store.board("p")["leases"][0]["expires_at"]
        self.assertGreater(after, before)

    def test_release_frees_paths_and_presence(self):
        self.store.claim("p", "T-1", "a", ["src/api/**"], 60)
        result = self.store.release("p", "T-1", "a")
        self.assertTrue(result["released"])
        self.assertEqual(self.store.board("p")["leases"], [])
        agent = self.store.board("p")["agents"][0]
        self.assertIsNone(agent["current_task"])

    def test_fencing_increments(self):
        one = self.store.claim("p", "T-1", "a", ["src/api/**"], 60)
        two = self.store.claim("p", "T-2", "a", ["docs/**"], 60)
        self.assertGreater(two["fencing"], one["fencing"])

    def test_expire_releases_leases(self):
        self.store.claim("p", "T-1", "a", ["src/api/**"], 60)
        result = self.store.expire("p", "a", ttl=60)
        self.assertEqual(result["released"], 1)
        self.assertEqual(self.store.board("p")["leases"], [])

    def test_quota_counter_is_shared_in_store(self):
        first = self.store.consume_quota("tenant", 2)
        second = self.store.consume_quota("tenant", 2)
        third = self.store.consume_quota("tenant", 2)
        self.assertTrue(first["allowed"])
        self.assertTrue(second["allowed"])
        self.assertFalse(third["allowed"])
        self.assertEqual(third["remaining"], 0)
        # A zero limit disables quota entirely.
        self.assertTrue(self.store.consume_quota("unlimited", 0)["allowed"])

    def test_audit_actor_filter(self):
        self.store.publish("audited", {"project": {"name": "audited"}}, actor="alpha")
        self.store.claim(
            "audited", "T-1", "a", ["src/api/**"], 60, actor="alpha"
        )
        self.store.claim("audited", "T-2", "b", ["docs/**"], 60, actor="beta")
        alpha = self.store.audit("audited", actor="alpha")
        beta = self.store.audit("audited", actor="beta")
        self.assertTrue(alpha)
        self.assertTrue(beta)
        self.assertTrue(all(item["actor"] == "alpha" for item in alpha))
        self.assertTrue(all(item["actor"] == "beta" for item in beta))
        self.assertTrue(any(item["type"] == "lease.claim" for item in alpha))
        self.assertEqual(
            [item["type"] for item in alpha if item["type"] == "lease.claim"],
            ["lease.claim"],
        )

    def test_board_lock_is_exclusive_and_reports_its_holder(self):
        first = self.store.acquire_board_lock("p", "a", 60)
        self.assertEqual(first["status"], 200)
        self.assertTrue(first["acquired"])
        self.assertFalse(first["renewed"])
        self.assertGreaterEqual(first["fencing"], 1)

        conflict = self.store.acquire_board_lock("p", "b", 60)
        self.assertEqual(conflict["status"], 409)
        self.assertEqual(conflict["holder"]["holder"], "a")
        self.assertTrue(conflict["holder"]["active"])
        self.assertIn("locked by a", conflict["error"])

        status = self.store.board_lock("p")
        self.assertEqual(status["holder"], "a")
        self.assertEqual(status["fencing"], first["fencing"])
        self.assertEqual(self.store.board("p")["board_lock"]["holder"], "a")

        kinds = [item["type"] for item in self.store.audit("p")]
        self.assertIn("board.lock", kinds)

    def test_board_lock_renews_and_fences_stale_releases(self):
        first = self.store.acquire_board_lock("p", "a", 60)
        renewed = self.store.acquire_board_lock("p", "a", 60)
        self.assertEqual(renewed["status"], 200)
        self.assertTrue(renewed["renewed"])
        self.assertEqual(renewed["fencing"], first["fencing"])

        foreign = self.store.release_board_lock("p", "b")
        self.assertEqual(foreign["status"], 409)
        self.assertEqual(foreign["holder"]["holder"], "a")

        stale = self.store.release_board_lock(
            "p", "a", fencing=first["fencing"] + 1
        )
        self.assertEqual(stale["status"], 409)
        self.assertIn("stale board fencing", stale["error"])

        released = self.store.release_board_lock(
            "p", "a", fencing=first["fencing"]
        )
        self.assertEqual(released["status"], 200)
        self.assertTrue(released["released"])
        self.assertFalse(self.store.board_lock("p")["active"])
        self.assertFalse(self.store.release_board_lock("p", "a")["released"])

        second = self.store.acquire_board_lock("p", "b", 60)
        self.assertEqual(second["status"], 200)
        self.assertGreater(second["fencing"], first["fencing"])
        kinds = [item["type"] for item in self.store.audit("p")]
        self.assertIn("board.unlock", kinds)

    def test_expired_board_lock_can_be_taken_over(self):
        first = self.store.acquire_board_lock("p", "a", 60)
        self.store.conn.execute(
            "UPDATE board_locks SET expires_at=? WHERE project=?", (ago_iso(10), "p")
        )
        self.store.conn.commit()
        self.assertFalse(self.store.board_lock("p")["active"])

        second = self.store.acquire_board_lock("p", "b", 60)
        self.assertEqual(second["status"], 200)
        self.assertEqual(second["stolen_from"], "a")
        self.assertGreater(second["fencing"], first["fencing"])

        # The old holder cannot release the new holder's lease.
        stale = self.store.release_board_lock(
            "p", "a", fencing=first["fencing"]
        )
        self.assertEqual(stale["status"], 409)
        self.assertEqual(self.store.board_lock("p")["holder"], "b")

        forced = self.store.release_board_lock("p", "a", force=True)
        self.assertEqual(forced["status"], 200)
        self.assertTrue(forced["released"])
        self.assertFalse(forced["expired"])
        self.assertFalse(self.store.board_lock("p")["active"])

    def test_prune_trims_old_events_and_caps_counts(self):
        for _ in range(3):
            self.store.publish("p", {"project": {"name": "p"}})
        self.store.conn.execute(
            "UPDATE events SET at=? WHERE project='p'", (ago_iso(40 * 86400),)
        )
        self.store.conn.commit()
        removed = self.store.prune(max_age_days=30)
        self.assertGreater(removed["events"], 0)
        self.assertEqual(self.store.audit("p", limit=100), [])

    def test_prune_caps_events_per_project(self):
        for _ in range(5):
            self.store.publish("p", {"project": {"name": "p"}})
        self.store.prune(max_events=2)
        self.assertLessEqual(len(self.store.audit("p", limit=100)), 2)

    def test_prune_can_scope_to_specific_projects(self):
        self.store.publish("q", {"project": {"name": "q"}})
        self.store.conn.execute("UPDATE events SET at=?", (ago_iso(40 * 86400),))
        self.store.conn.commit()
        self.store.prune(max_age_days=30, projects=["p"])
        # p's events are gone; q's are untouched.
        self.assertEqual(self.store.audit("p", limit=100), [])
        self.assertTrue(self.store.audit("q", limit=100))

    def test_backup_and_restore_roundtrip(self):
        self.store.claim("p", "T-1", "a", ["src/**"], 600)
        archive = self.store.export_project("p")
        self.assertEqual(archive["project"], "p")
        self.assertEqual(len(archive["leases"]), 1)
        fresh = HubStore(":memory:")
        self.addCleanup(fresh.close)
        fresh.import_project(archive)
        board = fresh.board("p")
        self.assertEqual(board["project"], "p")
        self.assertEqual(len(board["leases"]), 1)
        self.assertEqual(board["leases"][0]["agent"], "a")

    def test_import_rejects_non_archive(self):
        with self.assertRaises(AghubError):
            self.store.import_project({"not": "a backup"})


class HubHttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server, cls.store = build_server("127.0.0.1", 0, ":memory:", token="secret")
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.store.close()

    def _local_server(self, db=":memory:", **kwargs):
        server, store = build_server("127.0.0.1", 0, db, **kwargs)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(store.close)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server, store, f"http://127.0.0.1:{server.server_address[1]}"

    @staticmethod
    def _dead_url():
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        return f"http://127.0.0.1:{port}"

    def test_admin_backup_and_restore_are_scoped(self):
        tokens = {
            "root": {"role": "admin"},
            "web": {"role": "admin", "projects": ["web"]},
            "w": {"role": "writer", "projects": ["web"]},
        }
        _server, _store, base = self._local_server(tokens=tokens)
        hubclient.publish(base, "web", {"project": {"name": "web"}, "tasks": [{"id": "T-1"}]}, token="root")
        hubclient.publish(base, "api", {"project": {"name": "api"}}, token="root")

        archive = hubclient.backup(base, "web", token="web")
        self.assertEqual(archive["project"], "web")

        with self.assertRaises(hubclient.HubError) as cross:
            hubclient.backup(base, "api", token="web")
        self.assertEqual(cross.exception.status, 403)

        with self.assertRaises(hubclient.HubError) as writer:
            hubclient.backup(base, "web", token="w")
        self.assertEqual(writer.exception.status, 403)

        archive["project"] = "web2"
        archive["snapshot"]["snapshot"] = archive["snapshot"]["snapshot"].replace(
            '"web"', '"web2"'
        )
        hubclient.restore(base, archive, token="root")
        self.assertEqual(hubclient.board(base, "web2", token="root")["project"], "web2")

        with self.assertRaises(hubclient.HubError) as cross_restore:
            hubclient.restore(
                base,
                {"aghub_backup": 1, "project": "api", "snapshot": {"snapshot": "{}"}},
                token="web",
            )
        self.assertEqual(cross_restore.exception.status, 403)

    def test_admin_prune_requires_admin(self):
        tokens = {"root": {"role": "admin"}, "w": {"role": "writer"}}
        _server, _store, base = self._local_server(tokens=tokens)
        hubclient.publish(base, "p", {"project": {"name": "p"}}, token="root")
        with self.assertRaises(hubclient.HubError) as denied:
            hubclient.prune(base, max_events=0, token="w")
        self.assertEqual(denied.exception.status, 403)
        self.assertIn("events", hubclient.prune(base, max_events=0, token="root")["removed"])

    def test_health_is_open_and_api_requires_token(self):
        self.assertTrue(hubclient.health(self.base)["ok"])
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(self.base + "/api/projects", timeout=3)
        self.assertEqual(caught.exception.code, 401)
        data = hubclient.projects(self.base, token="secret")
        self.assertIsInstance(data["projects"], list)

    def test_snapshot_and_board(self):
        snapshot = {"project": {"name": "p", "ttl_seconds": 60}, "tasks": [{"id": "T-1"}], "agents": []}
        hubclient.publish(self.base, "p", snapshot, token="secret")
        data = hubclient.board(self.base, "p", token="secret")
        self.assertEqual(data["snapshot"]["tasks"][0]["id"], "T-1")

    def test_http_claim_conflict(self):
        hubclient.claim(self.base, "p2", "T-1", "a", ["src/api/**"], 60, token="secret")
        with self.assertRaises(hubclient.HubConflict):
            hubclient.claim(self.base, "p2", "T-1", "b", ["src/web/**"], 60, token="secret")

    def test_http_board_lock_conflict_and_fenced_release(self):
        acquired = hubclient.board_lock(
            self.base, "boardlock", "writer-a", ttl=60, token="secret"
        )
        self.assertTrue(acquired["acquired"])
        status = hubclient.board_lock_status(self.base, "boardlock", token="secret")
        self.assertEqual(status["holder"], "writer-a")
        self.assertTrue(status["active"])

        with self.assertRaises(hubclient.HubConflict) as caught:
            hubclient.board_lock(
                self.base, "boardlock", "writer-b", ttl=60, token="secret"
            )
        self.assertEqual(caught.exception.payload["holder"]["holder"], "writer-a")

        with self.assertRaises(hubclient.HubConflict):
            hubclient.board_unlock(
                self.base,
                "boardlock",
                "writer-a",
                fencing=acquired["fencing"] + 1,
                token="secret",
            )
        released = hubclient.board_unlock(
            self.base,
            "boardlock",
            "writer-a",
            fencing=acquired["fencing"],
            token="secret",
        )
        self.assertTrue(released["released"])
        self.assertFalse(
            hubclient.board_lock_status(self.base, "boardlock", token="secret")["active"]
        )

        snapshot = {"project": {"name": "boardlock"}, "tasks": []}
        hubclient.publish(
            self.base,
            "boardlock",
            snapshot,
            token="secret",
            fencing=acquired["fencing"],
        )
        second = hubclient.board_lock(
            self.base, "boardlock", "writer-b", ttl=60, token="secret"
        )
        with self.assertRaises(hubclient.HubConflict) as stale:
            hubclient.publish(
                self.base,
                "boardlock",
                snapshot,
                token="secret",
                fencing=acquired["fencing"],
            )
        self.assertIn("stale board fencing", str(stale.exception))
        hubclient.publish(
            self.base,
            "boardlock",
            snapshot,
            token="secret",
            fencing=second["fencing"],
        )

    def test_board_lock_requires_writer_and_project_scope(self):
        _, _, base = self._local_server(
            token="root",
            tokens={
                "tenant": {"role": "writer", "projects": ["lock-tenant"]},
                "reader": {"role": "reader", "projects": ["lock-tenant"]},
            },
        )
        hubclient.board_lock(base, "lock-tenant", "a", token="tenant")
        with self.assertRaises(hubclient.HubError) as denied:
            hubclient.board_lock(base, "other", "a", token="tenant")
        self.assertIn("cannot write project", str(denied.exception))
        with self.assertRaises(hubclient.HubError) as readonly:
            hubclient.board_lock(base, "lock-tenant", "b", token="reader")
        self.assertIn("writer token required", str(readonly.exception))
        status = hubclient.board_lock_status(base, "lock-tenant", token="reader")
        self.assertEqual(status["holder"], "a")

    def _read_event(self, response):
        lines = []
        while True:
            raw = response.fp.readline()
            if not raw:
                raise AssertionError("event stream closed")
            line = raw.decode("utf-8").rstrip("\r\n")
            if not line:
                if lines:
                    break
                continue
            if line.startswith(":"):
                continue
            lines.append(line)
        event = {"event": "message", "data": "", "id": None}
        data = []
        for line in lines:
            key, _, value = line.partition(":")
            value = value[1:] if value.startswith(" ") else value
            if key == "event":
                event["event"] = value
            elif key == "data":
                data.append(value)
            elif key == "id":
                event["id"] = value
        event["data"] = "\n".join(data)
        if event["event"] == "message" and not event["data"]:
            return self._read_event(response)
        return event

    def test_event_stream_pushes_board_updates(self):
        first = {
            "project": {"name": "sse", "ttl_seconds": 60},
            "tasks": [{"id": "T-1", "title": "one"}],
        }
        hubclient.publish(self.base, "sse", first, token="secret")
        with urllib.request.urlopen(
            self.base + "/api/events?project=sse&token=secret", timeout=10
        ) as response:
            self.assertEqual(
                response.headers.get("Content-Type"), "text/event-stream; charset=utf-8"
            )
            one = self._read_event(response)
            self.assertEqual(one["event"], "board")
            self.assertIn('"T-1"', one["data"])
            self.assertIsNotNone(one["id"])

            second = {
                "project": {"name": "sse", "ttl_seconds": 60},
                "tasks": [{"id": "T-2", "title": "two"}],
            }
            hubclient.publish(self.base, "sse", second, token="secret")
            two = self._read_event(response)
            self.assertEqual(two["event"], "board")
            self.assertIn('"T-2"', two["data"])
            self.assertGreater(int(two["id"]), int(one["id"]))

    def test_event_stream_replays_after_last_event_id(self):
        first = {
            "project": {"name": "replay", "ttl_seconds": 60},
            "tasks": [{"id": "T-1", "title": "one"}],
        }
        hubclient.publish(self.base, "replay", first, token="secret")
        with urllib.request.urlopen(
            self.base + "/api/events?project=replay&token=secret", timeout=10
        ) as response:
            one = self._read_event(response)
        second = {
            "project": {"name": "replay", "ttl_seconds": 60},
            "tasks": [{"id": "T-2", "title": "two"}],
        }
        hubclient.publish(self.base, "replay", second, token="secret")

        request = urllib.request.Request(
            self.base + "/api/events?project=replay&token=secret",
            headers={"Last-Event-ID": one["id"]},
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            replay = self._read_event(response)
        self.assertEqual(replay["event"], "board")
        self.assertIn('"T-2"', replay["data"])
        self.assertGreater(int(replay["id"]), int(one["id"]))

    def test_oversized_body_is_rejected(self):
        self.server.max_body = 256
        try:
            with self.assertRaises(hubclient.HubError) as caught:
                hubclient.publish(
                    self.base,
                    "big",
                    {"project": {"name": "big"}, "blob": "x" * 1024},
                    token="secret",
                )
            self.assertIn("exceeds 256 bytes", str(caught.exception))
        finally:
            del self.server.max_body

    def test_history_api_returns_recent_board_events(self):
        first = {
            "project": {"name": "history", "ttl_seconds": 60},
            "tasks": [{"id": "T-1", "title": "one"}],
        }
        hubclient.publish(self.base, "history", first, token="secret")
        with urllib.request.urlopen(
            self.base + "/api/board?project=history&token=secret", timeout=3
        ) as response:
            seq = json.loads(response.read().decode())["seq"]
        second = {
            "project": {"name": "history", "ttl_seconds": 60},
            "tasks": [{"id": "T-2", "title": "two"}],
        }
        hubclient.publish(self.base, "history", second, token="secret")
        with urllib.request.urlopen(
            self.base + f"/api/history?project=history&after={seq}&token=secret",
            timeout=3,
        ) as response:
            events = json.loads(response.read().decode())["events"]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["board"]["snapshot"]["tasks"][0]["id"], "T-2")
        self.assertGreater(events[0]["seq"], seq)

    def test_event_stream_requires_token(self):
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(
                self.base + "/api/events?project=sse", timeout=3
            )
        self.assertEqual(caught.exception.code, 401)

    def test_web_page_renders(self):
        with urllib.request.urlopen(self.base + "/", timeout=3) as response:
            body = response.read().decode()
        self.assertIn("<title>aghub</title>", body)
        self.assertIn("/api/board", body)
        self.assertIn("/api/events", body)
        self.assertIn("EventSource", body)
        self.assertIn("polling fallback", body)
        self.assertIn("Contract diff", body)
        self.assertIn("Failure trace", body)
        self.assertIn('id="modal"', body)
        self.assertIn('id="replay"', body)
        self.assertIn("/api/overview", body)
        self.assertIn("Reviews", body)
        # Web review workspace: queue, inline diff, and intent submission.
        self.assertIn('id="review-queue"', body)
        self.assertIn('id="review-open"', body)
        self.assertIn('id="review"', body)
        self.assertIn("/api/review/packet", body)
        self.assertIn("/api/review/requests", body)
        self.assertIn("data-decision", body)
        self.assertIn("diffBlock", body)

    def test_review_packet_and_intent_flow(self):
        snapshot = {
            "project": {"name": "reviews", "ttl_seconds": 60},
            "tasks": [
                {
                    "id": "T-1",
                    "title": "Ship API",
                    "status": "claimed",
                    "owner": "be-1",
                    "branch": "agent/be-1/T-1",
                    "requires_approval": True,
                },
                {"id": "T-2", "title": "Docs", "status": "ready"},
            ],
        }
        hubclient.publish(self.base, "reviews", snapshot, token="secret")
        packet = {
            "task": {"id": "T-1", "title": "Ship API"},
            "approval": {"head": "a" * 40, "decision": "pending"},
            "changes": {"branch": "agent/be-1/T-1", "head": "a" * 40, "patch": "@@ -1 +1 @@\n-x\n+y"},
        }
        published = hubclient.publish_review_packet(
            self.base, "reviews", "T-1", packet, token="secret"
        )
        self.assertEqual(published["head"], "a" * 40)
        stored = hubclient.review_packet(self.base, "reviews", "T-1", token="secret")
        self.assertEqual(stored["packet"]["changes"]["patch"], "@@ -1 +1 @@\n-x\n+y")
        heads = hubclient.review_packets(self.base, "reviews", token="secret")["packets"]
        self.assertEqual(heads[0]["task"], "T-1")
        self.assertEqual(heads[0]["head"], "a" * 40)
        with self.assertRaises(hubclient.HubError) as missing:
            hubclient.review_packet(self.base, "reviews", "T-2", token="secret")
        self.assertIn("404", str(missing.exception))

        created = hubclient.request_review(
            self.base,
            "reviews",
            "T-1",
            "approved",
            note="looks good",
            head="a" * 40,
            token="secret",
        )["request"]
        self.assertEqual(created["status"], "pending")
        self.assertEqual(created["actor"], "admin")
        pending = hubclient.review_requests(
            self.base, "reviews", status="pending", token="secret"
        )["requests"]
        self.assertEqual([item["id"] for item in pending], [created["id"]])

        claimed = hubclient.claim_review_request(
            self.base, created["id"], token="secret"
        )
        self.assertTrue(claimed["claimed"])
        applied = hubclient.resolve_review_request(
            self.base, created["id"], "applied", record_id="RV-1", token="secret"
        )["request"]
        self.assertEqual(applied["status"], "applied")
        self.assertEqual(applied["record_id"], "RV-1")
        all_requests = hubclient.review_requests(
            self.base, "reviews", status="all", token="secret"
        )["requests"]
        self.assertEqual(all_requests[0]["status"], "applied")

        # Non-gated tasks and self-review are rejected at the edge.
        with self.assertRaises(hubclient.HubError) as ungated:
            hubclient.request_review(
                self.base, "reviews", "T-2", "approved", token="secret"
            )
        self.assertIn("400", str(ungated.exception))
        hubclient.publish(
            self.base,
            "self",
            {
                "project": {"name": "self"},
                "tasks": [
                    {
                        "id": "T-9",
                        "owner": "admin",
                        "requires_approval": True,
                    }
                ],
            },
            token="secret",
        )
        with self.assertRaises(hubclient.HubError) as self_review:
            hubclient.request_review(
                self.base, "self", "T-9", "approved", token="secret"
            )
        self.assertIn("400", str(self_review.exception))

    def test_review_request_is_scoped_to_its_project(self):
        server, store, base = self._local_server(
            tokens={
                "writer": {"role": "writer", "projects": ["alpha"], "name": "alice"},
                "other": {"role": "writer", "projects": ["beta"], "name": "bob"},
            }
        )
        hubclient.publish(
            base,
            "alpha",
            {
                "project": {"name": "alpha"},
                "tasks": [
                    {"id": "T-1", "owner": "be-1", "requires_approval": True}
                ],
            },
            token="writer",
        )
        created = hubclient.request_review(
            base, "alpha", "T-1", "changes_requested", token="writer"
        )["request"]
        self.assertEqual(created["reviewer"], "alice")
        with self.assertRaises(hubclient.HubError) as foreign:
            hubclient.claim_review_request(base, created["id"], token="other")
        self.assertIn("403", str(foreign.exception))
        with self.assertRaises(hubclient.HubError) as leak:
            hubclient.review_requests(base, "alpha", token="other")
        self.assertIn("403", str(leak.exception))

    def test_audit_and_overview_apis(self):
        hubclient.publish(
            self.base,
            "audit",
            {"project": {"name": "audit"}, "tasks": [{"id": "T-1", "status": "ready"}]},
            token="secret",
        )
        audit = hubclient.audit(self.base, "audit", token="secret")
        self.assertTrue(
            any(item["type"] == "snapshot.publish" for item in audit["events"])
        )
        overview = hubclient.overview(self.base, token="secret")
        project = next(item for item in overview["projects"] if item["id"] == "audit")
        self.assertEqual(project["tasks"], 1)
        self.assertEqual(project["ready"], 1)

    def test_read_and_scoped_tokens(self):
        server, store = build_server(
            "127.0.0.1",
            0,
            ":memory:",
            token="root",
            read_token="reader",
            tokens={
                "writer": {"role": "writer", "projects": ["scoped"]},
                "empty": {"role": "reader", "projects": []},
            },
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(store.close)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        base = f"http://127.0.0.1:{server.server_address[1]}"
        hubclient.publish(
            base,
            "scoped",
            {"project": {"name": "scoped"}, "tasks": []},
            token="writer",
        )
        with self.assertRaises(hubclient.HubError) as denied:
            hubclient.publish(
                base, "other", {"project": {"name": "other"}, "tasks": []}, token="writer"
            )
        self.assertIn("cannot write project", str(denied.exception))
        data = hubclient.board(base, "scoped", token="reader")
        self.assertEqual(data["project"], "scoped")
        with self.assertRaises(hubclient.HubError) as readonly:
            hubclient.publish(
                base,
                "scoped",
                {"project": {"name": "scoped"}, "tasks": []},
                token="reader",
            )
        self.assertIn("writer token required", str(readonly.exception))

        # A scoped token without ?project= must not fall through to the
        # globally latest project when reading history.
        hubclient.publish(
            base,
            "other",
            {"project": {"name": "other"}, "tasks": []},
            token="root",
        )
        scoped_history = hubclient.history(base, token="writer")
        self.assertTrue(scoped_history["events"])
        self.assertTrue(
            all(item["board"]["project"] == "scoped" for item in scoped_history["events"])
        )
        with self.assertRaises(hubclient.HubError) as outside:
            hubclient.history(base, project="other", token="writer")
        self.assertIn("cannot read project", str(outside.exception))

        # An empty project scope must not fall back to the latest global board.
        for request in (
            lambda: hubclient.board(base, token="empty"),
            lambda: hubclient.history(base, token="empty"),
            lambda: hubclient.projects(base, token="empty"),
        ):
            with self.assertRaises(hubclient.HubError) as empty:
                request()
            self.assertIn("no project scope", str(empty.exception))

    def test_token_request_and_size_quotas(self):
        _, _, base = self._local_server(
            token="root",
            tokens={
                "rate": {
                    "role": "writer",
                    "projects": ["rate"],
                    "name": "rate-tenant",
                    "quota": {"requests_per_minute": 2},
                },
                "size": {
                    "role": "writer",
                    "projects": ["size"],
                    "name": "size-tenant",
                    "quota": {"max_request_bytes": 256},
                },
            },
            rate_limit=0,
        )
        rate = {"project": {"name": "rate"}, "tasks": []}
        hubclient.publish(base, "rate", rate, token="rate")
        hubclient.board(base, "rate", token="rate")
        with self.assertRaises(hubclient.HubError) as limited:
            hubclient.projects(base, token="rate")
        self.assertIn("token quota", str(limited.exception))

        with self.assertRaises(hubclient.HubError) as oversized:
            hubclient.publish(
                base,
                "size",
                {"project": {"name": "size"}, "blob": "x" * 1024},
                token="size",
            )
        self.assertIn("exceeds 256 bytes", str(oversized.exception))
        hubclient.publish(base, "size", {"project": {"name": "size"}}, token="size")
        self.assertEqual(
            hubclient.board(base, "size", token="size")["project"], "size"
        )

    def test_quota_is_shared_across_hubs(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db = str(Path(tmp.name) / "hub.db")
        tokens = {
            "tenant": {
                "role": "writer",
                "projects": ["tenant"],
                "name": "tenant",
                "quota": {"requests_per_minute": 2},
            }
        }
        _, _, first = self._local_server(
            db=db, token="root", tokens=tokens, rate_limit=0
        )
        _, _, second = self._local_server(
            db=db, token="root", tokens=tokens, rate_limit=0
        )
        hubclient.projects(first, token="tenant")
        hubclient.projects(second, token="tenant")
        with self.assertRaises(hubclient.HubError) as limited:
            hubclient.projects(first, token="tenant")
        self.assertIn("token quota", str(limited.exception))

    def test_per_token_audit_export(self):
        _, _, base = self._local_server(
            token="root",
            tokens={
                "alpha": {
                    "role": "writer",
                    "projects": ["alpha"],
                    "name": "alpha",
                },
                "beta": {"role": "writer", "projects": ["beta"], "name": "beta"},
            },
            rate_limit=0,
        )
        for name in ("alpha", "beta"):
            hubclient.publish(
                base,
                name,
                {"project": {"name": name}, "tasks": []},
                token=name,
            )
            hubclient.claim(
                base,
                name,
                "T-1",
                f"agent-{name}",
                ["src/**"],
                60,
                token=name,
            )

        own = hubclient.audit_export(base, token="alpha")
        rows = [json.loads(line) for line in own.splitlines() if line]
        self.assertTrue(rows)
        self.assertTrue(all(row["actor"] == "alpha" for row in rows))
        self.assertTrue(all(row["project"] == "alpha" for row in rows))
        self.assertTrue(any(row["type"] == "snapshot.publish" for row in rows))
        self.assertTrue(any(row["type"] == "lease.claim" for row in rows))

        everything = hubclient.audit_export(base, token="root")
        actors = {json.loads(line)["actor"] for line in everything.splitlines() if line}
        self.assertEqual(actors, {"alpha", "beta"})

        with self.assertRaises(hubclient.HubError) as denied:
            hubclient.audit_export(base, actor="beta", token="alpha")
        self.assertIn("another actor", str(denied.exception))

        with self.assertRaises(hubclient.HubError) as admin_only:
            hubclient.audit(base, token="alpha")
        self.assertIn("403", str(admin_only.exception))

        csv_text = hubclient.audit_export(base, fmt="csv", token="root")
        self.assertTrue(
            csv_text.startswith("seq,at,project,actor,type,payload")
        )
        self.assertIn("alpha", csv_text)
        self.assertIn("beta", csv_text)

    def test_load_tokens_validates_quota(self):
        with tempfile.NamedTemporaryFile(
            "w", suffix=".json", delete=False, encoding="utf-8"
        ) as handle:
            json.dump(
                {
                    "tokens": {
                        "tok": {
                            "role": "writer",
                            "projects": ["p"],
                            "name": "ci",
                            "quota": {
                                "requests_per_minute": 5,
                                "max_request_bytes": 128,
                            },
                        }
                    }
                },
                handle,
            )
            path = Path(handle.name)
        try:
            tokens = load_tokens(str(path))
        finally:
            path.unlink()
        self.assertEqual(tokens["tok"]["actor"], "ci")
        self.assertEqual(tokens["tok"]["quota"]["requests_per_minute"], 5)
        self.assertEqual(tokens["tok"]["quota"]["max_request_bytes"], 128)

        with tempfile.NamedTemporaryFile(
            "w", suffix=".json", delete=False, encoding="utf-8"
        ) as handle:
            json.dump(
                {"tokens": {"tok": {"role": "writer", "quota": {"bogus": 1}}}},
                handle,
            )
            bad = Path(handle.name)
        try:
            with self.assertRaises(AghubError):
                load_tokens(str(bad))
        finally:
            bad.unlink()

    def test_client_failover_across_hubs(self):
        _, _, live = self._local_server(token="secret", rate_limit=0)
        dead = self._dead_url()
        self.assertTrue(hubclient.health([dead, live], token="secret")["ok"])
        snapshot = {"project": {"name": "failover"}, "tasks": []}
        hubclient.publish([dead, live], "failover", snapshot, token="secret")
        self.assertEqual(
            hubclient.board([dead, live], "failover", token="secret")["project"],
            "failover",
        )

    def test_failover_never_overrides_a_conflict(self):
        _, _, first = self._local_server(token="secret", rate_limit=0)
        _, _, second = self._local_server(token="secret", rate_limit=0)
        snapshot = {"project": {"name": "split"}, "tasks": []}
        hubclient.publish(first, "split", snapshot, token="secret")
        hubclient.publish(second, "split", snapshot, token="secret")
        hubclient.claim(
            first, "split", "T-1", "a", ["src/api/**"], 60, token="secret"
        )
        with self.assertRaises(hubclient.HubConflict):
            hubclient.claim(
                [first, second],
                "split",
                "T-1",
                "b",
                ["src/web/**"],
                60,
                token="secret",
            )
        self.assertEqual(
            hubclient.board(second, "split", token="secret")["leases"], []
        )

    def test_health_reports_store_failure(self):
        _, store, base = self._local_server(token="secret", rate_limit=0)
        original = store.health

        def broken():
            raise RuntimeError("db down")

        store.health = broken
        try:
            with self.assertRaises(urllib.error.HTTPError) as caught:
                urllib.request.urlopen(base + "/api/health", timeout=3)
            self.assertEqual(caught.exception.code, 503)
            body = json.loads(caught.exception.read().decode("utf-8"))
            self.assertFalse(body["ok"])
        finally:
            store.health = original

    def test_rate_limit_returns_429(self):
        server, store = build_server(
            "127.0.0.1", 0, ":memory:", token="secret", rate_limit=2
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(store.close)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        base = f"http://127.0.0.1:{server.server_address[1]}"
        hubclient.projects(base, token="secret")
        hubclient.projects(base, token="secret")
        with self.assertRaises(hubclient.HubError) as limited:
            hubclient.projects(base, token="secret")
        self.assertIn("429", str(limited.exception))


if __name__ == "__main__":
    unittest.main()
