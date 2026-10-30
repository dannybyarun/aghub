import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from aghub import keys, service  # noqa: E402
from aghub.errors import AghubError  # noqa: E402
from aghub.util import ago_iso, now_iso  # noqa: E402


def make_state(ttl=service.EVAL_TTL_DEFAULT, secret="test-secret", attestation=None):
    project = {"eval_ttl_seconds": ttl, "eval_secret": secret}
    if attestation:
        project["eval_attestation"] = attestation
    return SimpleNamespace(project=project, evals=[], agents={})


def make_record(**overrides):
    record = service.eval_record(
        "agent-1",
        "smoke",
        "python3 -m unittest",
        0,
        duration_ms=12,
        branch="agent/agent-1/T-001",
        commit="abc1234",
        machine="test-host",
    )
    record.update(overrides)
    return record


class EvalAttestationTests(unittest.TestCase):
    def test_signed_record_verifies_and_tampering_is_detected(self):
        state = make_state()
        record = service.sign_eval(state, make_record())
        self.assertTrue(service.eval_verified(state, record))
        self.assertEqual(service.eval_short_status(state, record), "ok")

        record["exit_code"] = 7
        self.assertFalse(service.eval_verified(state, record))
        self.assertEqual(service.eval_short_status(state, record), "BAD")

    def test_unsigned_record_fails_the_gate_when_a_key_exists(self):
        state = make_state()
        state.evals = [make_record()]
        gaps = service.eval_gate(state, {"evals": ["smoke"]}, "agent-1")
        self.assertEqual(gaps, ["smoke (not attested)"])

    def test_legacy_boards_without_a_key_accept_unsigned_records(self):
        state = make_state(secret="")
        state.evals = [make_record()]
        self.assertIsNone(service.eval_verified(state, state.evals[0]))
        self.assertEqual(service.eval_gate(state, {"evals": ["smoke"]}, "agent-1"), [])

    def test_agent_signed_record_verifies_and_tampering_is_detected(self):
        private_key, public_key = keys.generate()
        state = make_state(secret="", attestation="ed25519")
        profile = service.register_agent_key(state, "agent-1", public_key)
        record = service.attest_eval(make_record(), private_key, public_key)
        self.assertEqual(record["attestation"]["scheme"], "ed25519")
        self.assertEqual(record["attestation"]["key_id"], profile["key_id"])
        self.assertTrue(service.eval_verified(state, record))

        record["command"] = "echo tampered"
        self.assertFalse(service.eval_verified(state, record))
        self.assertEqual(service.eval_short_status(state, record), "BAD")

    def test_agent_key_must_match_the_published_profile(self):
        private_key, public_key = keys.generate()
        _, other_public = keys.generate()
        state = make_state(secret="", attestation="ed25519")
        service.register_agent_key(state, "agent-1", other_public)
        record = service.attest_eval(make_record(), private_key, public_key)
        self.assertFalse(service.eval_verified(state, record))

    def test_record_eval_requires_a_local_key_on_an_agent_signed_board(self):
        state = make_state(secret="", attestation="ed25519")
        with self.assertRaises(AghubError):
            service.record_eval(state, make_record())


class EvalFingerprintTests(unittest.TestCase):
    def test_fingerprint_tracks_dirty_input_without_breaking_legacy_records(self):
        base = make_record()
        legacy = dict(base)
        legacy.pop("worktree", None)
        self.assertEqual(
            service.eval_fingerprint(base), service.eval_fingerprint(legacy)
        )
        dirty = dict(base, worktree="abc")
        self.assertNotEqual(
            service.eval_fingerprint(base), service.eval_fingerprint(dirty)
        )

    def test_fingerprint_ignores_runtime_output(self):
        first = make_record()
        second = dict(first, duration_ms=9999, stdout="different", exit_code=1)
        self.assertEqual(
            service.eval_fingerprint(first), service.eval_fingerprint(second)
        )


class EvalSandboxPolicyTests(unittest.TestCase):
    def test_auto_escalates_when_the_gate_requires_bubblewrap(self):
        state = make_state()
        self.assertEqual(
            service.eval_mode(state, {"require_sandbox": True}, "auto"), "require"
        )
        state.project["require_eval_sandbox"] = True
        self.assertEqual(service.eval_mode(state, None, "auto"), "require")

    def test_explicit_modes_are_preserved(self):
        state = make_state()
        state.project["require_eval_sandbox"] = True
        self.assertEqual(service.eval_mode(state, None, "off"), "off")
        self.assertEqual(service.eval_mode(state, None, "require"), "require")
        state.project.pop("require_eval_sandbox")
        self.assertEqual(service.eval_mode(state, None, "auto"), "auto")


class EvalFreshnessTests(unittest.TestCase):
    def test_old_pass_is_stale(self):
        state = make_state(ttl=3600)
        record = service.sign_eval(state, make_record(at=ago_iso(7200)))
        state.evals = [record]
        self.assertFalse(service.eval_fresh(state, record))
        gaps = service.eval_gate(state, {"evals": ["smoke"]}, "agent-1")
        self.assertEqual(gaps, ["smoke (stale: 2h old, ttl 1h)"])

    def test_fresh_pass_satisfies_the_gate(self):
        state = make_state(ttl=3600)
        record = service.sign_eval(state, make_record(at=now_iso()))
        state.evals = [record]
        self.assertTrue(service.eval_fresh(state, record))
        self.assertEqual(service.eval_gate(state, {"evals": ["smoke"]}, "agent-1"), [])

    def test_zero_ttl_disables_expiry(self):
        state = make_state(ttl=0)
        record = service.sign_eval(state, make_record(at=ago_iso(10 * 365 * 86400)))
        state.evals = [record]
        self.assertTrue(service.eval_fresh(state, record))
        self.assertEqual(service.eval_gate(state, {"evals": ["smoke"]}, "agent-1"), [])

    def test_failed_pass_still_wins_over_an_older_success(self):
        state = make_state()
        old = service.sign_eval(state, make_record(at=ago_iso(60)))
        new = service.sign_eval(state, make_record(at=now_iso(), exit_code=1, passed=False))
        state.evals = [old, new]
        gaps = service.eval_gate(state, {"evals": ["smoke"]}, "agent-1")
        self.assertEqual(gaps, ["smoke (last run failed)"])


if __name__ == "__main__":
    unittest.main()
