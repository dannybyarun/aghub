import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from aghub import service  # noqa: E402
from aghub.errors import AghubError  # noqa: E402
from aghub.util import ago_iso, now_iso  # noqa: E402

HEAD = "a" * 40
OTHER = "b" * 40


def fake_state(project=None, approvals=None, tasks=None):
    def emit(*args, **kwargs):
        return None

    return SimpleNamespace(
        project=project or {},
        approvals=list(approvals or []),
        tasks=dict(tasks or {}),
        agents={},
        queue=[],
        new_approvals=[],
        emit=emit,
    )


def review(
    record_id,
    agent,
    *,
    decision="approved",
    commit=HEAD,
    binding="commit",
    at=None,
    reviewer=None,
    github_login=None,
):
    record = {
        "id": record_id,
        "at": at or now_iso(),
        "task": "T-1",
        "agent": agent,
        "decision": decision,
        "note": "",
        "commit": commit,
    }
    if binding:
        record["binding"] = binding
    if reviewer:
        record["reviewer"] = reviewer
    if github_login:
        record["github_login"] = github_login
    return record


def task(**overrides):
    record = {
        "id": "T-1",
        "title": "Billing",
        "owner": "be-1",
        "requires_approval": True,
    }
    record.update(overrides)
    return record


class ReviewPolicyTests(unittest.TestCase):
    def test_two_distinct_reviewers_are_required_for_the_current_head(self):
        state = fake_state(
            {"review": {"approvals": 2}},
            [
                review("R-1", "fe-1"),
                review("R-2", "ci"),
            ],
        )
        summary = service.approval_summary(state, task(), head=HEAD)
        self.assertTrue(summary["satisfied"])
        self.assertEqual(summary["approved_count"], 2)
        self.assertEqual(summary["missing_approvals"], 0)

        state = fake_state(
            {"review": {"approvals": 2}},
            [review("R-1", "fe-1"), review("R-2", "fe-1")],
        )
        summary = service.approval_summary(state, task(), head=HEAD)
        self.assertFalse(summary["satisfied"])
        self.assertEqual(summary["approved_count"], 1)
        self.assertIn("requires 2 approvals", summary["reason"])

        state = fake_state(
            {"review": {"approvals": 2}},
            [review("R-1", "fe-1"), review("R-2", "ci", commit=OTHER)],
        )
        summary = service.approval_summary(state, task(), head=HEAD)
        self.assertFalse(summary["satisfied"])
        self.assertEqual(summary["approved_count"], 1)
        self.assertEqual(len(summary["stale"]), 1)
        self.assertIn("current head", summary["reason"])

    def test_task_fields_tighten_the_board_policy(self):
        state = fake_state(
            {
                "review": {
                    "approvals": 2,
                    "ttl_seconds": 3600,
                    "groups": {
                        "backend": ["be-1", "ci"],
                        "frontend": ["fe-1"],
                    },
                    "required_groups": ["backend"],
                    "reviewers": ["fe-1", "be-1", "ci"],
                }
            }
        )
        policy = service.review_policy_for(
            state,
            task(
                required_approvals=1,
                review_ttl_seconds=7200,
                review_groups=["frontend"],
            ),
        )
        self.assertEqual(policy["approvals"], 2)
        self.assertEqual(policy["ttl_seconds"], 3600)
        self.assertEqual(policy["required_groups"], ["backend", "frontend"])

        tighter = service.review_policy_for(
            state,
            task(required_approvals=4, review_ttl_seconds=60),
        )
        self.assertEqual(tighter["approvals"], 4)
        self.assertEqual(tighter["ttl_seconds"], 60)

    def test_task_reviewers_never_loosen_a_later_board_allowlist(self):
        state = fake_state(
            {"review": {"reviewers": ["fe-1", "be-1"]}},
            [review("R-1", "fe-1")],
        )
        narrowed = task(reviewers=["fe-1"])
        self.assertTrue(
            service.approval_summary(state, narrowed, head=HEAD)["satisfied"]
        )

        state.project["review"]["reviewers"] = ["ci"]
        summary = service.approval_summary(state, narrowed, head=HEAD)
        self.assertFalse(summary["satisfied"])
        self.assertEqual(summary["reviewers"], [])
        self.assertTrue(summary["allowlist"])
        self.assertEqual(len(summary["ineligible"]), 1)
        self.assertIn("allowed: none", summary["reason"])

    def test_required_group_and_reviewer_allowlist(self):
        state = fake_state(
            {
                "review": {
                    "approvals": 1,
                    "groups": {"backend": ["be-1"]},
                    "required_groups": ["backend"],
                    "reviewers": ["fe-1", "be-1"],
                }
            },
            [review("R-1", "fe-1")],
        )
        summary = service.approval_summary(state, task(), head=HEAD)
        self.assertFalse(summary["satisfied"])
        self.assertEqual(summary["missing_groups"], ["backend"])
        self.assertTrue(summary["groups"]["backend"]["defined"])
        self.assertIn("review group 'backend'", summary["reason"])

        state = fake_state(
            {
                "review": {
                    "approvals": 1,
                    "groups": {"backend": ["be-1"]},
                    "required_groups": ["backend"],
                    "reviewers": ["fe-1", "be-1"],
                }
            },
            [review("R-1", "be-1")],
        )
        summary = service.approval_summary(state, task(), head=HEAD)
        self.assertTrue(summary["satisfied"])
        self.assertEqual(summary["missing_groups"], [])

        state = fake_state(
            {
                "review": {
                    "approvals": 1,
                    "reviewers": ["be-1"],
                }
            },
            [review("R-1", "fe-1")],
        )
        summary = service.approval_summary(state, task(), head=HEAD)
        self.assertFalse(summary["satisfied"])
        self.assertEqual(summary["approved_count"], 0)
        self.assertEqual(len(summary["ineligible"]), 1)
        self.assertIn("not from an eligible reviewer", summary["reason"])

    def test_expired_approvals_stop_counting(self):
        state = fake_state(
            {"review": {"approvals": 1, "ttl_seconds": 60}},
            [review("R-1", "fe-1", at=ago_iso(120))],
        )
        summary = service.approval_summary(state, task(), head=HEAD)
        self.assertFalse(summary["satisfied"])
        self.assertEqual(summary["approved_count"], 0)
        self.assertEqual(len(summary["expired"]), 1)
        self.assertIn("expired", summary["reason"])

    def test_legacy_unbound_reviews_still_count(self):
        state = fake_state(
            {"review": {"approvals": 1}},
            [review("R-1", "fe-1", commit=None, binding=None)],
        )
        summary = service.approval_summary(state, task(), head=HEAD)
        self.assertTrue(summary["satisfied"])
        self.assertEqual(summary["approved_count"], 1)
        self.assertEqual(summary["stale"], [])

    def test_reviewer_identity_is_checked_for_self_review_and_allowlist(self):
        state = fake_state(
            {"review": {"reviewers": ["fe-1"]}},
            tasks={"T-1": task()},
        )
        with self.assertRaises(AghubError) as owned:
            service.approve(state, "T-1", "ci", reviewer="be-1")
        self.assertIn("owns T-1", str(owned.exception))

        with self.assertRaises(AghubError) as ineligible:
            service.approve(state, "T-1", "ci", reviewer="ci")
        self.assertIn("not an eligible reviewer", str(ineligible.exception))

        record = service.approve(
            state, "T-1", "ci", reviewer="fe-1", github_login="alice"
        )
        self.assertEqual(record["agent"], "ci")
        self.assertEqual(record["reviewer"], "fe-1")
        self.assertEqual(record["github_login"], "alice")

    def test_policy_updates_validate_groups_and_the_allowlist(self):
        state = fake_state(
            {
                "review": {
                    "groups": {"backend": ["be-1"]},
                    "required_groups": ["backend"],
                }
            }
        )
        with self.assertRaises(AghubError) as undefined:
            service.set_review_policy(
                state,
                {"groups": {"frontend": ["fe-1"]}, "required_groups": ["missing"]},
                "admin",
            )
        self.assertIn("not defined", str(undefined.exception))

        with self.assertRaises(AghubError) as outside:
            service.set_review_policy(
                state,
                {
                    "groups": {"frontend": ["fe-1"]},
                    "reviewers": ["be-1"],
                },
                "admin",
            )
        self.assertIn("not in the reviewer allowlist", str(outside.exception))

        policy = service.set_review_policy(
            state,
            {
                "approvals": 2,
                "ttl_seconds": 1800,
                "groups": {"frontend": ["fe-1"]},
                "required_groups": ["backend", "frontend"],
                "reviewers": ["fe-1", "be-1"],
                "github": {"Alice": "fe-1"},
            },
            "admin",
        )
        self.assertEqual(policy["approvals"], 2)
        self.assertEqual(policy["ttl_seconds"], 1800)
        self.assertEqual(policy["required_groups"], ["backend", "frontend"])
        self.assertEqual(policy["github"], {"alice": "fe-1"})
        self.assertEqual(service.github_reviewer(state, "ALICE"), "fe-1")
        self.assertEqual(service.github_reviewer(state, "bob", "ci"), "ci")

        cleared = service.set_review_policy(state, {"clear": True}, "admin")
        self.assertEqual(cleared["approvals"], 1)
        self.assertIsNone(cleared["ttl_seconds"])
        self.assertEqual(cleared["groups"], {})
        self.assertEqual(cleared["github"], {})

    def test_task_policy_flags_imply_an_approval_gate(self):
        state = fake_state(
            {
                "review": {
                    "approvals": 2,
                    "groups": {"backend": ["be-1", "ci"]},
                    "required_groups": ["backend"],
                    "reviewers": ["fe-1", "be-1", "ci"],
                }
            }
        )
        created = service.add_task(
            state,
            "Billing",
            ["src/api/**"],
            [],
            [],
            "",
            "fe-1",
            required_approvals=3,
            review_groups=["backend"],
            reviewers=["fe-1"],
            review_ttl=120,
        )
        self.assertTrue(created["requires_approval"])
        self.assertEqual(created["required_approvals"], 3)
        self.assertEqual(created["review_groups"], ["backend"])
        self.assertEqual(created["reviewers"], ["fe-1"])
        self.assertEqual(created["review_ttl_seconds"], 120)

        with self.assertRaises(AghubError) as unknown:
            service.add_task(
                state,
                "Unknown group",
                [],
                [],
                [],
                "",
                "fe-1",
                review_groups=["nope"],
            )
        self.assertIn("unknown review group", str(unknown.exception))


class ProtectedPathPolicyTests(unittest.TestCase):
    def project(self, **review):
        base = {
            "approvals": 1,
            "groups": {
                "payments": ["be-1"],
                "security": ["sec-1", "fe-1"],
            },
        }
        base.update(review)
        return {"review": base}

    def test_protected_paths_do_not_bite_when_untouched(self):
        state = fake_state(
            self.project(protected=[{"path": "src/payments/**", "require": "payments"}]),
            [review("R-1", "fe-1")],
        )
        summary = service.approval_summary(
            state, task(), head=HEAD, changed=["src/web/app.py"]
        )
        self.assertTrue(summary["satisfied"])
        self.assertEqual(summary["protected"], [])
        self.assertEqual(summary["required_approvals"], 1)

    def test_protected_paths_add_a_group_and_an_approval(self):
        state = fake_state(
            self.project(protected=[{"path": "src/payments/**", "require": "payments"}]),
            [review("R-1", "fe-1")],
        )
        summary = service.approval_summary(
            state, task(), head=HEAD, changed=["src/payments/charge.py"]
        )
        self.assertFalse(summary["satisfied"])
        self.assertEqual(summary["required_approvals"], 2)
        self.assertEqual([hit["path"] for hit in summary["protected"]], ["src/payments/**"])
        self.assertEqual(summary["protected"][0]["files"], ["src/payments/charge.py"])
        self.assertIn("payments", summary["missing_groups"])
        self.assertIn("protected paths", summary["reason"])
        self.assertIn("src/payments/**", summary["reason"])

        state = fake_state(
            self.project(protected=[{"path": "src/payments/**", "require": "payments"}]),
            [review("R-1", "fe-1"), review("R-2", "be-1")],
        )
        summary = service.approval_summary(
            state, task(), head=HEAD, changed=["src/payments/charge.py"]
        )
        self.assertTrue(summary["satisfied"])
        self.assertEqual(summary["required_approvals"], 2)
        self.assertEqual(summary["groups"]["payments"]["source"], "protected")

    def test_protected_paths_without_a_group_still_need_extra_reviewer(self):
        state = fake_state(
            self.project(protected=["migrations/**"]),
            [review("R-1", "fe-1")],
        )
        summary = service.approval_summary(
            state, task(), head=HEAD, changed=["migrations/001.sql"]
        )
        self.assertFalse(summary["satisfied"])
        self.assertEqual(summary["required_approvals"], 2)
        self.assertIn("still need 1 more", summary["reason"])

        state = fake_state(
            self.project(protected=["migrations/**"]),
            [review("R-1", "fe-1"), review("R-2", "be-1")],
        )
        self.assertTrue(
            service.approval_summary(
                state, task(), head=HEAD, changed=["migrations/001.sql"]
            )["satisfied"]
        )

    def test_unknown_change_set_keeps_the_policy_intact(self):
        state = fake_state(
            self.project(protected=["migrations/**"]),
            [review("R-1", "fe-1")],
        )
        summary = service.approval_summary(state, task(), head=HEAD, changed=None)
        self.assertTrue(summary["satisfied"])
        self.assertIsNone(summary["changed"])

    def test_gate_accepts_the_change_set(self):
        state = fake_state(
            self.project(protected=["migrations/**"]),
            [review("R-1", "fe-1")],
        )
        self.assertIsNone(
            service.approval_gate(state, task(), head=HEAD, changed=["src/web.py"])
        )
        self.assertIn(
            "requires 2 approvals",
            service.approval_gate(
                state, task(), head=HEAD, changed=["migrations/001.sql"]
            ),
        )

    def test_policy_round_trips_protected_paths(self):
        class Project(dict):
            pass

        project = Project()
        state = fake_state(project)
        state.project = project
        policy = service.set_review_policy(
            state,
            {
                "groups": {"payments": ["be-1"]},
                "protected": ["src/payments/**=payments"],
            },
            "admin",
        )
        self.assertEqual(
            policy["protected"],
            [{"path": "src/payments/**", "require": "payments"}],
        )
        self.assertEqual(
            service.normalize_review_policy(state.project)["protected"],
            policy["protected"],
        )
        normalized = service.normalize_review_policy(
            {"review": {"protected": ["migrations/**"]}}
        )
        self.assertEqual(
            normalized["protected"], [{"path": "migrations/**", "require": None}]
        )
        policy = service.set_review_policy(
            state, {"unprotected": ["src/payments/**"]}, "admin"
        )
        self.assertEqual(policy["protected"], [])

    def test_protected_paths_require_a_defined_group(self):
        class Project(dict):
            pass

        project = Project()
        state = fake_state(project)
        state.project = project
        with self.assertRaises(AghubError) as raised:
            service.set_review_policy(
                state, {"protected": ["src/payments/**=payments"]}, "admin"
            )
        self.assertIn("undefined review group", str(raised.exception))


class ReviewSlaTests(unittest.TestCase):
    def test_sla_marks_a_stale_review_request_overdue(self):
        state = fake_state({"review": {"approvals": 1, "sla_seconds": 3600}})
        state.queue = [
            {
                "id": "Q-1",
                "task": "T-1",
                "status": "waiting",
                "submitted_at": ago_iso(7200),
                "submitted_commit": HEAD,
            }
        ]
        summary = service.approval_summary(state, task(), head=HEAD)
        self.assertTrue(summary["overdue"])
        self.assertGreaterEqual(summary["pending_seconds"], 7200)
        self.assertIn("SLA", summary["reason"])
        self.assertIn("overdue", summary["reason"])

    def test_sla_is_quiet_before_the_deadline(self):
        state = fake_state({"review": {"approvals": 1, "sla_seconds": 3600}})
        state.queue = [
            {
                "id": "Q-1",
                "task": "T-1",
                "status": "waiting",
                "submitted_at": ago_iso(60),
                "submitted_commit": HEAD,
            }
        ]
        summary = service.approval_summary(state, task(), head=HEAD)
        self.assertFalse(summary["overdue"])
        self.assertNotIn("SLA", summary["reason"])

    def test_a_newer_head_restarts_the_clock(self):
        state = fake_state({"review": {"approvals": 1, "sla_seconds": 3600}})
        state.queue = [
            {
                "id": "Q-1",
                "task": "T-1",
                "status": "rejected",
                "submitted_at": ago_iso(7200),
                "submitted_commit": OTHER,
            }
        ]
        task_record = task(claimed_at=ago_iso(30))
        summary = service.approval_summary(state, task_record, head=HEAD)
        self.assertFalse(summary["overdue"])
        self.assertLess(summary["pending_seconds"], 120)

    def test_sla_is_absent_without_a_deadline_or_a_request(self):
        state = fake_state({"review": {"approvals": 1}})
        state.queue = []
        summary = service.approval_summary(state, task(), head=HEAD)
        self.assertFalse(summary["overdue"])
        self.assertIsNone(summary["pending_since"])
        self.assertIsNone(summary["sla_seconds"])

    def test_sla_is_packaged_into_the_policy(self):
        policy = service.normalize_review_policy(
            {"review": {"approvals": 1, "sla_seconds": 900}}
        )
        self.assertEqual(policy["sla_seconds"], 900)
        self.assertIsNone(
            service.normalize_review_policy({"review": {"sla_seconds": 0}})[
                "sla_seconds"
            ]
        )
        bits = service.describe_review_policy(
            fake_state({"review": {"reviewers": [], "sla_seconds": 900}})
        )
        self.assertIn("review SLA", bits)


if __name__ == "__main__":
    unittest.main()
