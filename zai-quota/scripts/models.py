"""Pure parsing helpers for Z.ai five-hour quota and reset-card payloads.

No I/O happens here: every function takes already-fetched payload dicts plus a
``now_ms`` clock value so tests can freeze time. Malformed data raises
``DataError`` with a stable machine-readable ``reason``; it is never interpreted
as "0% used" or "zero cards".
"""

from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass

# limits[].unit == 3 identifies the five-hour token window in the cited
# implementations. Weekly windows (and everything else) are ignored.
FIVE_HOUR_UNIT = 3

# Card expiry strings look like "2026-10-19 09:18:08" with no timezone. Source
# projects disagree on UTC vs UTC+8; until the offset is confirmed against the
# dashboard (plan acceptance step) raw timestamps are compared under one
# uniform convention: rendered UTC in the same naive shape. Sorting and expiry
# exclusion are therefore internally consistent and stable.
_NAIVE_FORMAT = "%Y-%m-%d %H:%M:%S"


class DataError(Exception):
    """Raised when a payload cannot be safely interpreted as quota/card data."""

    def __init__(self, reason: str, message: str = ""):
        super().__init__(message or reason)
        self.reason = reason
        self.message = message or reason


@dataclass(frozen=True)
class QuotaSnapshot:
    used_percent: float
    remaining_percent: float
    next_reset_at_epoch_ms: int
    minutes_to_reset: int


@dataclass(frozen=True)
class ResetCard:
    record_id: int
    expire_time: str
    available: bool


def is_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def minutes_until(now_ms: int, reset_ms: int) -> int:
    """Ceiling-rounded whole minutes until ``reset_ms``; 0 once it is due."""
    return max(0, math.ceil((reset_ms - now_ms) / 60000))


def iso_utc(epoch_ms: int) -> str:
    """Epoch milliseconds -> "YYYY-MM-DDTHH:MM:SSZ" (UTC)."""
    return dt.datetime.fromtimestamp(epoch_ms / 1000, tz=dt.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def utc_naive_string(epoch_ms: int) -> str:
    """Epoch milliseconds rendered in the same naive shape Z.ai uses for expiry strings."""
    return dt.datetime.fromtimestamp(epoch_ms / 1000, tz=dt.timezone.utc).strftime(
        _NAIVE_FORMAT
    )


def parse_quota_payload(payload, now_ms: int) -> QuotaSnapshot:
    """Extract the five-hour window from a quota envelope payload.

    ``percentage`` is the USED percentage; remaining is computed as 100 - used.
    A missing or malformed five-hour window raises ``DataError``.
    """
    if not isinstance(payload, dict):
        raise DataError("invalid_quota_data", "quota payload is not a JSON object")
    data = payload.get("data")
    limits = data.get("limits") if isinstance(data, dict) else None
    if not isinstance(limits, list):
        raise DataError("missing_five_hour_window", "quota payload has no limits list")
    entry = None
    for item in limits:
        if isinstance(item, dict) and item.get("unit") == FIVE_HOUR_UNIT:
            entry = item
            break
    if entry is None:
        raise DataError(
            "missing_five_hour_window",
            "no limits entry with unit == %d (five-hour window)" % FIVE_HOUR_UNIT,
        )
    used_raw = entry.get("percentage")
    if not is_number(used_raw) or not (0.0 <= float(used_raw) <= 100.0):
        raise DataError(
            "invalid_percentage",
            "five-hour percentage is missing, non-numeric or out of range",
        )
    reset_ms = entry.get("nextResetTime")
    if not is_int(reset_ms) or reset_ms <= 0:
        raise DataError(
            "invalid_next_reset_time",
            "five-hour nextResetTime is missing or not a positive epoch-ms integer",
        )
    used = round(float(used_raw), 6)
    remaining = round(100.0 - used, 6)
    return QuotaSnapshot(
        used_percent=used,
        remaining_percent=remaining,
        next_reset_at_epoch_ms=reset_ms,
        minutes_to_reset=minutes_until(now_ms, reset_ms),
    )


def parse_cards_payload(payload) -> list:
    """Return the raw ``data.fiveHourResets`` list from a card inventory payload.

    An absent list raises ``DataError("missing_five_hour_resets")`` -- it is
    never treated as an empty inventory. Weekly cards are ignored entirely.
    """
    if not isinstance(payload, dict):
        raise DataError("invalid_cards_data", "cards payload is not a JSON object")
    data = payload.get("data")
    raw = data.get("fiveHourResets") if isinstance(data, dict) else None
    if not isinstance(raw, list):
        raise DataError(
            "missing_five_hour_resets",
            "cards payload omits data.fiveHourResets; refusing to assume an empty inventory",
        )
    return raw


def _parse_card(item):
    """Return a ResetCard for a fully valid, available, unspent entry, else None."""
    if not isinstance(item, dict):
        return None
    record_id = item.get("recordId")
    expire_time = item.get("expireTime")
    available = item.get("available")
    if not is_int(record_id) or record_id <= 0:
        return None
    if not isinstance(expire_time, str) or not expire_time.strip():
        return None
    if available is not True:
        return None
    if item.get("consumed") or item.get("redeemed"):
        return None
    return ResetCard(record_id=record_id, expire_time=expire_time, available=True)


def select_cards(raw_cards, now_ms: int):
    """Filter and order usable five-hour cards.

    Returns ``(cards, skipped)`` where ``cards`` is sorted by earliest expiry
    with ascending ``recordId`` as the deterministic tie-break, and ``skipped``
    counts entries dropped because they were malformed, marked consumed,
    unavailable, or already expired. Raw expiry strings sort chronologically
    under one uniform naive-time convention.
    """
    now_naive = utc_naive_string(now_ms)
    cards = []
    skipped = 0
    for item in raw_cards:
        card = _parse_card(item)
        if card is None:
            skipped += 1
            continue
        if card.expire_time < now_naive:
            skipped += 1
            continue
        cards.append(card)
    cards.sort(key=lambda c: (c.expire_time, c.record_id))
    return cards, skipped
