"""Unit tests for payload parsing, minutes rounding and card selection.

Stdlib unittest only; no network. The numbered docstrings are the verbatim test
groups from plans/zai-quota-implementation-plan.md (plus a bracketed aspect
note), so coverage is auditable against the plan.
"""

import pathlib
import sys
import unittest

SCRIPTS_DIR = pathlib.Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import models  # noqa: E402

NOW_MS = 1_700_000_000_000  # 2023-11-14T22:13:20Z, frozen clock


def quota_payload(used=99.5, reset_ms=None):
    return {
        "success": True,
        "code": 200,
        "data": {
            "limits": [
                {
                    "unit": 3,
                    "type": "TOKENS_LIMIT",
                    "percentage": used,
                    "nextResetTime": NOW_MS + 200 * 60_000
                    if reset_ms is None
                    else reset_ms,
                }
            ]
        },
    }


def cards_payload(cards, weeks=None):
    return {
        "success": True,
        "code": 200,
        "data": {"fiveHourResets": cards, "weekResets": weeks or []},
    }


def card(record_id=910002, expire="2099-10-19 09:18:08", available=True, **extra):
    entry = {"recordId": record_id, "available": available, "expireTime": expire}
    entry.update(extra)
    return entry


class QuotaParsingTests(unittest.TestCase):
    def test_used_percentage_and_low_boundary(self):
        """1. Quota `percentage` is used percentage, and 1.00% remaining is treated as low. (parsing aspect)"""
        snap = models.parse_quota_payload(quota_payload(used=96.7), NOW_MS)
        self.assertEqual(snap.used_percent, 96.7)
        self.assertEqual(snap.remaining_percent, 3.3)
        snap = models.parse_quota_payload(quota_payload(used=99.0), NOW_MS)
        self.assertEqual(snap.remaining_percent, 1.0)  # exactly 1.00% remaining

    def test_missing_five_hour_window(self):
        """7. ... missing unit:3 ... -> safe errors, no POST. (parsing aspect)"""
        payload = quota_payload()
        payload["data"]["limits"] = [
            {"unit": 2, "type": "TOKENS_LIMIT", "percentage": 50.0, "nextResetTime": NOW_MS}
        ]
        with self.assertRaises(models.DataError) as ctx:
            models.parse_quota_payload(payload, NOW_MS)
        self.assertEqual(ctx.exception.reason, "missing_five_hour_window")

    def test_missing_limits_list(self):
        """7. ... missing unit:3 ... -> safe errors, no POST. (parsing aspect: absent limits list)"""
        with self.assertRaises(models.DataError) as ctx:
            models.parse_quota_payload({"success": True, "code": 200, "data": {}}, NOW_MS)
        self.assertEqual(ctx.exception.reason, "missing_five_hour_window")

    def test_missing_next_reset_time(self):
        """7. ... missing nextResetTime ... -> safe errors, no POST. (parsing aspect)"""
        payload = quota_payload()
        del payload["data"]["limits"][0]["nextResetTime"]
        with self.assertRaises(models.DataError) as ctx:
            models.parse_quota_payload(payload, NOW_MS)
        self.assertEqual(ctx.exception.reason, "invalid_next_reset_time")

    def test_bad_percentages(self):
        """7. ... bad percentages -> safe errors, no POST. (parsing aspect)"""
        for bad in (150.0, -3.0, "96.7", None, True):
            with self.assertRaises(models.DataError) as ctx:
                models.parse_quota_payload(quota_payload(used=bad), NOW_MS)
            self.assertEqual(ctx.exception.reason, "invalid_percentage")

    def test_weekly_windows_ignored_in_quota(self):
        """4. ... weekly cards ignored. (quota aspect: non-five-hour limits never selected)"""
        payload = quota_payload(used=96.7)
        payload["data"]["limits"].insert(
            0, {"unit": 2, "type": "TOKENS_LIMIT", "percentage": 10.0, "nextResetTime": 1}
        )
        snap = models.parse_quota_payload(payload, NOW_MS)
        self.assertEqual(snap.used_percent, 96.7)


class CardParsingTests(unittest.TestCase):
    def test_earliest_expiry_wins(self):
        """4. Multiple five-hour cards with distinct expiries -> earliest expiry. (selection aspect)"""
        raw = [card(1, "2099-03-01 00:00:00"), card(2, "2098-01-01 00:00:00"), card(3, "2099-01-01 00:00:00")]
        cards, skipped = models.select_cards(cards_payload(raw)["data"]["fiveHourResets"], NOW_MS)
        self.assertEqual(skipped, 0)
        self.assertEqual([c.record_id for c in cards], [2, 3, 1])

    def test_same_expiry_stable_id_ordering(self):
        """4. ... same expiry -> stable ID ordering. ..."""
        raw = [card(910002, "2099-01-01 00:00:00"), card(910001, "2099-01-01 00:00:00")]
        cards, _ = models.select_cards(raw, NOW_MS)
        self.assertEqual([c.record_id for c in cards], [910001, 910002])

    def test_unavailable_and_consumed_excluded(self):
        """4. ... unavailable and expired cards excluded; ... (availability aspect)"""
        raw = [
            card(1, available=False),
            card(2, available=True, consumed=True),
            card(3, available=True, redeemed="yes"),
            card(4, available=None),
            card(5),
        ]
        cards, skipped = models.select_cards(raw, NOW_MS)
        self.assertEqual([c.record_id for c in cards], [5])
        self.assertEqual(skipped, 4)

    def test_expired_cards_excluded(self):
        """4. ... unavailable and expired cards excluded; ... (expiry aspect)"""
        raw = [card(1, "2000-01-01 00:00:00"), card(2, "2099-01-01 00:00:00")]
        cards, skipped = models.select_cards(raw, NOW_MS)
        self.assertEqual([c.record_id for c in cards], [2])
        self.assertEqual(skipped, 1)

    def test_weekly_cards_ignored(self):
        """4. ... weekly cards ignored."""
        payload = cards_payload([card(1)], weeks=[{"recordId": 77, "available": True, "expireTime": "2099-01-01 00:00:00"}])
        cards, _ = models.select_cards(payload["data"]["fiveHourResets"], NOW_MS)
        self.assertEqual([c.record_id for c in cards], [1])
        self.assertNotIn(77, [c.record_id for c in cards])

    def test_malformed_entries_skipped_not_fatal(self):
        """4. ... Validate all fields before selecting ... (malformed-entry aspect)"""
        raw = ["junk", 42, {"recordId": "x", "available": True, "expireTime": "2099-01-01"}, card(9)]
        cards, skipped = models.select_cards(raw, NOW_MS)
        self.assertEqual([c.record_id for c in cards], [9])
        self.assertEqual(skipped, 3)

    def test_omitted_five_hour_resets_is_error_not_empty(self):
        """6. Inventory GET fails or response omits `fiveHourResets` -> error for `reset_please`; `partial` for `quota_info`. Never treat failure as zero cards. (parsing aspect)"""
        for payload in (
            {"success": True, "code": 200, "data": {}},
            {"success": True, "code": 200, "data": {"fiveHourResets": "nope"}},
        ):
            with self.assertRaises(models.DataError) as ctx:
                models.parse_cards_payload(payload)
            self.assertEqual(ctx.exception.reason, "missing_five_hour_resets")

    def test_present_empty_list_is_genuinely_empty(self):
        """5. Zero available five-hour cards (confirmed successful inventory GET) -> `no_resets_available`. (parsing aspect)"""
        raw = models.parse_cards_payload(cards_payload([]))
        cards, skipped = models.select_cards(raw, NOW_MS)
        self.assertEqual((cards, skipped), ([], 0))


class MinutesAndTimeTests(unittest.TestCase):
    def test_minutes_ceiling_rounding(self):
        """12. Deadline rounding, UTC epoch milliseconds, Naive expiry timestamps, clock boundary, and `minutes` ceiling behavior. (rounding aspect)"""
        self.assertEqual(models.minutes_until(NOW_MS, NOW_MS + 84 * 60_000), 84)
        self.assertEqual(models.minutes_until(NOW_MS, NOW_MS + 84 * 60_000 - 1), 84)
        self.assertEqual(models.minutes_until(NOW_MS, NOW_MS + 83 * 60_000 + 1), 84)
        self.assertEqual(models.minutes_until(NOW_MS, NOW_MS + 83 * 60_000), 83)

    def test_clock_boundary_due_reset_is_zero(self):
        """12. Deadline rounding, UTC epoch milliseconds, Naive expiry timestamps, clock boundary, and `minutes` ceiling behavior. (boundary aspect)"""
        self.assertEqual(models.minutes_until(NOW_MS, NOW_MS), 0)
        self.assertEqual(models.minutes_until(NOW_MS, NOW_MS - 1), 0)
        snap = models.parse_quota_payload(quota_payload(reset_ms=NOW_MS), NOW_MS)
        self.assertEqual(snap.minutes_to_reset, 0)

    def test_utc_epoch_ms_iso_format(self):
        """12. Deadline rounding, UTC epoch milliseconds, Naive expiry timestamps, clock boundary, and `minutes` ceiling behavior. (UTC aspect)"""
        self.assertEqual(models.iso_utc(0), "1970-01-01T00:00:00Z")
        self.assertEqual(models.iso_utc(NOW_MS), "2023-11-14T22:13:20Z")

    def test_naive_expiry_strings_sort_chronologically(self):
        """12. Deadline rounding, UTC epoch milliseconds, Naive expiry timestamps, clock boundary, and `minutes` ceiling behavior. (naive-string aspect)"""
        raw = [card(1, "2099-01-02 00:00:00"), card(2, "2099-01-01 23:59:59"), card(3, "2099-01-01 00:00:00")]
        cards, _ = models.select_cards(raw, NOW_MS)
        self.assertEqual([c.record_id for c in cards], [3, 2, 1])

    def test_utc_naive_string_matches_expiry_shape(self):
        """12. Deadline rounding, UTC epoch milliseconds, Naive expiry timestamps, clock boundary, and `minutes` ceiling behavior. (shape aspect)"""
        self.assertEqual(models.utc_naive_string(NOW_MS), "2023-11-14 22:13:20")


if __name__ == "__main__":
    unittest.main()
