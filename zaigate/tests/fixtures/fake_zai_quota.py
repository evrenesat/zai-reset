#!/usr/bin/env python3
"""Offline stub of the zai-quota CLI for coordinator tests and selftest.

Environment:
  ZR_FAKE_STATE       path to a JSON file {"mode": "<mode>"}
  ZR_FAKE_SUBCOMMAND  quota_info (default) | reset_please

Prints exactly one schema_version 1 JSON line per run, mirroring the zai-quota
contract for the situations the coordinator gates on. Zero network. Modes:

  quota_info: ok | low_near_reset | low_far_cards | low_far_nocards |
              low_null_minutes | inventory_unknown (partial) | api_error
  reset_please: reset_success | reset_uncertain | reset_error
                (quota_info under a reset_* mode behaves like low_far_cards)
"""

import json
import os
import sys
import time


def _now_ms():
    return int(time.time() * 1000)


def _quota_info(mode):
    now = _now_ms()

    def info(remaining, minutes, cards, status="ok"):
        payload = {
            "schema_version": 1,
            "status": status,
            "minutes": minutes,
            "reason": "quota_and_cards_available",
            "quota_used_percent": round(100 - remaining, 1),
            "quota_remaining_percent": remaining,
            "cards_available": cards,
        }
        if minutes is not None:
            payload["next_reset_at"] = "fake"
            payload["next_reset_at_epoch_ms"] = now + minutes * 60_000
        return payload

    modes = {
        "ok": lambda: info(15.4, 212, 5),
        "low_near_reset": lambda: info(0.6, 42, 5),
        "low_far_cards": lambda: info(0.4, 185, 3),
        "low_far_nocards": lambda: info(0.0, 174, 0),
        "low_null_minutes": lambda: info(0.5, None, 2),
        "inventory_unknown": lambda: info(0.4, 190, None, status="partial"),
        "api_error": lambda: {"schema_version": 1, "status": "error",
                              "minutes": None,
                              "reason": "quota_api_failed"},
    }
    builder = modes.get(mode)
    if builder is None:
        return {"schema_version": 1, "status": "error", "minutes": None,
                "reason": f"fake_mode_unknown_{mode}"}
    return builder()


def _reset_please(mode):
    now = _now_ms()
    if mode == "reset_success":
        return {"schema_version": 1, "status": "success", "minutes": 300,
                "reason": "reset_card_redeemed", "card_id": 910002,
                "next_reset_at_epoch_ms": now + 300 * 60_000}
    if mode == "reset_uncertain":
        return {"schema_version": 1, "status": "uncertain", "minutes": 185,
                "reason": "reset_response_lost", "card_id": 910002}
    if mode == "reset_error":
        return {"schema_version": 1, "status": "error", "minutes": 185,
                "reason": "provider_rejected"}
    # Any other mode: refuse to "POST" like the real CLI would on bad input.
    return {"schema_version": 1, "status": "error", "minutes": None,
            "reason": f"fake_mode_unknown_{mode}"}


def main():
    subcommand = os.environ.get("ZR_FAKE_SUBCOMMAND", "quota_info")
    state_path = os.environ.get("ZR_FAKE_STATE")
    mode = "ok"
    if state_path and os.path.isfile(state_path):
        try:
            with open(state_path, encoding="utf-8") as f:
                mode = (json.load(f) or {}).get("mode", "ok")
        except (OSError, ValueError):
            mode = "ok"
    if subcommand == "reset_please":
        resp = _reset_please(mode)
    else:
        resp = _quota_info(mode if not mode.startswith("reset_")
                           else "low_far_cards")
    sys.stdout.write(json.dumps(resp) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
