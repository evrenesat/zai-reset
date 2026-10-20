"""End-to-end redemption tests for reset_please with a mocked transport and
frozen clock. Zero network; the stub transport raises on any unexpected request
so a stray POST fails the test loudly.

Numbered docstrings are the verbatim test groups from
plans/zai-quota-implementation-plan.md (plus a bracketed aspect note); tests
named addendum_* cover plans/quota-and-delegation-20261019.md section A2.
"""

import fcntl
import json
import os
import pathlib
import sys
import tempfile
import threading
import time
import unittest

SCRIPTS_DIR = pathlib.Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import models  # noqa: E402
import zai_api  # noqa: E402
import zai_quota  # noqa: E402

NOW_MS = 1_700_000_000_000  # 2023-11-14T22:13:20Z, frozen clock
API_KEY = "test-key-redemption"


def clock():
    return NOW_MS


def quota_payload(used=99.5, minutes_ahead=200):
    return {
        "success": True,
        "code": 200,
        "data": {
            "limits": [
                {
                    "unit": 3,
                    "type": "TOKENS_LIMIT",
                    "percentage": used,
                    "nextResetTime": NOW_MS + minutes_ahead * 60_000,
                }
            ]
        },
    }


def cards_payload(cards):
    return {"success": True, "code": 200, "data": {"fiveHourResets": cards, "weekResets": []}}


def card(record_id=910002, expire="2099-10-19 09:18:08", available=True, **extra):
    entry = {"recordId": record_id, "available": available, "expireTime": expire}
    entry.update(extra)
    return entry


def post_success():
    return {"success": True, "code": 200, "msg": "ok"}


class FakeTransport:
    """Queued offline transport. Each URL has a FIFO queue of either
    ("resp", status, body-bytes) or ("raise", exception). An unexpected request
    raises AssertionError so tests fail on any stray call."""

    def __init__(self):
        self.queues = {}
        self.requests = []
        self.post_count = 0

    def queue_json(self, url, status, payload):
        self.queues.setdefault(url, []).append(("resp", status, json.dumps(payload).encode("utf-8")))

    def queue_raw(self, url, status, body):
        self.queues.setdefault(url, []).append(("resp", status, body))

    def queue_raise(self, url, exc):
        self.queues.setdefault(url, []).append(("raise", exc))

    def __call__(self, request):
        url = request.full_url
        self.requests.append((request.get_method(), url))
        if request.get_method() == "POST":
            self.post_count += 1
        queue = self.queues.get(url) or []
        if not queue:
            raise AssertionError("unexpected request: %s %s" % (request.get_method(), url))
        item = queue.pop(0)
        if item[0] == "raise":
            raise item[1]
        return item[1], item[2]

    def unused(self, url):
        return len(self.queues.get(url, []))


def temp_state_dir():
    return tempfile.mkdtemp(prefix="zai-quota-test-")


def seed_success(state_dir, reset_ms, card_id=910001, now_ms=NOW_MS):
    return zai_quota.append_ledger(
        state_dir,
        action="redeem",
        card_id=card_id,
        request_id="req-seeded",
        result="success",
        next_reset_at_epoch_ms=reset_ms,
        ts=models.iso_utc(now_ms),
    )


class RedemptionFlowTests(unittest.TestCase):
    def test_zero_available_cards_no_resets_available(self):
        """5. Zero available five-hour cards (confirmed successful inventory GET) -> `no_resets_available`."""
        state_dir = temp_state_dir()
        transport = FakeTransport()
        transport.queue_json(zai_api.QUOTA_URL, 200, quota_payload(used=100.0))
        transport.queue_json(zai_api.CARDS_URL, 200, cards_payload([]))
        resp = zai_quota.run_reset_please(
            API_KEY, state_dir=state_dir, transport=transport, now_ms=clock
        )
        self.assertEqual(resp["status"], "no_resets_available")
        self.assertEqual(resp["reason"], "no_usable_five_hour_cards")
        self.assertEqual(resp["minutes"], 200)
        self.assertEqual(transport.post_count, 0)
        self.assertIsNone(zai_quota.read_pending(state_dir))

    def test_inventory_failure_is_error_for_reset_and_partial_for_quota_info(self):
        """6. Inventory GET fails or response omits `fiveHourResets` -> error for `reset_please`; `partial` for `quota_info`. Never treat failure as zero cards."""
        for mode in ("http_500", "omitted_key", "timeout"):
            state_dir = temp_state_dir()
            transport = FakeTransport()
            transport.queue_json(zai_api.QUOTA_URL, 200, quota_payload())
            if mode == "http_500":
                transport.queue_raw(zai_api.CARDS_URL, 500, b"boom")
            elif mode == "omitted_key":
                transport.queue_json(zai_api.CARDS_URL, 200, {"success": True, "code": 200, "data": {}})
            else:
                transport.queue_raise(zai_api.CARDS_URL, TimeoutError())
            resp = zai_quota.run_reset_please(
                API_KEY, state_dir=state_dir, transport=transport, now_ms=clock
            )
            self.assertEqual(resp["status"], "error", mode)
            self.assertEqual(resp["reason"], "cards_api_failed", mode)
            self.assertEqual(transport.post_count, 0, mode)
            self.assertIsNone(zai_quota.read_pending(state_dir), mode)

            # Same stubs through quota_info: partial, cards unknown (not zero).
            transport2 = FakeTransport()
            transport2.queue_json(zai_api.QUOTA_URL, 200, quota_payload())
            if mode == "http_500":
                transport2.queue_raw(zai_api.CARDS_URL, 500, b"boom")
            elif mode == "omitted_key":
                transport2.queue_json(zai_api.CARDS_URL, 200, {"success": True, "code": 200, "data": {}})
            else:
                transport2.queue_raise(zai_api.CARDS_URL, TimeoutError())
            info = zai_quota.run_quota_info(API_KEY, transport=transport2, now_ms=clock)
            self.assertEqual(info["status"], "partial", mode)
            self.assertIsNone(info["cards_available"], mode)
            self.assertNotEqual(info.get("cards_available"), 0, mode)

    def test_quota_read_failures_are_safe_errors_without_post(self):
        """7. Non-200 HTTP, HTTP 200 with `success:false`, malformed JSON, missing `unit:3`, missing `nextResetTime`, bad percentages -> safe errors, no POST."""
        cases = {
            "http_500": lambda t: t.queue_raw(zai_api.QUOTA_URL, 500, b"oops"),
            "http_403": lambda t: t.queue_raw(zai_api.QUOTA_URL, 403, b'{"success":false,"code":403,"msg":"forbidden"}'),
            "success_false": lambda t: t.queue_json(
                zai_api.QUOTA_URL, 200, {"success": False, "code": 200, "msg": "denied"}
            ),
            "malformed_json": lambda t: t.queue_raw(zai_api.QUOTA_URL, 200, b"{not json"),
            "missing_unit3": lambda t: t.queue_json(
                zai_api.QUOTA_URL, 200, {"success": True, "code": 200, "data": {"limits": []}}
            ),
            "missing_next_reset": lambda t: t.queue_json(
                zai_api.QUOTA_URL,
                200,
                {"success": True, "code": 200, "data": {"limits": [{"unit": 3, "percentage": 99.0}]}},
            ),
            "bad_percentage": lambda t: t.queue_json(
                zai_api.QUOTA_URL,
                200,
                {
                    "success": True,
                    "code": 200,
                    "data": {
                        "limits": [
                            {"unit": 3, "percentage": 150.0, "nextResetTime": NOW_MS + 60_000}
                        ]
                    },
                },
            ),
            "timeout": lambda t: t.queue_raise(zai_api.QUOTA_URL, TimeoutError()),
        }
        for name, setup in cases.items():
            state_dir = temp_state_dir()
            transport = FakeTransport()
            setup(transport)
            resp = zai_quota.run_reset_please(
                API_KEY, state_dir=state_dir, transport=transport, now_ms=clock
            )
            self.assertEqual(resp["status"], "error", name)
            self.assertEqual(resp["reason"], "quota_api_failed", name)
            self.assertIsNone(resp["minutes"], name)
            self.assertEqual(transport.post_count, 0, name)
            self.assertEqual(transport.unused(zai_api.CARDS_URL), 0, name)  # never reached inventory
            self.assertIsNone(zai_quota.read_pending(state_dir), name)

    def test_positive_post_acknowledgment_is_success_with_verification(self):
        """8. Positive POST business acknowledgment -> `success`; HTTP 200 with failed business envelope -> `error`."""
        # Success path: low quota -> two cards -> refetch low -> POST ok -> verify.
        state_dir = temp_state_dir()
        transport = FakeTransport()
        transport.queue_json(zai_api.QUOTA_URL, 200, quota_payload(used=99.5, minutes_ahead=300))
        transport.queue_json(zai_api.CARDS_URL, 200, cards_payload([card(910002), card(910001, "2098-01-01 00:00:00")]))
        transport.queue_json(zai_api.QUOTA_URL, 200, quota_payload(used=99.5, minutes_ahead=300))
        transport.queue_json(zai_api.RESET_URL, 200, post_success())
        transport.queue_json(zai_api.QUOTA_URL, 200, quota_payload(used=10.0, minutes_ahead=290))
        transport.queue_json(zai_api.CARDS_URL, 200, cards_payload([card(910002)]))
        resp = zai_quota.run_reset_please(
            API_KEY, state_dir=state_dir, transport=transport, now_ms=clock
        )
        self.assertEqual(resp["status"], "success")
        self.assertEqual(resp["reason"], "reset_card_redeemed")
        self.assertEqual(resp["card_id"], 910001)  # earliest expiry wins
        self.assertEqual(resp["minutes"], 290)
        self.assertFalse(resp["verification_pending"])
        self.assertAlmostEqual(resp["quota_remaining_percent"], 90.0)
        self.assertEqual(transport.post_count, 1)
        self.assertIsNone(zai_quota.read_pending(state_dir))
        entries = zai_quota.read_ledger(state_dir)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["result"], "success")
        self.assertEqual(entries[0]["card_id"], 910001)
        self.assertEqual(entries[0]["next_reset_at_epoch_ms"], NOW_MS + 300 * 60_000)
        for key in ("ts", "action", "card_id", "request_id", "result", "next_reset_at_epoch_ms"):
            self.assertIn(key, entries[0])

        # Business rejection: HTTP 200 with success:false -> error, no retry.
        state_dir = temp_state_dir()
        transport = FakeTransport()
        transport.queue_json(zai_api.QUOTA_URL, 200, quota_payload(used=99.5, minutes_ahead=300))
        transport.queue_json(zai_api.CARDS_URL, 200, cards_payload([card(910003)]))
        transport.queue_json(zai_api.QUOTA_URL, 200, quota_payload(used=99.5, minutes_ahead=300))
        transport.queue_json(
            zai_api.RESET_URL, 200, {"success": False, "code": 500, "msg": "card already used"}
        )
        resp = zai_quota.run_reset_please(
            API_KEY, state_dir=state_dir, transport=transport, now_ms=clock
        )
        self.assertEqual(resp["status"], "error")
        self.assertEqual(resp["reason"], "provider_rejected")
        self.assertEqual(resp["provider_message"], "card already used")
        self.assertEqual(transport.post_count, 1)
        self.assertIsNone(zai_quota.read_pending(state_dir))
        entries = zai_quota.read_ledger(state_dir)
        self.assertEqual([e["result"] for e in entries], ["error"])

    def test_dropped_post_response_is_uncertain_and_never_retransmitted(self):
        """9. Dropped POST response -> `uncertain`, saved request ID, no automatic duplicate POST on rerun."""
        state_dir = temp_state_dir()
        transport = FakeTransport()
        transport.queue_json(zai_api.QUOTA_URL, 200, quota_payload(used=99.5, minutes_ahead=300))
        transport.queue_json(zai_api.CARDS_URL, 200, cards_payload([card(910004)]))
        transport.queue_json(zai_api.QUOTA_URL, 200, quota_payload(used=99.5, minutes_ahead=300))
        transport.queue_raise(zai_api.RESET_URL, TimeoutError())
        resp = zai_quota.run_reset_please(
            API_KEY, state_dir=state_dir, transport=transport, now_ms=clock
        )
        self.assertEqual(resp["status"], "uncertain")
        self.assertEqual(resp["reason"], "reset_response_lost")
        self.assertEqual(resp["card_id"], 910004)
        pending = zai_quota.read_pending(state_dir)
        self.assertIsNotNone(pending)
        self.assertEqual(pending["card_id"], 910004)
        self.assertEqual(pending["request_id"], resp["request_id"])
        self.assertEqual([e["result"] for e in zai_quota.read_ledger(state_dir)], ["uncertain"])

        # Rerun 1: quota recovered -> definitively confirmed -> success, zero POSTs.
        transport2 = FakeTransport()
        transport2.queue_json(zai_api.QUOTA_URL, 200, quota_payload(used=5.0, minutes_ahead=290))
        resp2 = zai_quota.run_reset_please(
            API_KEY, state_dir=state_dir, transport=transport2, now_ms=clock
        )
        self.assertEqual(resp2["status"], "success")
        self.assertEqual(resp2["confirmed_from"], "quota_recovered")
        self.assertEqual(transport2.post_count, 0)
        self.assertIsNone(zai_quota.read_pending(state_dir))

        # Rerun 2 (fresh state): reads unchanged -> still uncertain, zero POSTs.
        state_dir2 = temp_state_dir()
        transport3 = FakeTransport()
        transport3.queue_json(zai_api.QUOTA_URL, 200, quota_payload(used=99.5, minutes_ahead=300))
        transport3.queue_json(zai_api.CARDS_URL, 200, cards_payload([card(910004)]))
        transport3.queue_json(zai_api.QUOTA_URL, 200, quota_payload(used=99.5, minutes_ahead=300))
        transport3.queue_raise(zai_api.RESET_URL, TimeoutError())
        first = zai_quota.run_reset_please(
            API_KEY, state_dir=state_dir2, transport=transport3, now_ms=clock
        )
        self.assertEqual(first["status"], "uncertain")
        transport4 = FakeTransport()
        transport4.queue_json(zai_api.QUOTA_URL, 200, quota_payload(used=99.5, minutes_ahead=300))
        transport4.queue_json(zai_api.CARDS_URL, 200, cards_payload([card(910004)]))
        resp4 = zai_quota.run_reset_please(
            API_KEY, state_dir=state_dir2, transport=transport4, now_ms=clock
        )
        self.assertEqual(resp4["status"], "uncertain")
        self.assertEqual(resp4["reason"], "pending_reset_unverified")
        self.assertEqual(resp4["card_id"], 910004)
        self.assertEqual(transport3.post_count + transport4.post_count, 1)

    def test_two_concurrent_invocations_single_post(self):
        """10. Two concurrent `reset_please` invocations -> at most one POST and both see safe status."""
        state_dir = temp_state_dir()
        transport = FakeTransport()
        transport.queue_json(zai_api.QUOTA_URL, 200, quota_payload(used=99.5, minutes_ahead=300))
        transport.queue_json(zai_api.CARDS_URL, 200, cards_payload([card(910005)]))
        transport.queue_json(zai_api.QUOTA_URL, 200, quota_payload(used=99.5, minutes_ahead=300))
        transport.queue_json(zai_api.RESET_URL, 200, post_success())
        transport.queue_json(zai_api.QUOTA_URL, 200, quota_payload(used=10.0, minutes_ahead=290))
        transport.queue_json(zai_api.CARDS_URL, 200, cards_payload([]))
        lock_path = os.path.join(state_dir, "lock")
        holder = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        fcntl.flock(holder, fcntl.LOCK_EX)
        results = {}

        def worker():
            results["resp"] = zai_quota.run_reset_please(
                API_KEY, state_dir=state_dir, transport=transport, now_ms=clock
            )

        thread = threading.Thread(target=worker)
        thread.start()
        time.sleep(0.3)
        # While the first invocation holds the lock, the second has made zero requests.
        self.assertEqual(transport.requests, [])
        fcntl.flock(holder, fcntl.LOCK_UN)
        os.close(holder)
        thread.join(timeout=20)
        self.assertFalse(thread.is_alive())
        self.assertEqual(results["resp"]["status"], "success")
        self.assertEqual(transport.post_count, 1)
        # Second invocation now sees the success ledger entry: safe error, no POST.
        transport2 = FakeTransport()
        transport2.queue_json(zai_api.QUOTA_URL, 200, quota_payload(used=99.6, minutes_ahead=300))
        second = zai_quota.run_reset_please(
            API_KEY, state_dir=state_dir, transport=transport2, now_ms=clock
        )
        self.assertEqual(second["status"], "error")
        self.assertEqual(second["reason"], "redemption_already_used_this_window")
        self.assertEqual(transport.post_count + transport2.post_count, 1)

    def test_quota_recovery_before_post_and_delayed_verification(self):
        """11. Quota recovers before POST -> no card spent. After confirmed POST, read-only verification can be delayed and represented explicitly."""
        # Recovery before POST: refetched quota is healthy -> continue, zero POSTs.
        state_dir = temp_state_dir()
        transport = FakeTransport()
        transport.queue_json(zai_api.QUOTA_URL, 200, quota_payload(used=99.5, minutes_ahead=300))
        transport.queue_json(zai_api.CARDS_URL, 200, cards_payload([card(910006)]))
        transport.queue_json(zai_api.QUOTA_URL, 200, quota_payload(used=50.0, minutes_ahead=300))
        resp = zai_quota.run_reset_please(
            API_KEY, state_dir=state_dir, transport=transport, now_ms=clock
        )
        self.assertEqual(resp["status"], "continue")
        self.assertEqual(resp["reason"], "quota_available")
        self.assertEqual(transport.post_count, 0)
        self.assertIsNone(zai_quota.read_pending(state_dir))

        # Acknowledged POST but verification reads still low -> success + verification_pending.
        state_dir2 = temp_state_dir()
        transport2 = FakeTransport()
        transport2.queue_json(zai_api.QUOTA_URL, 200, quota_payload(used=99.5, minutes_ahead=300))
        transport2.queue_json(zai_api.CARDS_URL, 200, cards_payload([card(910007)]))
        transport2.queue_json(zai_api.QUOTA_URL, 200, quota_payload(used=99.5, minutes_ahead=300))
        transport2.queue_json(zai_api.RESET_URL, 200, post_success())
        transport2.queue_json(zai_api.QUOTA_URL, 200, quota_payload(used=99.5, minutes_ahead=295))
        transport2.queue_json(zai_api.CARDS_URL, 200, cards_payload([card(910007)]))
        resp2 = zai_quota.run_reset_please(
            API_KEY, state_dir=state_dir2, transport=transport2, now_ms=clock
        )
        self.assertEqual(resp2["status"], "success")
        self.assertTrue(resp2["verification_pending"])
        self.assertTrue(resp2["warnings"])
        self.assertIsNone(zai_quota.read_pending(state_dir2))

        # Verification quota GET failing entirely -> success with minutes null.
        state_dir3 = temp_state_dir()
        transport3 = FakeTransport()
        transport3.queue_json(zai_api.QUOTA_URL, 200, quota_payload(used=99.5, minutes_ahead=300))
        transport3.queue_json(zai_api.CARDS_URL, 200, cards_payload([card(910008)]))
        transport3.queue_json(zai_api.QUOTA_URL, 200, quota_payload(used=99.5, minutes_ahead=300))
        transport3.queue_json(zai_api.RESET_URL, 200, post_success())
        transport3.queue_raw(zai_api.QUOTA_URL, 500, b"boom")
        transport3.queue_raw(zai_api.CARDS_URL, 500, b"boom")
        resp3 = zai_quota.run_reset_please(
            API_KEY, state_dir=state_dir3, transport=transport3, now_ms=clock
        )
        self.assertEqual(resp3["status"], "success")
        self.assertIsNone(resp3["minutes"])
        self.assertTrue(resp3["verification_pending"])


class LedgerGuardTests(unittest.TestCase):
    def test_ledger_success_blocks_within_same_natural_window(self):
        """addendum: ONE_REDEMPTION_PER_NATURAL_WINDOW blocks a second POST while now < reset + 120s."""
        state_dir = temp_state_dir()
        seed_success(state_dir, NOW_MS + 60 * 60_000)  # window ends in 60 min
        transport = FakeTransport()
        transport.queue_json(zai_api.QUOTA_URL, 200, quota_payload(used=99.5, minutes_ahead=300))
        # One queued inventory response must remain unconsumed: the block must
        # happen before any card inventory GET.
        transport.queue_json(zai_api.CARDS_URL, 200, cards_payload([card(910009)]))
        resp = zai_quota.run_reset_please(
            API_KEY, state_dir=state_dir, transport=transport, now_ms=clock
        )
        self.assertEqual(resp["status"], "error")
        self.assertEqual(resp["reason"], "redemption_already_used_this_window")
        self.assertEqual(transport.post_count, 0)
        self.assertEqual(transport.unused(zai_api.CARDS_URL), 1)  # inventory never consulted

    def test_ledger_margin_boundaries(self):
        """addendum: the 120s propagation margin is honored exactly (block at <margin, allow at margin)."""
        # Exactly at reset+120s: no longer blocked (proceeds to inventory; empty -> no_resets_available).
        state_dir = temp_state_dir()
        seed_success(state_dir, NOW_MS - 120_000)
        transport = FakeTransport()
        transport.queue_json(zai_api.QUOTA_URL, 200, quota_payload(used=99.5, minutes_ahead=300))
        transport.queue_json(zai_api.CARDS_URL, 200, cards_payload([]))
        resp = zai_quota.run_reset_please(
            API_KEY, state_dir=state_dir, transport=transport, now_ms=clock
        )
        self.assertEqual(resp["status"], "no_resets_available")

        # One millisecond inside the margin: blocked.
        state_dir2 = temp_state_dir()
        seed_success(state_dir2, NOW_MS - 119_999)
        transport2 = FakeTransport()
        transport2.queue_json(zai_api.QUOTA_URL, 200, quota_payload(used=99.5, minutes_ahead=300))
        resp2 = zai_quota.run_reset_please(
            API_KEY, state_dir=state_dir2, transport=transport2, now_ms=clock
        )
        self.assertEqual(resp2["reason"], "redemption_already_used_this_window")
        self.assertEqual(transport2.post_count, 0)

    def test_ledger_uncertain_entries_do_not_block(self):
        """addendum: only `result == "success"` blocks; uncertain entries keep pending but allow resolution."""
        state_dir = temp_state_dir()
        zai_quota.append_ledger(
            state_dir,
            action="redeem",
            card_id=910009,
            request_id="req-u",
            result="uncertain",
            next_reset_at_epoch_ms=NOW_MS + 60 * 60_000,
            ts=models.iso_utc(NOW_MS),
        )
        # Uncertain entry alone: quota healthy -> plain continue (no POST anywhere).
        transport = FakeTransport()
        transport.queue_json(zai_api.QUOTA_URL, 200, quota_payload(used=15.0, minutes_ahead=300))
        resp = zai_quota.run_reset_please(
            API_KEY, state_dir=state_dir, transport=transport, now_ms=clock
        )
        self.assertEqual(resp["status"], "continue")
        self.assertEqual(transport.post_count, 0)

    def test_ledger_success_with_unknown_window_blocks_conservatively(self):
        """addendum: a success entry with no evaluable window is blocked, never risked."""
        state_dir = temp_state_dir()
        zai_quota.append_ledger(
            state_dir,
            action="redeem",
            card_id=910010,
            request_id="req-x",
            result="success",
            next_reset_at_epoch_ms=None,
            ts=models.iso_utc(NOW_MS),
        )
        transport = FakeTransport()
        transport.queue_json(zai_api.QUOTA_URL, 200, quota_payload(used=99.5, minutes_ahead=300))
        resp = zai_quota.run_reset_please(
            API_KEY, state_dir=state_dir, transport=transport, now_ms=clock
        )
        self.assertEqual(resp["reason"], "redemption_already_used_this_window")
        self.assertEqual(transport.post_count, 0)

    def test_post_outcomes_reach_the_ledger_once(self):
        """addendum: every outcome that reached a POST decision appends exactly one ledger line."""
        state_dir = temp_state_dir()
        transport = FakeTransport()
        transport.queue_json(zai_api.QUOTA_URL, 200, quota_payload(used=99.5, minutes_ahead=300))
        transport.queue_json(zai_api.CARDS_URL, 200, cards_payload([card(910011)]))
        transport.queue_json(zai_api.QUOTA_URL, 200, quota_payload(used=99.5, minutes_ahead=300))
        transport.queue_raise(zai_api.RESET_URL, ConnectionError("reset"))
        zai_quota.run_reset_please(API_KEY, state_dir=state_dir, transport=transport, now_ms=clock)
        entries = zai_quota.read_ledger(state_dir)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["result"], "uncertain")
        self.assertEqual(entries[0]["action"], "redeem")


if __name__ == "__main__":
    unittest.main()
