"""zaigate policy tests against fixture run dirs built per the run-dir
contract (plans/run-dir-contract.md). Zero engine imports; quota via injected
callables or the fake fixture CLI (ZAI_QUOTA_CMD)."""

import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest

from zaigate import gate, journal, policy

NOW = 1_700_000_000_000
FIXTURE = pathlib.Path(__file__).parent / "fixtures" / "fake_zai_quota.py"


def qi(remaining=15.4, minutes=212, cards=5, status="ok"):
    return {"schema_version": 1, "status": status, "minutes": minutes,
            "quota_remaining_percent": remaining, "cards_available": cards}


class RunDirTestCase(unittest.TestCase):
    def setUp(self):
        self.run_dir = tempfile.mkdtemp(prefix="zr-zaigate-")
        manifest = {
            "run_id": os.path.basename(self.run_dir), "title": "t",
            "goal": None, "adapter": "fake", "cwd": "/tmp", "policy": {},
            "session_id": None, "status": "active",
            "created_at": "2026-10-20T00:00:00+00:00",
        }
        self.manifest_path = os.path.join(self.run_dir, "run.json")
        with open(self.manifest_path, "w") as f:
            json.dump(manifest, f)
        with open(os.path.join(self.run_dir, "events.jsonl"), "w"):
            pass

    def manifest(self):
        with open(self.manifest_path) as f:
            return json.load(f)

    def events_of(self, kind):
        return [e for e in journal.read_events(self.run_dir)
                if e["type"] == kind]


class GateCheckTests(RunDirTestCase):
    def test_dispatch_ok_and_quota_state_event(self):
        result = policy.gate_check(self.run_dir, work_remaining=2,
                                   run_quota_fn=lambda: qi(), now_ms=NOW)
        self.assertEqual(result["verdict"]["verdict"], "dispatch_ok")
        self.assertEqual(len(self.events_of("quota_state")), 1)
        self.assertEqual(self.events_of("quota_state")[0]["payload"]["source"],
                         "gate_check")

    def test_redeem_candidate(self):
        result = policy.gate_check(
            self.run_dir, work_remaining=2,
            run_quota_fn=lambda: qi(remaining=0.4, minutes=185, cards=3),
            now_ms=NOW)
        self.assertEqual(result["verdict"]["verdict"], "redeem_candidate")


class CooldownTests(unittest.TestCase):
    def test_within_window_blocks(self):
        ev = [{"type": "redemption", "payload": {
            "action": "success",
            "next_reset_at_epoch_ms": NOW + 300 * 60_000}}]
        self.assertTrue(policy.cooldown_active(ev, NOW))

    def test_after_window_free(self):
        ev = [{"type": "redemption", "payload": {
            "action": "success",
            "next_reset_at_epoch_ms": NOW - 60 * 60_000}}]
        self.assertFalse(policy.cooldown_active(ev, NOW))

    def test_unknown_window_blocks(self):
        ev = [{"type": "redemption", "payload": {"action": "success"}}]
        self.assertTrue(policy.cooldown_active(ev, NOW))


class RedeemTests(RunDirTestCase):
    def test_success_journals_redemption(self):
        result = policy.redeem(
            self.run_dir, work_remaining=2,
            run_quota_fn=lambda: qi(remaining=0.4, minutes=185, cards=3),
            run_reset_fn=lambda: {"schema_version": 1, "status": "success",
                                  "card_id": 910002,
                                  "next_reset_at_epoch_ms": NOW + 300 * 60_000},
            now_ms=NOW)
        self.assertTrue(result["executed"])
        self.assertEqual(self.events_of("redemption")[-1]["payload"]["action"],
                         "success")

    def test_uncertain_parks(self):
        policy.redeem(
            self.run_dir, work_remaining=2,
            run_quota_fn=lambda: qi(remaining=0.4, minutes=185, cards=3),
            run_reset_fn=lambda: {"schema_version": 1, "status": "uncertain",
                                  "card_id": 910002},
            now_ms=NOW)
        self.assertEqual(self.manifest()["status"], "needs_user")
        self.assertTrue(self.events_of("needs_user"))

    def test_refuses_when_not_candidate(self):
        result = policy.redeem(self.run_dir, work_remaining=2,
                               run_quota_fn=lambda: qi(),
                               run_reset_fn=lambda: self.fail("no"),
                               now_ms=NOW)
        self.assertFalse(result["executed"])


class PauseResumeTests(RunDirTestCase):
    def test_pause_writes_state_and_notify(self):
        result = policy.pause(self.run_dir, reason="quota", eta_minutes=30)
        self.assertTrue(result["paused"])
        self.assertEqual(self.manifest()["status"], "paused_quota")
        notify = pathlib.Path(self.run_dir, "NOTIFY.md").read_text()
        self.assertIn("PAUSED", notify)
        self.assertIn("resume-check", notify)
        self.assertTrue(self.events_of("run_paused"))

    def test_resume_recovers(self):
        policy.pause(self.run_dir, reason="quota", eta_minutes=30)
        result = policy.resume_check(self.run_dir,
                                     run_quota_fn=lambda: qi(remaining=15.4),
                                     now_ms=NOW)
        self.assertTrue(result["resumed"])
        self.assertTrue(self.events_of("quota_recovered"))
        manifest = self.manifest()
        self.assertEqual(manifest["status"], "active")
        self.assertEqual(manifest.get("resume_rechecks"), 0)

    def test_resume_exhausted_rechecks_with_wake_hint(self):
        policy.pause(self.run_dir, reason="quota", eta_minutes=30)
        result = policy.resume_check(
            self.run_dir,
            run_quota_fn=lambda: qi(remaining=0.4, minutes=90, cards=3),
            now_ms=NOW)
        self.assertFalse(result["resumed"])
        self.assertEqual(result["resume_rechecks"], 1)
        self.assertEqual(result["next_check_minutes"], 90)
        self.assertEqual(result["recheck_wake_hint_epoch_ms"],
                         NOW + (90 + 2) * 60_000)

    def test_resume_parks_after_max_rechecks(self):
        policy.pause(self.run_dir, reason="quota", eta_minutes=30)
        exhausted = lambda: qi(remaining=0.4, minutes=90, cards=3)
        last = None
        for _ in range(gate.load_policy({})["max_resume_rechecks"] + 1):
            last = policy.resume_check(self.run_dir, run_quota_fn=exhausted,
                                       now_ms=NOW)
        self.assertTrue(last["parked"])
        self.assertEqual(self.manifest()["status"], "needs_user")

    def test_resume_rejects_wrong_status(self):
        result = policy.resume_check(self.run_dir, run_quota_fn=lambda: qi(),
                                     now_ms=NOW)
        self.assertFalse(result["resumed"])
        self.assertEqual(result["reason"], "status_active")


class CliTests(RunDirTestCase):
    """CLI against the fake quota fixture via ZAI_QUOTA_CMD (contract path)."""

    def _set_mode(self, mode):
        with tempfile.NamedTemporaryFile("w", suffix=".json",
                                         delete=False) as f:
            json.dump({"mode": mode}, f)
            return f.name

    def test_gate_dispatch_ok_via_fixture(self):
        state = self._set_mode("ok")
        try:
            env = dict(os.environ,
                       ZAI_QUOTA_CMD=f"{sys.executable} {FIXTURE}",
                       ZR_FAKE_STATE=state, ZR_FAKE_SUBCOMMAND="quota_info")
            old = os.environ
            os.environ = env
            try:
                import contextlib, io
                from zaigate.__main__ import main as cli_main
                with contextlib.redirect_stdout(io.StringIO()):
                    rc = cli_main(["gate", self.run_dir,
                                   "--work-remaining", "2"])
            finally:
                os.environ = old
            self.assertEqual(rc, 0)
        finally:
            os.unlink(state)

    def test_gate_needs_user_exit_code(self):
        result_rc = policy.gate_check(self.run_dir, work_remaining=2,
                                      run_quota_fn=lambda: qi(
                                          remaining=0.4, minutes=190,
                                          cards=None),
                                      now_ms=NOW)
        self.assertEqual(result_rc["verdict"]["verdict"], "needs_user")


if __name__ == "__main__":
    unittest.main()
