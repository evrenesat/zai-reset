"""Pure threshold decisions for the five-hour quota reset helper.

These functions perform no I/O: the caller supplies the quota snapshot values
and (where relevant) the number of usable cards, and gets back a verdict dict
with ``status``, ``reason`` and ``minutes``. Statuses mirror the CLI contract:

- ``continue``            remaining quota above the low threshold
- ``wait``                low quota but the natural reset is within the wait window
- ``no_resets_available`` low quota, past the wait window, zero usable cards
- ``redeem_candidate``    low quota, past the wait window, a usable card exists
                          (internal verdict; the CLI performs the POST)

Threshold defaults mirror the configurable constants in ``zai_quota.py``;
callers pass the live values explicitly so this module stays dependency-free.
"""

from __future__ import annotations

DEFAULT_MIN_REMAINING_PERCENT = 1.0
DEFAULT_WAIT_WITHIN_MINUTES = 60


def decide(
    remaining_percent,
    minutes_to_reset,
    *,
    min_remaining_percent: float = DEFAULT_MIN_REMAINING_PERCENT,
    wait_within_minutes: int = DEFAULT_WAIT_WITHIN_MINUTES,
):
    """Decide from quota alone: continue, wait, or enter redemption territory.

    ``minutes_to_reset is None`` means the reset time is unknown; the verdict is
    then ``redeem_candidate`` so the caller fetches the card inventory to decide
    further. A due-but-still-exhausted quota (``minutes_to_reset <= 0``) is a
    ``wait`` with ``minutes: 1`` and reason ``stale_at_boundary`` -- never a
    reason to spend a card while API propagation lags.
    """
    if remaining_percent is None:
        return {"status": "error", "reason": "quota_unreadable", "minutes": None}
    if remaining_percent > min_remaining_percent:
        return {
            "status": "continue",
            "reason": "quota_available",
            "minutes": minutes_to_reset,
        }
    if minutes_to_reset is None:
        return {
            "status": "redeem_candidate",
            "reason": "low_quota_reset_time_unknown",
            "minutes": None,
        }
    if minutes_to_reset <= 0:
        return {"status": "wait", "reason": "stale_at_boundary", "minutes": 1}
    if minutes_to_reset <= wait_within_minutes:
        return {
            "status": "wait",
            "reason": "natural_reset_within_threshold",
            "minutes": minutes_to_reset,
        }
    return {
        "status": "redeem_candidate",
        "reason": "low_quota_past_wait_window",
        "minutes": minutes_to_reset,
    }


def decide_with_cards(cards_available, minutes_to_reset):
    """Complete the decision once the card inventory is known (count >= 0)."""
    if cards_available is None:
        return {
            "status": "error",
            "reason": "cards_unavailable",
            "minutes": minutes_to_reset,
        }
    if cards_available <= 0:
        return {
            "status": "no_resets_available",
            "reason": "no_usable_five_hour_cards",
            "minutes": minutes_to_reset,
        }
    return {
        "status": "redeem_candidate",
        "reason": "usable_card_available",
        "minutes": minutes_to_reset,
    }
