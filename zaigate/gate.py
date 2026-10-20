"""Pure quota-gate decision logic (plan §B6).

evaluate() performs no I/O and takes an injectable clock; callers pass the
parsed ``zai_quota.py quota_info`` JSON plus run-policy booleans. Verdicts:
``dispatch_ok | wait_natural | watch | wait_no_cards | redeem_candidate |
needs_user``. This is the coordinator-side gate only — the zai-quota CLI
independently re-enforces its lock, pending record and per-window ledger
before any POST, so a bug here can never alone spend a card twice.
"""

DISPATCH_MIN_REMAINING_PERCENT = 5.0
LOW_PERCENT = 1.0
WAIT_NATURAL_WITHIN_MIN = 60
RESUME_PROPAGATION_BUFFER_MIN = 2
MAX_RESUME_RECHECKS = 8
WATCHDOG_MINUTES = 60

# Manifest policy keys with their built-in fallbacks (plan §B0).
_POLICY_DEFAULTS = {
    "allow_reset": True,
    "dispatch_min_remaining_percent": DISPATCH_MIN_REMAINING_PERCENT,
    "low_percent": LOW_PERCENT,
    "wait_natural_within_min": WAIT_NATURAL_WITHIN_MIN,
    "watchdog_minutes": WATCHDOG_MINUTES,
    "max_resume_rechecks": MAX_RESUME_RECHECKS,
}


def load_policy(manifest):
    """Merge run.json policy over the built-in defaults (manifest wins)."""
    policy = dict(_POLICY_DEFAULTS)
    raw = (manifest or {}).get("policy")
    if isinstance(raw, dict):
        for key in policy:
            if raw.get(key) is not None:
                policy[key] = raw[key]
    return policy


def _is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _reset_wake_epoch_ms(quota_info, now_ms, buffer_min):
    """Best wake time after the next natural reset, from whatever is known."""
    reset_ms = quota_info.get("next_reset_at_epoch_ms")
    if _is_number(reset_ms):
        return int(reset_ms + buffer_min * 60_000)
    minutes = quota_info.get("minutes")
    if _is_number(minutes):
        return int(now_ms + (minutes + buffer_min) * 60_000)
    return int(now_ms + 30 * 60_000)


def evaluate(quota_info, *, allow_reset, work_remaining, cooldown_active,
             now_ms, policy=None):
    """Apply the §B6 verdict table; return the verdict dict with echo fields."""
    policy = policy or dict(_POLICY_DEFAULTS)
    remaining = quota_info.get("quota_remaining_percent")
    minutes = quota_info.get("minutes")
    cards = quota_info.get("cards_available")
    echo = {"remaining_percent": remaining, "minutes": minutes,
            "cards_available": cards}

    status = quota_info.get("status")
    if status not in ("ok", "partial"):
        return {"verdict": "needs_user", "reason": f"quota_status_{status}",
                **echo}
    if not _is_number(remaining):
        return {"verdict": "needs_user", "reason": "quota_unreadable", **echo}

    dispatch_min = policy["dispatch_min_remaining_percent"]
    low = policy["low_percent"]
    wait_within = policy["wait_natural_within_min"]
    buffer_min = RESUME_PROPAGATION_BUFFER_MIN

    if remaining >= dispatch_min:
        return {"verdict": "dispatch_ok", "reason": "quota_available", **echo}

    if remaining <= low:
        if not _is_number(minutes):
            return {"verdict": "watch", "reason": "reset_time_unknown", **echo}
        if minutes <= wait_within:
            return {"verdict": "wait_natural",
                    "reason": "natural_reset_within_threshold",
                    "wake_at_epoch_ms": int(now_ms + (minutes + buffer_min) * 60_000),
                    **echo}
        if cards is None:
            return {"verdict": "needs_user", "reason": "inventory_unknown",
                    **echo}
        if cards == 0:
            return {"verdict": "wait_no_cards",
                    "reason": "no_usable_five_hour_cards",
                    "wake_at_epoch_ms": int(now_ms + (minutes + buffer_min) * 60_000),
                    **echo}
        if not allow_reset:
            return {"verdict": "needs_user",
                    "reason": "redemption_disabled_by_policy", **echo}
        if not _is_number(work_remaining) or work_remaining <= 0:
            return {"verdict": "wait_no_cards",
                    "reason": "no_work_remaining_no_reset",
                    "wake_at_epoch_ms": _reset_wake_epoch_ms(quota_info, now_ms, buffer_min),
                    **echo}
        if cooldown_active:
            return {"verdict": "wait_natural",
                    "reason": "redemption_cooldown_active",
                    "wake_at_epoch_ms": _reset_wake_epoch_ms(quota_info, now_ms, buffer_min),
                    **echo}
        return {"verdict": "redeem_candidate", "reason": "logical_reset_window",
                **echo}

    return {"verdict": "dispatch_ok", "reason": "quota_low_but_usable", **echo}
