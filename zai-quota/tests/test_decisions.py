"""Unit tests for the pure threshold decision functions.

Stdlib unittest only, no I/O. Numbered docstrings are the verbatim test groups
from plans/zai-quota-implementation-plan.md (plus a bracketed aspect note).
"""

import pathlib
import sys
import unittest

SCRIPTS_DIR = pathlib.Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import decisions  # noqa: E402


class ThresholdDecisionTests(unittest.TestCase):
    def test_exactly_one_percent_remaining_is_low(self):
        """1. Quota `percentage` is used percentage, and 1.00% remaining is treated as low. (decision aspect)"""
        verdict = decisions.decide(1.0, 200)
        self.assertNotEqual(verdict["status"], "continue")
        self.assertEqual(verdict["status"], "redeem_candidate")  # 200 min away, past wait window

    def test_remaining_threshold_boundaries(self):
        """2. Remaining 1.01% -> `continue`; exactly 1% -> decision proceeds; 0% -> low."""
        self.assertEqual(decisions.decide(1.01, 500)["status"], "continue")
        self.assertEqual(decisions.decide(1.01, 500)["reason"], "quota_available")
        # exactly 1%: the decision proceeds past `continue` into the low branches
        proceed = decisions.decide(1.0, 200)
        self.assertNotEqual(proceed["status"], "continue")
        low = decisions.decide(0.0, 200)
        self.assertNotEqual(low["status"], "continue")
        self.assertEqual(low["status"], "redeem_candidate")  # 200 min away, past wait window

    def test_natural_reset_wait_boundaries(self):
        """3. Natural reset at 59, 60, 61 minutes -> `wait`, `wait`, redemption candidate respectively."""
        self.assertEqual(decisions.decide(0.5, 59)["status"], "wait")
        self.assertEqual(decisions.decide(0.5, 60)["status"], "wait")
        self.assertEqual(decisions.decide(0.5, 60)["reason"], "natural_reset_within_threshold")
        candidate = decisions.decide(0.5, 61)
        self.assertEqual(candidate["status"], "redeem_candidate")

    def test_due_reset_is_wait_with_stale_boundary(self):
        """12. Deadline rounding, UTC epoch milliseconds, Naive expiry timestamps, clock boundary, and `minutes` ceiling behavior. (stale-boundary decision aspect)"""
        verdict = decisions.decide(0.0, 0)
        self.assertEqual(verdict["status"], "wait")
        self.assertEqual(verdict["reason"], "stale_at_boundary")
        self.assertEqual(verdict["minutes"], 1)
        verdict = decisions.decide(0.5, -5)
        self.assertEqual((verdict["status"], verdict["minutes"]), ("wait", 1))

    def test_unknown_reset_time_with_low_quota(self):
        """3. Natural reset at 59, 60, 61 minutes -> `wait`, `wait`, redemption candidate respectively. (unknown-time aspect: caller must consult inventory)"""
        verdict = decisions.decide(0.5, None)
        self.assertEqual(verdict["status"], "redeem_candidate")
        self.assertEqual(verdict["reason"], "low_quota_reset_time_unknown")


class CardsDecisionTests(unittest.TestCase):
    def test_zero_cards_is_no_resets_available(self):
        """5. Zero available five-hour cards (confirmed successful inventory GET) -> `no_resets_available`."""
        verdict = decisions.decide_with_cards(0, 174)
        self.assertEqual(verdict["status"], "no_resets_available")
        self.assertEqual(verdict["reason"], "no_usable_five_hour_cards")
        self.assertEqual(verdict["minutes"], 174)

    def test_cards_present_is_redeem_candidate(self):
        """5. Zero available five-hour cards (confirmed successful inventory GET) -> `no_resets_available`. (cards-present aspect)"""
        verdict = decisions.decide_with_cards(3, 174)
        self.assertEqual(verdict["status"], "redeem_candidate")
        self.assertEqual(verdict["reason"], "usable_card_available")

    def test_unknown_inventory_count_is_error(self):
        """6. Inventory GET fails or response omits `fiveHourResets` -> error for `reset_please`; `partial` for `quota_info`. Never treat failure as zero cards. (decision aspect)"""
        verdict = decisions.decide_with_cards(None, 174)
        self.assertEqual(verdict["status"], "error")
        self.assertNotEqual(verdict["reason"], "no_usable_five_hour_cards")


if __name__ == "__main__":
    unittest.main()
