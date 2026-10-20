"""Pure-verdict tests for zaigate.gate.evaluate (plan §B6 table)."""

import unittest

from zaigate import gate

NOW = 1_700_000_000_000


def qi(status="ok", remaining=15.4, minutes=212, cards=5, next_reset=None):
    payload = {"schema_version": 1, "status": status, "minutes": minutes,
               "quota_remaining_percent": remaining, "cards_available": cards}
    if next_reset is not None:
        payload["next_reset_at_epoch_ms"] = next_reset
    return payload


class EvaluateTests(unittest.TestCase):
    def evaluate(self, quota_info, **kw):
        kw.setdefault("allow_reset", True)
        kw.setdefault("work_remaining", 2)
        kw.setdefault("cooldown_active", False)
        return gate.evaluate(quota_info, now_ms=NOW, **kw)

    def test_dispatch_ok_healthy(self):
        v = self.evaluate(qi(remaining=15.4))
        self.assertEqual(v["verdict"], "dispatch_ok")
        self.assertEqual(v["reason"], "quota_available")

    def test_dispatch_ok_low_but_usable(self):
        v = self.evaluate(qi(remaining=3.0))
        self.assertEqual(v["verdict"], "dispatch_ok")
        self.assertEqual(v["reason"], "quota_low_but_usable")

    def test_wait_natural_includes_propagation_buffer(self):
        v = self.evaluate(qi(remaining=0.6, minutes=42))
        self.assertEqual(v["verdict"], "wait_natural")
        self.assertEqual(v["reason"], "natural_reset_within_threshold")
        self.assertEqual(v["wake_at_epoch_ms"], NOW + (42 + 2) * 60_000)

    def test_watch_when_reset_time_unknown(self):
        v = self.evaluate(qi(remaining=0.5, minutes=None, cards=2))
        self.assertEqual(v["verdict"], "watch")
        self.assertEqual(v["reason"], "reset_time_unknown")

    def test_needs_user_when_inventory_unknown(self):
        v = self.evaluate(qi(status="partial", remaining=0.4, minutes=190,
                             cards=None))
        self.assertEqual(v["verdict"], "needs_user")
        self.assertEqual(v["reason"], "inventory_unknown")

    def test_wait_no_cards_wakes_at_reset(self):
        v = self.evaluate(qi(remaining=0.0, minutes=174, cards=0))
        self.assertEqual(v["verdict"], "wait_no_cards")
        self.assertEqual(v["reason"], "no_usable_five_hour_cards")
        self.assertEqual(v["wake_at_epoch_ms"], NOW + (174 + 2) * 60_000)

    def test_redeem_candidate_when_logical(self):
        v = self.evaluate(qi(remaining=0.4, minutes=185, cards=3))
        self.assertEqual(v["verdict"], "redeem_candidate")
        self.assertEqual(v["reason"], "logical_reset_window")

    def test_needs_user_when_policy_disables_reset(self):
        v = self.evaluate(qi(remaining=0.4, minutes=185, cards=3),
                          allow_reset=False)
        self.assertEqual(v["verdict"], "needs_user")
        self.assertEqual(v["reason"], "redemption_disabled_by_policy")

    def test_no_work_remaining_never_spends_a_card(self):
        next_reset = NOW + 185 * 60_000
        v = self.evaluate(qi(remaining=0.4, minutes=185, cards=3,
                             next_reset=next_reset), work_remaining=0)
        self.assertEqual(v["verdict"], "wait_no_cards")
        self.assertEqual(v["reason"], "no_work_remaining_no_reset")
        self.assertEqual(v["wake_at_epoch_ms"],
                         next_reset + gate.RESUME_PROPAGATION_BUFFER_MIN * 60_000)

    def test_cooldown_waits_for_natural_reset(self):
        next_reset = NOW + 90 * 60_000
        v = self.evaluate(qi(remaining=0.4, minutes=90, cards=3,
                             next_reset=next_reset), cooldown_active=True)
        self.assertEqual(v["verdict"], "wait_natural")
        self.assertEqual(v["reason"], "redemption_cooldown_active")
        self.assertEqual(v["wake_at_epoch_ms"],
                         next_reset + gate.RESUME_PROPAGATION_BUFFER_MIN * 60_000)

    def test_needs_user_on_quota_error_status(self):
        v = self.evaluate(qi(status="error", remaining=None, minutes=None,
                             cards=None))
        self.assertEqual(v["verdict"], "needs_user")
        self.assertEqual(v["reason"], "quota_status_error")

    def test_needs_user_when_remaining_unreadable(self):
        v = self.evaluate(qi(remaining=None))
        self.assertEqual(v["verdict"], "needs_user")
        self.assertEqual(v["reason"], "quota_unreadable")

    def test_echo_fields_always_present(self):
        v = self.evaluate(qi(remaining=0.4, minutes=185, cards=3))
        self.assertEqual(v["remaining_percent"], 0.4)
        self.assertEqual(v["minutes"], 185)
        self.assertEqual(v["cards_available"], 3)


class PolicyTests(unittest.TestCase):
    def test_defaults_when_manifest_empty(self):
        policy = gate.load_policy({})
        self.assertTrue(policy["allow_reset"])
        self.assertEqual(policy["dispatch_min_remaining_percent"], 5.0)
        self.assertEqual(policy["max_resume_rechecks"], 8)

    def test_manifest_policy_overrides(self):
        policy = gate.load_policy(
            {"policy": {"allow_reset": False,
                        "dispatch_min_remaining_percent": 8.0}})
        self.assertFalse(policy["allow_reset"])
        self.assertEqual(policy["dispatch_min_remaining_percent"], 8.0)
        self.assertEqual(policy["low_percent"], 1.0)

    def test_custom_dispatch_min_is_honored(self):
        policy = gate.load_policy({"policy": {"dispatch_min_remaining_percent": 8.0}})
        v = gate.evaluate(qi(remaining=9.0), allow_reset=True,
                          work_remaining=2, cooldown_active=False,
                          now_ms=NOW, policy=policy)
        self.assertEqual(v["verdict"], "dispatch_ok")
        v = gate.evaluate(qi(remaining=6.0), allow_reset=True,
                          work_remaining=2, cooldown_active=False,
                          now_ms=NOW, policy=policy)
        self.assertEqual(v["verdict"], "dispatch_ok")
        self.assertEqual(v["reason"], "quota_low_but_usable")


if __name__ == "__main__":
    unittest.main()
