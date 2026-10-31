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


if __name__ == "__main__":
    unittest.main()
