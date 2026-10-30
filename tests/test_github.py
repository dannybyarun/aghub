import json
import os
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from aghub import github  # noqa: E402
from aghub.errors import AghubError  # noqa: E402


def pr_event(review=None, head_repo="acme/repo", action="synchronize"):
    event = {
        "action": action,
        "number": 7,
        "pull_request": {
            "number": 7,
            "title": "Ship API",
            "body": "Implements T-003.",
            "html_url": "https://github.com/acme/repo/pull/7",
            "draft": False,
            "user": {"login": "alice"},
            "head": {
                "ref": "agent/be-1/T-003",
                "sha": "a" * 40,
                "repo": {"full_name": head_repo},
            },
            "base": {"ref": "main", "sha": "b" * 40},
        },
    }
    if review:
        event["review"] = review
    return event


def review_event(state="approved", association="MEMBER", body="ship it"):
    return pr_event(
        review={
            "id": 9001,
            "state": state,
            "user": {"login": "alice"},
            "author_association": association,
            "body": body,
        },
        action="submitted",
    )


def packet(**overrides):
    task = {
        "id": "T-003",
        "title": "Ship API",
        "status": "claimed",
        "owner": "be-1",
        "branch": "agent/be-1/T-003",
        "paths": ["src/api/**"],
        "requires_approval": True,
    }
    task.update(overrides.pop("task", {}))
    payload = {
        "project": "demo",
        "board": "abc1234",
        "generated_at": "2026-01-01T00:00:00Z",
        "task": task,
        "status": "claimed",
        "owner_age": 10,
        "approval": {
            "required": True,
            "decision": "pending",
            "reviewer": None,
            "note": "",
            "reviews": [],
        },
        "contracts": [{"ref": "api@1", "status": "frozen", "latest": "1", "stale": False}],
        "evals": [],
        "queue": [],
        "handoff": None,
        "failure": None,
        "changes": {
            "trunk": "main",
            "base": "abc1234",
            "branch": "agent/be-1/T-003",
            "files": [{"status": "M", "path": "src/api/items.py"}],
            "stat": " src/api/items.py | 1 +",
        },
        "actions": [],
    }
    payload.update(overrides)
    return payload


class FakeGitHub:
    def __init__(self):
        self.requests = []
        self.list_comments = []
        self.list_checks = []
        self.fail_status = None

    def handler(self):
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _body(self):
                length = int(self.headers.get("Content-Length") or 0)
                if not length:
                    return None
                return json.loads(self.rfile.read(length).decode("utf-8"))

            def _reply(self, status, payload):
                body = json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _record(self, body):
                owner.requests.append(
                    {
                        "method": self.command,
                        "path": self.path,
                        "body": body,
                        "authorization": self.headers.get("Authorization"),
                        "accept": self.headers.get("Accept"),
                        "user_agent": self.headers.get("User-Agent"),
                    }
                )
                if owner.fail_status:
                    self._reply(owner.fail_status, {"message": "forbidden"})
                    return True
                return False

            def do_GET(self):
                if self._record(None):
                    return
                if "/comments?per_page=100&page=" in self.path:
                    self._reply(200, owner.list_comments)
                    return
                if "/check-runs?" in self.path:
                    self._reply(
                        200, {"total_count": len(owner.list_checks), "check_runs": owner.list_checks}
                    )
                    return
                self._reply(404, {"message": "not found"})

            def do_POST(self):
                body = self._body()
                if self._record(body):
                    return
                if self.path.endswith("/comments"):
                    created = {"id": 456, **body}
                    owner.list_comments.append(created)
                    self._reply(201, created)
                    return
                if self.path.endswith("/check-runs"):
                    created = {"id": 789, **body}
                    owner.list_checks.append(created)
                    self._reply(201, created)
                    return
                self._reply(404, {"message": "not found"})

            def do_PATCH(self):
                body = self._body()
                if self._record(body):
                    return
                if "/issues/comments/" in self.path:
                    self._reply(200, {"id": 123, **body})
                    return
                if "/check-runs/" in self.path:
                    self._reply(200, {"id": 789, **body})
                    return
                self._reply(404, {"message": "not found"})

        return Handler


class GitHubTests(unittest.TestCase):
    def test_event_kind_and_pull_request_info(self):
        self.assertEqual(github.event_kind({"pull_request": {}}), "pull_request")
        self.assertEqual(
            github.event_kind({"review": {}, "pull_request": {}}),
            "pull_request_review",
        )
        info = github.pull_request_info(review_event())
        self.assertEqual(info["number"], 7)
        self.assertEqual(info["branch"], "agent/be-1/T-003")
        self.assertEqual(info["sha"], "a" * 40)
        self.assertEqual(info["action"], "submitted")
        self.assertEqual(info["review"]["state"], "approved")
        self.assertEqual(info["review"]["association"], "MEMBER")
        self.assertEqual(info["review"]["login"], "alice")

    def test_event_kind_uses_environment(self):
        with mock.patch.dict(os.environ, {"GITHUB_EVENT_NAME": "pull_request"}):
            self.assertEqual(github.event_kind(), "pull_request")

    def test_load_event(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "event.json"
            path.write_text(json.dumps({"action": "opened"}))
            self.assertEqual(github.load_event(str(path))["action"], "opened")
            with self.assertRaises(AghubError):
                github.load_event(str(Path(tmp) / "missing.json"))
            path.write_text("{")
            with self.assertRaises(AghubError):
                github.load_event(str(path))

    def test_find_task_by_branch_marker_and_text(self):
        tasks = {
            "T-001": {"id": "T-001", "branch": "agent/a/T-001"},
            "T-002": {"id": "T-002", "branch": "feature/other"},
        }
        self.assertEqual(
            github.find_task(tasks, "agent/a/T-001", "")["id"], "T-001"
        )
        self.assertEqual(
            github.find_task(tasks, "feature/other", "")["id"], "T-002"
        )
        self.assertEqual(
            github.find_task(tasks, "feature/unknown", "aghub-task: T-002")["id"],
            "T-002",
        )
        self.assertEqual(
            github.find_task(tasks, "agent/unknown", "fixes T-001")["id"], "T-001"
        )
        self.assertIsNone(github.find_task(tasks, "feature/unknown", "nothing"))

    def test_comment_body_has_sticky_marker_and_status(self):
        text = github.comment_body(packet())
        self.assertTrue(text.startswith(github.MARKER))
        self.assertIn("T-003 Ship API", text)
        self.assertIn("**approval** pending", text)
        self.assertIn("A human approval is required", text)
        self.assertIn("api@1", text)
        self.assertIn("`abc1234` → `agent/be-1/T-003`", text)
        self.assertIn("src/api/items.py", text)

    def test_comment_body_renders_queue_and_failure(self):
        text = github.comment_body(
            packet(
                queue=[
                    {"id": "Q-1", "status": "rejected", "reason": "tests failed"}
                ],
                failure={"kind": "check", "reason": "pytest failed"},
                approval={
                    "required": True,
                    "decision": "changes_requested",
                    "reviewer": "github-review",
                    "note": "needs tests",
                    "reviews": [],
                },
            )
        )
        self.assertIn("`Q-1` rejected — tests failed", text)
        self.assertIn("**check** — pytest failed", text)
        self.assertIn("needs tests", text)
        self.assertIn("Changes were requested", text)
        self.assertNotIn("A human approval is required", text)

    def test_check_payload_conclusions(self):
        self.assertEqual(github.check_payload(packet())["conclusion"], "action_required")
        self.assertEqual(
            github.check_payload(
                packet(
                    task={"status": "done"},
                    approval={
                        "required": True,
                        "decision": "approved",
                        "reviewer": "r",
                        "note": "",
                        "reviews": [],
                    },
                )
            )["conclusion"],
            "success",
        )
        self.assertEqual(
            github.check_payload(
                packet(queue=[{"id": "Q-1", "status": "rejected", "reason": "no"}])
            )["conclusion"],
            "failure",
        )
        self.assertEqual(
            github.check_payload(
                packet(
                    approval={
                        "required": True,
                        "decision": "changes_requested",
                        "reviewer": "r",
                        "note": "no",
                        "reviews": [],
                    }
                )
            )["conclusion"],
            "action_required",
        )
        self.assertEqual(
            github.check_payload(
                packet(
                    task={"requires_approval": False},
                    approval={
                        "required": False,
                        "decision": "pending",
                        "reviewer": None,
                        "note": "",
                        "reviews": [],
                    },
                    queue=[{"id": "Q-1", "status": "waiting"}],
                )
            )["conclusion"],
            "success",
        )
        self.assertEqual(
            github.check_payload(
                packet(
                    task={"requires_approval": False},
                    approval={
                        "required": False,
                        "decision": "pending",
                        "reviewer": None,
                        "note": "",
                        "reviews": [],
                    },
                )
            )["conclusion"],
            "neutral",
        )

    def test_stale_github_approval_is_flagged(self):
        approval = {
            "required": True,
            "decision": "approved",
            "reviewer": "github-review",
            "note": "ship it",
            "commit": "a" * 40,
            "source": "github",
            "external_id": "9001",
            "reviews": [],
        }
        payload = packet(approval=dict(approval))
        self.assertTrue(github.mark_stale_approval(payload, "b" * 40)["stale"])
        text = github.comment_body(payload)
        self.assertIn("for an older commit", text)
        self.assertIn("Re-approve the current head", text)
        check = github.check_payload(payload)
        self.assertEqual(check["conclusion"], "action_required")
        self.assertIn("approval is for an older commit", check["output"]["summary"])

        fresh = packet(approval=dict(approval))
        self.assertFalse(github.mark_stale_approval(fresh, "a" * 40)["stale"])
        self.assertEqual(github.check_payload(fresh)["conclusion"], "neutral")

        local = packet(
            approval={**approval, "source": None, "commit": "board-sha"}
        )
        self.assertFalse(github.mark_stale_approval(local, "b" * 40)["stale"])

    def test_upsert_comment_creates_then_updates_marker(self):
        fake = FakeGitHub()
        server = ThreadingHTTPServer(("127.0.0.1", 0), fake.handler())
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            api = f"http://127.0.0.1:{server.server_port}"
            created = github.upsert_comment(
                "acme/repo", 7, github.MARKER + "\nhello", token="tok", api=api
            )
            self.assertEqual(created, {"id": 456, "updated": False})
            self.assertEqual(
                fake.requests[0]["path"],
                "/repos/acme/repo/issues/7/comments?per_page=100&page=1",
            )
            self.assertEqual(fake.requests[0]["authorization"], "Bearer tok")
            self.assertEqual(fake.requests[1]["method"], "POST")
            self.assertEqual(fake.requests[1]["body"]["body"], github.MARKER + "\nhello")

            updated = github.upsert_comment(
                "acme/repo", 7, github.MARKER + "\nnew", token="tok", api=api
            )
            self.assertEqual(updated, {"id": 123, "updated": True})
            self.assertEqual(fake.requests[-1]["method"], "PATCH")
            self.assertEqual(fake.requests[-1]["path"], "/repos/acme/repo/issues/comments/456")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_upsert_check_creates_then_updates(self):
        fake = FakeGitHub()
        server = ThreadingHTTPServer(("127.0.0.1", 0), fake.handler())
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            api = f"http://127.0.0.1:{server.server_port}"
            check = {"conclusion": "success", "output": {"title": "t", "summary": "s"}}
            created = github.upsert_check(
                "acme/repo", "a" * 40, check, token="tok", api=api
            )
            self.assertEqual(created, {"id": 789, "updated": False})
            self.assertIn("/commits/" + "a" * 40 + "/check-runs?", fake.requests[0]["path"])
            self.assertEqual(fake.requests[1]["method"], "POST")
            self.assertEqual(fake.requests[1]["body"]["head_sha"], "a" * 40)
            self.assertEqual(fake.requests[1]["body"]["name"], "aghub/review")
            self.assertEqual(fake.requests[1]["body"]["status"], "completed")

            updated = github.upsert_check(
                "acme/repo", "a" * 40, check, token="tok", api=api
            )
            self.assertEqual(updated, {"id": 789, "updated": True})
            self.assertEqual(fake.requests[-1]["method"], "PATCH")
            self.assertEqual(fake.requests[-1]["path"], "/repos/acme/repo/check-runs/789")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_api_errors_are_wrapped(self):
        fake = FakeGitHub()
        fake.fail_status = 403
        server = ThreadingHTTPServer(("127.0.0.1", 0), fake.handler())
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            api = f"http://127.0.0.1:{server.server_port}"
            with self.assertRaises(AghubError) as caught:
                github.upsert_comment("acme/repo", 7, "body", token="tok", api=api)
            self.assertIn("GitHub API 403", str(caught.exception))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_repository_and_token_validation(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(AghubError):
                github._repo(None)
            with self.assertRaises(AghubError):
                github._request("GET", "/x", token=None)
        with self.assertRaises(AghubError):
            github.upsert_check("acme/repo", "", {}, token="tok")

    def test_dry_run_does_not_need_a_token(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(github._repo("acme/repo"), "acme/repo")


if __name__ == "__main__":
    unittest.main()
