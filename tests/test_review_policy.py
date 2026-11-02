import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from aghub import service  # noqa: E402
from aghub.cli import _parse_protect  # noqa: E402
from aghub.errors import AghubError  # noqa: E402
from aghub.util import ago_iso, now_iso  # noqa: E402

HEAD = "a" * 40
OTHER = "b" * 40


def fake_state(project=None, approvals=None, tasks=None, queue=None):
    def emit(*args, **kwargs):
        return None

    return SimpleNamespace(
        project=project or {},
        approvals=list(approvals or []),
        tasks=dict(tasks or {}),
        agents={},
        queue=list(queue or []),
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


class ProtectedPathTests(unittest.TestCase):
    def _state(self, approvals_list):
        return fake_state(
            {
                "review": {
                    "approvals": 1,
                    "groups": {"security": ["sec-1"]},
                    "protected_paths": [
                        {"paths": ["src/security/**"], "groups": ["security"]}
                    ],
                }
            },
            approvals_list,
        )

    def test_protected_group_is_required_only_when_the_diff_matches(self):
        # An ordinary reviewer satisfies the base policy off the protected path.
        state = self._state([review("R-1", "ci")])
        clean = service.approval_summary(
            state, task(), head=HEAD, changed_paths=["src/api/items.py"]
        )
        self.assertTrue(clean["satisfied"])
        self.assertEqual(clean["required_groups"], [])
        self.assertIs(clean["protected"][0]["triggered"], False)

        # The same approval no longer satisfies a diff that touches the path.
        state = self._state([review("R-1", "ci")])
        touched = service.approval_summary(
            state, task(), head=HEAD, changed_paths=["src/security/keys.py"]
        )
        self.assertFalse(touched["satisfied"])
        self.assertEqual(touched["required_groups"], ["security"])
        self.assertEqual(touched["missing_groups"], ["security"])
        self.assertTrue(touched["protected"][0]["triggered"])
        self.assertEqual(
            touched["protected"][0]["matched"], ["src/security/keys.py"]
        )
        self.assertIs(touched["protected"][0]["satisfied"], False)

        # A member of the protected group clears it.
        state = self._state([review("R-1", "sec-1")])
        ok = service.approval_summary(
            state, task(), head=HEAD, changed_paths=["src/security/keys.py"]
        )
        self.assertTrue(ok["satisfied"])
        self.assertIs(ok["protected"][0]["satisfied"], True)

    def test_protected_rules_are_advisory_without_a_diff(self):
        state = self._state([review("R-1", "ci")])
        summary = service.approval_summary(state, task(), head=HEAD)
        self.assertTrue(summary["satisfied"])
        self.assertFalse(summary["protected_known"])
        self.assertIsNone(summary["protected"][0]["triggered"])
        self.assertEqual(summary["required_groups"], [])

    def test_protected_path_can_require_a_named_reviewer(self):
        project = {
            "review": {
                "approvals": 1,
                "protected_paths": [
                    {"paths": ["**/auth.py"], "reviewers": ["alice"]}
                ],
            }
        }
        state = fake_state(project, [review("R-1", "bob")])
        gated = service.approval_summary(
            state, task(), head=HEAD, changed_paths=["src/auth.py"]
        )
        self.assertFalse(gated["satisfied"])
        self.assertIn("alice", gated["reason"])
        self.assertIn("protected paths", gated["reason"])

        state = fake_state(
            project, [review("R-1", "bob"), review("R-2", "alice")]
        )
        ok = service.approval_summary(
            state, task(), head=HEAD, changed_paths=["src/auth.py"]
        )
        self.assertTrue(ok["satisfied"])

    def test_protected_path_can_raise_the_approval_count(self):
        project = {
            "review": {
                "approvals": 1,
                "protected_paths": [
                    {"paths": ["migrations/**"], "approvals": 2}
                ],
            }
        }
        state = fake_state(project, [review("R-1", "ci")])
        one = service.approval_summary(
            state, task(), head=HEAD, changed_paths=["migrations/001.sql"]
        )
        self.assertFalse(one["satisfied"])
        self.assertEqual(one["required_approvals"], 2)
        self.assertIn("requires 2 approvals", one["reason"])

        state = fake_state(project, [review("R-1", "ci"), review("R-2", "fe-1")])
        two = service.approval_summary(
            state, task(), head=HEAD, changed_paths=["migrations/001.sql"]
        )
        self.assertTrue(two["satisfied"])
        self.assertEqual(two["required_approvals"], 2)

        # Off the protected path the base count of one still applies.
        state = fake_state(project, [review("R-1", "ci")])
        off = service.approval_summary(
            state, task(), head=HEAD, changed_paths=["README.md"]
        )
        self.assertTrue(off["satisfied"])
        self.assertEqual(off["required_approvals"], 1)

    def test_policy_set_validates_and_stores_protected_rules(self):
        state = fake_state({"review": {"groups": {"security": ["sec-1"]}}})
        with self.assertRaises(AghubError) as undefined:
            service.set_review_policy(
                state,
                {"protected_paths": [{"paths": ["x/**"], "groups": ["ghost"]}]},
                "admin",
            )
        self.assertIn("undefined group", str(undefined.exception))

        with self.assertRaises(AghubError) as outside:
            service.set_review_policy(
                state,
                {
                    "reviewers": ["sec-1"],
                    "protected_paths": [
                        {"paths": ["x/**"], "reviewers": ["stranger"]}
                    ],
                },
                "admin",
            )
        self.assertIn("not in the reviewer allowlist", str(outside.exception))

        policy = service.set_review_policy(
            state,
            {"protected_paths": [{"paths": ["src/security/**"], "groups": ["security"]}]},
            "admin",
        )
        self.assertEqual(len(policy["protected_paths"]), 1)
        self.assertEqual(policy["protected_paths"][0]["groups"], ["security"])

        cleared = service.set_review_policy(state, {"clear_protected": True}, "admin")
        self.assertEqual(cleared["protected_paths"], [])


class ReviewerSlaTests(unittest.TestCase):
    def test_sla_breaches_when_a_review_waits_past_the_threshold(self):
        state = fake_state(
            {"review": {"approvals": 1, "sla_seconds": 60}},
            queue=[
                {"task": "T-1", "status": "waiting", "submitted_at": ago_iso(120)}
            ],
        )
        summary = service.approval_summary(state, task(), head=HEAD)
        self.assertFalse(summary["satisfied"])
        self.assertTrue(summary["sla_breached"])
        self.assertGreaterEqual(summary["pending_age"], 120)

    def test_fresh_reviews_and_satisfied_tasks_are_not_overdue(self):
        fresh = fake_state(
            {"review": {"approvals": 1, "sla_seconds": 60}},
            queue=[
                {"task": "T-1", "status": "waiting", "submitted_at": ago_iso(5)}
            ],
        )
        self.assertFalse(
            service.approval_summary(fresh, task(), head=HEAD)["sla_breached"]
        )

        satisfied = fake_state(
            {"review": {"approvals": 1, "sla_seconds": 60}},
            [review("R-1", "fe-1")],
            queue=[
                {"task": "T-1", "status": "waiting", "submitted_at": ago_iso(900)}
            ],
        )
        summary = service.approval_summary(satisfied, task(), head=HEAD)
        self.assertTrue(summary["satisfied"])
        self.assertFalse(summary["sla_breached"])

    def test_changes_requested_pauses_the_reviewer_sla(self):
        state = fake_state(
            {"review": {"approvals": 1, "sla_seconds": 60}},
            [review("R-1", "fe-1", decision="changes_requested", at=ago_iso(900))],
            queue=[
                {"task": "T-1", "status": "waiting", "submitted_at": ago_iso(900)}
            ],
        )
        summary = service.approval_summary(state, task(), head=HEAD)
        self.assertFalse(summary["satisfied"])
        self.assertEqual(summary["decision"], "changes_requested")
        self.assertFalse(summary["sla_breached"])

    def test_pending_since_falls_back_to_the_task_when_not_queued(self):
        state = fake_state({"review": {"approvals": 1, "sla_seconds": 60}})
        waiting = task(branch="agent/be-1/T-1", updated_at=ago_iso(300))
        summary = service.approval_summary(state, waiting, head=HEAD)
        self.assertTrue(summary["sla_breached"])

        unstarted = task(branch=None, owner=None, updated_at=ago_iso(300))
        quiet = service.approval_summary(state, unstarted, head=HEAD)
        self.assertIsNone(quiet["pending_since"])
        self.assertFalse(quiet["sla_breached"])


class ParseProtectTests(unittest.TestCase):
    def test_paths_and_requirements_parse(self):
        rule = _parse_protect("src/db/**,**/schema.sql:group=db,reviewer=carol,approvals=2")
        self.assertEqual(rule["paths"], ["src/db/**", "**/schema.sql"])
        self.assertEqual(rule["groups"], ["db"])
        self.assertEqual(rule["reviewers"], ["carol"])
        self.assertEqual(rule["approvals"], 2)

    def test_paths_only_is_allowed(self):
        rule = _parse_protect("src/security/**")
        self.assertEqual(rule["paths"], ["src/security/**"])
        self.assertEqual(rule["groups"], [])

    def test_bad_specs_are_rejected(self):
        with self.assertRaises(AghubError):
            _parse_protect(":group=db")
        with self.assertRaises(AghubError):
            _parse_protect("x/**:group")
        with self.assertRaises(AghubError):
            _parse_protect("x/**:approvals=lots")
        with self.assertRaises(AghubError):
            _parse_protect("x/**:weird=1")


if __name__ == "__main__":
    unittest.main()
