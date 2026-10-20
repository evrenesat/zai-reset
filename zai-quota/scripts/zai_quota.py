#!/usr/bin/env python3
"""zai-quota CLI: Z.ai Coding Plan five-hour quota inspector and gated reset-card redemption.

Contract: exactly one JSON object on stdout per run (``schema_version`` 1);
diagnostics go to stderr and never contain secrets. ``quota_info`` is read-only.
``reset_please`` may POST a reset card only without ``--dry-run``; its first live
use must be an explicitly owner-authorized real low-quota moment. Development,
tests and smoke checks never POST.

State lives under ``--state-dir`` (default
``~/.local/state/zai-quota/<sha256(key)[:16]>/``, mode 0700, files 0600) and
holds the append-only redemption ledger, the pending-operation record and the
exclusive run lock.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import json
import os
import pathlib
import shlex
import subprocess
import sys
import time
import uuid

_SCRIPTS_DIR = pathlib.Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import decisions  # noqa: E402
import models  # noqa: E402
import zai_api  # noqa: E402

SCHEMA_VERSION = 1

# Configurable thresholds (original plan contract).
WAIT_IF_NATURAL_RESET_WITHIN_MINUTES = 60
MIN_REMAINING_QUOTA_PERCENT = 1.0

# Addendum: at most one redemption per natural five-hour window, enforced
# locally via the append-only ledger (complements the lock + pending record).
ONE_REDEMPTION_PER_NATURAL_WINDOW = True
REDEMPTION_PROPAGATION_MARGIN_MS = 120_000

DEFAULT_ENV_FILE = "~/.config/zai-quota/env"
DEFAULT_STATE_ROOT = "~/.local/state/zai-quota"
PENDING_FILE = "pending.json"
LEDGER_FILE = "ledger.jsonl"
LOCK_FILE = "lock"

_EXIT_CODES = {
    "ok": 0,
    "partial": 0,
    "continue": 0,
    "wait": 0,
    "no_resets_available": 0,
    "success": 0,
    "dry_run": 0,
    "uncertain": 3,
    "error": 2,
}


# ---------------------------------------------------------------------------
# auth
# ---------------------------------------------------------------------------


def _parse_env_file(text: str) -> dict:
    """Parse KEY=VALUE lines; comments and blank lines ignored; only the
    caller decides which keys are consumed. Values never reach logs."""
    values = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        if key:
            values[key] = value
    return values


def load_env_file(path) -> dict:
    """Read the env file if present; a missing file is not an error."""
    try:
        text = pathlib.Path(os.path.expanduser(str(path))).read_text(encoding="utf-8")
    except (FileNotFoundError, NotADirectoryError, IsADirectoryError):
        return {}
    except OSError as exc:
        _stderr_note("env file unreadable (%s); ignoring it" % type(exc).__name__)
        return {}
    return _parse_env_file(text)


def resolve_api_key(env=None, env_file=None):
    """ZAI_API_KEY from the process environment first, else the env file,
    else the stdout of $ZAI_API_KEY_CMD (one line, trimmed).

    The env file path comes from the explicit argument, else
    ``$ZAI_QUOTA_ENV_FILE``, else ``~/.config/zai-quota/env``. The command
    source lets a harness-specific helper supply the key without persisting
    it. Returns None when no key is found. The value is never printed or
    logged; command stderr is discarded and never surfaced.
    """
    env = os.environ if env is None else env
    key = env.get("ZAI_API_KEY")
    if key:
        return key
    path = env_file or env.get("ZAI_QUOTA_ENV_FILE") or DEFAULT_ENV_FILE
    key = load_env_file(path).get("ZAI_API_KEY")
    if key:
        return key
    command = env.get("ZAI_API_KEY_CMD")
    if command:
        try:
            proc = subprocess.run(
                shlex.split(command), capture_output=True, text=True, timeout=15
            )
        except (OSError, ValueError, subprocess.TimeoutExpired):
            return None
        if proc.returncode == 0:
            lines = (proc.stdout or "").strip().splitlines()
            if lines:
                return lines[0].strip()
    return None


# ---------------------------------------------------------------------------
# state dir, ledger, pending record, lock
# ---------------------------------------------------------------------------


class LockBusy(Exception):
    pass


def state_dir_for(api_key: str, override=None, home=None) -> pathlib.Path:
    """Default per-key state dir uses a non-reversible hash; never the key."""
    if override:
        return pathlib.Path(os.path.expanduser(str(override)))
    digest = hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:16]
    root = DEFAULT_STATE_ROOT if home is None else str(home) + "/.local/state/zai-quota"
    return pathlib.Path(os.path.expanduser(root)) / digest


def ensure_state_dir(path: pathlib.Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(str(path), 0o700)


@contextlib.contextmanager
def exclusive_lock(path, timeout_s=None, poll_interval_s=0.05):
    """Exclusive flock held until result and local state are committed.

    Default waits indefinitely (concurrent invocations serialize); with a
    timeout the wait is a bounded non-blocking poll and ``LockBusy`` is raised.
    """
    fd = os.open(str(path), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        os.chmod(str(path), 0o600)
        if timeout_s is None:
            fcntl.flock(fd, fcntl.LOCK_EX)
        else:
            deadline = time.monotonic() + timeout_s
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise LockBusy(str(path))
                    time.sleep(poll_interval_s)
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _ledger_path(state_dir) -> pathlib.Path:
    return pathlib.Path(state_dir) / LEDGER_FILE


def append_ledger(
    state_dir,
    *,
    action,
    card_id,
    request_id,
    result,
    next_reset_at_epoch_ms,
    ts=None,
) -> dict:
    """Append one line to the append-only redemption ledger (mode 0600, fsync)."""
    entry = {
        "ts": ts if ts is not None else models.iso_utc(_system_now_ms()),
        "action": action,
        "card_id": card_id,
        "request_id": request_id,
        "result": result,
        "next_reset_at_epoch_ms": next_reset_at_epoch_ms,
    }
    line = json.dumps(entry, separators=(",", ":")) + "\n"
    fd = os.open(
        str(_ledger_path(state_dir)), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600
    )
    try:
        os.write(fd, line.encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)
    os.chmod(str(_ledger_path(state_dir)), 0o600)
    return entry


def read_ledger(state_dir):
    """Parse the ledger; malformed lines are skipped, never fatal."""
    entries = []
    try:
        text = _ledger_path(state_dir).read_text(encoding="utf-8")
    except (FileNotFoundError, NotADirectoryError):
        return entries
    except OSError:
        return entries
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict):
            entries.append(entry)
    return entries


def ledger_block_info(state_dir, now_ms: int):
    """Return the ledger entry blocking redemption now, or None.

    With ONE_REDEMPTION_PER_NATURAL_WINDOW, a prior ``result == "success"``
    entry blocks while ``now < its next_reset_at_epoch_ms + 120s`` propagation
    margin. A success entry with an unknown window cannot be evaluated and is
    blocked conservatively (never risk a second card).
    """
    if not ONE_REDEMPTION_PER_NATURAL_WINDOW:  # pragma: no cover - constant True
        return None
    for entry in read_ledger(state_dir):
        if entry.get("result") != "success":
            continue
        reset_ms = entry.get("next_reset_at_epoch_ms")
        if not models.is_int(reset_ms):
            return entry
        if now_ms < reset_ms + REDEMPTION_PROPAGATION_MARGIN_MS:
            return entry
    return None


def _pending_path(state_dir) -> pathlib.Path:
    return pathlib.Path(state_dir) / PENDING_FILE


def write_pending(state_dir, record: dict) -> None:
    """Atomically persist the pending-operation record (mode 0600) pre-POST."""
    path = _pending_path(state_dir)
    tmp = path.with_name(PENDING_FILE + ".tmp")
    payload = json.dumps(record, separators=(",", ":")) + "\n"
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, payload.encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)
    os.chmod(str(tmp), 0o600)
    os.replace(str(tmp), str(path))


def read_pending(state_dir):
    try:
        text = _pending_path(state_dir).read_text(encoding="utf-8")
    except (FileNotFoundError, NotADirectoryError):
        return None
    except OSError:
        return None
    try:
        record = json.loads(text)
    except ValueError:
        return None
    return record if isinstance(record, dict) else None


def clear_pending(state_dir) -> None:
    pathlib.Path(_pending_path(state_dir)).unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# shared internals
# ---------------------------------------------------------------------------


def _system_now_ms() -> int:
    return int(time.time() * 1000)


def _stderr_note(message: str) -> None:
    sys.stderr.write("zai-quota: %s\n" % message)


def _response(status, minutes, reason, warnings, **extra) -> dict:
    resp = {
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "minutes": minutes,
        "reason": reason,
    }
    resp.update(extra)
    resp["warnings"] = list(warnings)
    return resp


def _threshold_kwargs():
    return {
        "min_remaining_percent": MIN_REMAINING_QUOTA_PERCENT,
        "wait_within_minutes": WAIT_IF_NATURAL_RESET_WITHIN_MINUTES,
    }


def _get_quota_snapshot(api_key, transport, now_ms, warnings):
    """Returns (snapshot, reason, detail); reason is the contract-level one."""
    try:
        payload = zai_api.fetch_quota_payload(api_key, transport=transport)
    except zai_api.ApiError as exc:
        warnings.append("quota GET failed (%s)" % exc.reason)
        return None, "quota_api_failed", exc.reason
    try:
        return models.parse_quota_payload(payload, now_ms()), None, None
    except models.DataError as exc:
        warnings.append("quota data unusable (%s)" % exc.reason)
        return None, "quota_api_failed", exc.reason


def _get_card_selection(api_key, transport, now_ms, warnings):
    """Returns (cards|None, skipped, reason|None, detail|None).

    ``cards is None`` means the inventory could not be read or was malformed;
    this is never treated as zero cards.
    """
    try:
        payload = zai_api.fetch_cards_payload(api_key, transport=transport)
    except zai_api.ApiError as exc:
        warnings.append("card inventory GET failed (%s)" % exc.reason)
        return None, 0, "cards_api_failed", exc.reason
    try:
        raw = models.parse_cards_payload(payload)
    except models.DataError as exc:
        warnings.append("card inventory data unusable (%s)" % exc.reason)
        return None, 0, "cards_api_failed", exc.reason
    cards, skipped = models.select_cards(raw, now_ms())
    return cards, skipped, None, None


def _quota_fields(snapshot) -> dict:
    return {
        "quota_used_percent": snapshot.used_percent,
        "quota_remaining_percent": snapshot.remaining_percent,
        "next_reset_at": models.iso_utc(snapshot.next_reset_at_epoch_ms),
        "next_reset_at_epoch_ms": snapshot.next_reset_at_epoch_ms,
    }


def _wait_response(snapshot, verdict, warnings) -> dict:
    if verdict["reason"] == "stale_at_boundary":
        warnings.append(
            "quota still exhausted at the natural reset boundary; "
            "waiting instead of spending a card while reads lag"
        )
    fields = _quota_fields(snapshot)
    fields["quota_remaining_percent"] = snapshot.remaining_percent
    return _response("wait", verdict["minutes"], verdict["reason"], warnings, **fields)


def _continue_response(snapshot, warnings) -> dict:
    fields = _quota_fields(snapshot)
    fields["quota_remaining_percent"] = snapshot.remaining_percent
    return _response(
        "continue", snapshot.minutes_to_reset, "quota_available", warnings, **fields
    )


def _blocked_response(snapshot, blocker, warnings) -> dict:
    return _response(
        "error",
        snapshot.minutes_to_reset if snapshot else None,
        "redemption_already_used_this_window",
        warnings,
        detail="a five-hour reset card was already redeemed in this natural window",
        blocking_card_id=blocker.get("card_id"),
        next_reset_at_epoch_ms=blocker.get("next_reset_at_epoch_ms"),
    )


# ---------------------------------------------------------------------------
# quota_info
# ---------------------------------------------------------------------------


def run_quota_info(api_key, *, transport=None, now_ms=None, warnings=None) -> dict:
    """Read-only inspection of quota plus reset-card inventory.

    The two GETs are independent: a failed inventory GET with a good quota
    response is a ``partial`` read (``cards_available`` null), never zero cards.
    """
    transport = transport or zai_api.default_transport
    now_ms = now_ms or _system_now_ms
    warnings = [] if warnings is None else warnings
    snapshot, reason, detail = _get_quota_snapshot(
        api_key, transport, now_ms, warnings
    )
    if snapshot is None:
        return _response("error", None, reason, warnings, detail=detail)
    resp = _response(
        "ok",
        snapshot.minutes_to_reset,
        "quota_and_cards_available",
        warnings,
        fetched_at=models.iso_utc(now_ms()),
        **_quota_fields(snapshot),
    )
    cards, skipped, cards_reason, cards_detail = _get_card_selection(
        api_key, transport, now_ms, warnings
    )
    if cards is None:
        warnings.append(
            "card inventory unavailable (%s); cards_available is unknown" % cards_detail
        )
        resp["status"] = "partial"
        resp["reason"] = "quota_available_cards_unknown"
        resp["cards_available"] = None
        resp["earliest_card_expires_at"] = None
        return resp
    if skipped:
        warnings.append(
            "%d card entries skipped (malformed, unavailable, consumed or expired)"
            % skipped
        )
    resp["cards_available"] = len(cards)
    resp["earliest_card_expires_at"] = cards[0].expire_time if cards else None
    return resp


# ---------------------------------------------------------------------------
# reset_please
# ---------------------------------------------------------------------------


def run_reset_please(
    api_key, *, state_dir=None, dry_run=False, transport=None, now_ms=None, warnings=None
) -> dict:
    """Gated redemption of one five-hour reset card (see plan algorithm 1-9)."""
    transport = transport or zai_api.default_transport
    now_ms = now_ms or _system_now_ms
    warnings = [] if warnings is None else warnings
    sdir = state_dir_for(api_key, state_dir)
    if dry_run:
        return _dry_run(api_key, sdir, transport, now_ms, warnings)
    ensure_state_dir(sdir)
    try:
        with exclusive_lock(sdir / LOCK_FILE):
            return _reset_please_locked(api_key, sdir, transport, now_ms, warnings)
    except LockBusy:
        warnings.append("another reset_please invocation holds the state lock")
        return _response("error", None, "lock_busy", warnings)


def _reset_please_locked(api_key, sdir, transport, now_ms, warnings) -> dict:
    # Step 9: resolve a persisted uncertain operation first; never retransmit.
    pending = read_pending(sdir)
    if pending is not None:
        return _resolve_pending(api_key, sdir, pending, transport, now_ms, warnings)

    # Step 2: fresh quota via the same implementation as quota_info.
    snapshot, reason, detail = _get_quota_snapshot(
        api_key, transport, now_ms, warnings
    )
    if snapshot is None:
        return _response("error", None, reason, warnings, detail=detail)

    # Steps 3-4: cheap exits before any inventory call.
    verdict = decisions.decide(
        snapshot.remaining_percent, snapshot.minutes_to_reset, **_threshold_kwargs()
    )
    if verdict["status"] == "continue":
        return _continue_response(snapshot, warnings)
    if verdict["status"] == "wait":
        return _wait_response(snapshot, verdict, warnings)

    # Addendum: one redemption per natural five-hour window (early check).
    blocker = ledger_block_info(sdir, now_ms())
    if blocker is not None:
        return _blocked_response(snapshot, blocker, warnings)

    # Step 5: card inventory; unreadable/malformed is an error, never zero cards.
    cards, skipped, cards_reason, cards_detail = _get_card_selection(
        api_key, transport, now_ms, warnings
    )
    if cards is None:
        return _response(
            "error",
            snapshot.minutes_to_reset,
            cards_reason,
            warnings,
            detail=cards_detail,
        )
    if skipped:
        warnings.append(
            "%d card entries skipped (malformed, unavailable, consumed or expired)"
            % skipped
        )
    if not cards:
        return _response(
            "no_resets_available",
            snapshot.minutes_to_reset,
            "no_usable_five_hour_cards",
            warnings,
            cards_available=0,
            **_quota_fields(snapshot),
        )
    card = cards[0]  # step 6: earliest expiry, recordId tie-break

    # Step 6: re-fetch quota once immediately before POST.
    fresh, fresh_reason, fresh_detail = _get_quota_snapshot(
        api_key, transport, now_ms, warnings
    )
    if fresh is None:
        warnings.append("quota re-check before POST failed; refusing to spend a card")
        return _response(
            "error",
            snapshot.minutes_to_reset,
            fresh_reason,
            warnings,
            detail=fresh_detail,
        )
    fresh_verdict = decisions.decide(
        fresh.remaining_percent, fresh.minutes_to_reset, **_threshold_kwargs()
    )
    if fresh_verdict["status"] == "continue":
        warnings.append("quota recovered before POST; card not spent")
        return _continue_response(fresh, warnings)
    if fresh_verdict["status"] == "wait":
        warnings.append("natural reset reached the wait threshold before POST; card not spent")
        return _wait_response(fresh, fresh_verdict, warnings)

    # Addendum: re-check the ledger immediately before the POST.
    blocker = ledger_block_info(sdir, now_ms())
    if blocker is not None:
        return _blocked_response(fresh, blocker, warnings)

    # Step 7: persist the pending record (card id + UUID request id) pre-POST.
    request_id = str(uuid.uuid4())
    write_pending(
        sdir,
        {
            "card_id": card.record_id,
            "request_id": request_id,
            "ts": models.iso_utc(now_ms()),
            "next_reset_at_epoch_ms": fresh.next_reset_at_epoch_ms,
        },
    )
    try:
        zai_api.post_reset_card(
            api_key, card.record_id, request_id, transport=transport
        )
    except zai_api.ApiError as exc:
        if exc.definite:
            # Definite API rejection: outcome known, safe to record and clear.
            append_ledger(
                sdir,
                action="redeem",
                card_id=card.record_id,
                request_id=request_id,
                result="error",
                next_reset_at_epoch_ms=fresh.next_reset_at_epoch_ms,
                ts=models.iso_utc(now_ms()),
            )
            clear_pending(sdir)
            return _response(
                "error",
                fresh.minutes_to_reset,
                "provider_rejected",
                warnings,
                detail=exc.reason,
                provider_message=exc.message,
                card_id=card.record_id,
                request_id=request_id,
                **_quota_fields(fresh),
            )
        # Timeout / broken connection / 5xx / unreadable: outcome UNKNOWN.
        append_ledger(
            sdir,
            action="redeem",
            card_id=card.record_id,
            request_id=request_id,
            result="uncertain",
            next_reset_at_epoch_ms=fresh.next_reset_at_epoch_ms,
            ts=models.iso_utc(now_ms()),
        )
        # The pending record is intentionally kept for the next invocation.
        return _response(
            "uncertain",
            fresh.minutes_to_reset,
            "reset_response_lost",
            warnings,
            card_id=card.record_id,
            request_id=request_id,
            **_quota_fields(fresh),
        )

    # Step 8: definite success -> safe local record, clear pending, then verify.
    append_ledger(
        sdir,
        action="redeem",
        card_id=card.record_id,
        request_id=request_id,
        result="success",
        next_reset_at_epoch_ms=fresh.next_reset_at_epoch_ms,
        ts=models.iso_utc(now_ms()),
    )
    clear_pending(sdir)
    return _verify_success(
        api_key, card, request_id, transport, now_ms, warnings
    )


def _verify_success(api_key, card, request_id, transport, now_ms, warnings) -> dict:
    """Read-only verification after an acknowledged redemption (step 8)."""
    snapshot, _reason, _detail = _get_quota_snapshot(
        api_key, transport, now_ms, warnings
    )
    cards, _skipped, _creason, _cdetail = _get_card_selection(
        api_key, transport, now_ms, warnings
    )
    verification_pending = False
    minutes = None
    extra = {"card_id": card.record_id, "request_id": request_id}
    if snapshot is None:
        verification_pending = True
        warnings.append("quota verification GET failed after acknowledged redemption")
    else:
        minutes = snapshot.minutes_to_reset
        extra.update(_quota_fields(snapshot))
        extra["quota_remaining_percent"] = snapshot.remaining_percent
        if snapshot.remaining_percent <= MIN_REMAINING_QUOTA_PERCENT:
            verification_pending = True
            warnings.append(
                "provider acknowledged the redemption but quota still reads low; "
                "reads may lag (verification_pending)"
            )
    if cards is None:
        verification_pending = True
        warnings.append("card inventory verification GET failed after redemption")
    elif any(c.record_id == card.record_id for c in cards):
        verification_pending = True
        warnings.append(
            "redeemed card still listed as available; reads may lag (verification_pending)"
        )
    extra["verification_pending"] = verification_pending
    return _response("success", minutes, "reset_card_redeemed", warnings, **extra)


def _resolve_pending(api_key, sdir, pending, transport, now_ms, warnings) -> dict:
    """Step 9: refresh quota + inventory and resolve the uncertain POST.

    Definitive confirmation of consumption clears the pending state and returns
    success without any POST. Anything else returns uncertain with the pending
    card id. An uncertain POST is never retransmitted automatically.
    """
    card_id = pending.get("card_id")
    request_id = pending.get("request_id")
    warnings.append(
        "persisted uncertain redemption record found; refreshing reads, no POST is retransmitted"
    )
    snapshot, _reason, _detail = _get_quota_snapshot(
        api_key, transport, now_ms, warnings
    )
    cards, _skipped, _creason, _cdetail = _get_card_selection(
        api_key, transport, now_ms, warnings
    )
    confirmed_how = None
    if snapshot is not None and snapshot.remaining_percent > MIN_REMAINING_QUOTA_PERCENT:
        confirmed_how = "quota_recovered"
    elif (
        cards is not None
        and models.is_int(card_id)
        and not any(c.record_id == card_id for c in cards)
    ):
        confirmed_how = "card_no_longer_available"
    if confirmed_how is None:
        return _response(
            "uncertain",
            snapshot.minutes_to_reset if snapshot else None,
            "pending_reset_unverified",
            warnings,
            card_id=card_id,
            request_id=request_id,
            **(_quota_fields(snapshot) if snapshot else {}),
        )
    append_ledger(
        sdir,
        action="resolve_pending",
        card_id=card_id,
        request_id=request_id,
        result="success",
        next_reset_at_epoch_ms=(
            snapshot.next_reset_at_epoch_ms
            if snapshot
            else pending.get("next_reset_at_epoch_ms")
        ),
        ts=models.iso_utc(now_ms()),
    )
    clear_pending(sdir)
    extra = {
        "card_id": card_id,
        "request_id": request_id,
        "confirmed_from": confirmed_how,
        "verification_pending": bool(
            snapshot is None or snapshot.remaining_percent <= MIN_REMAINING_QUOTA_PERCENT
        ),
    }
    minutes = None
    if snapshot is not None:
        minutes = snapshot.minutes_to_reset
        extra.update(_quota_fields(snapshot))
        extra["quota_remaining_percent"] = snapshot.remaining_percent
    return _response("success", minutes, "reset_card_redeemed", warnings, **extra)


def _dry_run(api_key, sdir, transport, now_ms, warnings) -> dict:
    """GET-only simulation of reset_please. Zero POSTs, zero state writes."""
    snapshot, reason, detail = _get_quota_snapshot(
        api_key, transport, now_ms, warnings
    )
    if snapshot is None:
        return _response("error", None, reason, warnings, detail=detail)
    if read_pending(sdir) is not None:
        warnings.append(
            "an unresolved pending redemption record exists; a real reset_please "
            "would try to resolve it before anything else"
        )
    resp = _response(
        "dry_run",
        snapshot.minutes_to_reset,
        "dry_run",
        warnings,
        fetched_at=models.iso_utc(now_ms()),
        **_quota_fields(snapshot),
    )
    verdict = decisions.decide(
        snapshot.remaining_percent, snapshot.minutes_to_reset, **_threshold_kwargs()
    )
    if verdict["status"] in ("continue", "wait"):
        resp["would_status"] = verdict["status"]
        resp["would_reason"] = verdict["reason"]
        return resp
    blocker = ledger_block_info(sdir, now_ms())
    if blocker is not None:
        resp["would_status"] = "error"
        resp["would_reason"] = "redemption_already_used_this_window"
        resp["blocking_card_id"] = blocker.get("card_id")
        return resp
    cards, skipped, cards_reason, cards_detail = _get_card_selection(
        api_key, transport, now_ms, warnings
    )
    if cards is None:
        resp["would_status"] = "error"
        resp["would_reason"] = cards_reason
        resp["detail"] = cards_detail
        return resp
    if skipped:
        warnings.append(
            "%d card entries skipped (malformed, unavailable, consumed or expired)"
            % skipped
        )
    if not cards:
        resp["would_status"] = "no_resets_available"
        resp["would_reason"] = "no_usable_five_hour_cards"
        resp["cards_available"] = 0
        return resp
    resp["would_status"] = "redeem_candidate"
    resp["would_reason"] = "usable_card_available"
    resp["would_redeem_card_id"] = cards[0].record_id
    return resp


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--state-dir",
        default=None,
        metavar="PATH",
        help="state directory override (default: per-key dir under "
        "~/.local/state/zai-quota); used by tests",
    )
    parser = argparse.ArgumentParser(
        prog="zai_quota.py",
        description="Z.ai Coding Plan five-hour quota helper (one JSON object on stdout).",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    quota_parser = subparsers.add_parser(
        "quota_info",
        parents=[common],
        help="read-only quota and reset-card inventory inspection",
    )
    quota_parser.set_defaults(func="quota_info")
    reset_parser = subparsers.add_parser(
        "reset_please",
        parents=[common],
        help="gated redemption of one five-hour reset card (may POST; see --dry-run)",
    )
    reset_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="GET-only simulation: reports would_status / would_redeem_card_id, never POSTs",
    )
    reset_parser.set_defaults(func="reset_please")
    return parser


def _dispatch(args, api_key, transport, now_ms, warnings) -> dict:
    if args.func == "quota_info":
        return run_quota_info(
            api_key, transport=transport, now_ms=now_ms, warnings=warnings
        )
    return run_reset_please(
        api_key,
        state_dir=args.state_dir,
        dry_run=bool(getattr(args, "dry_run", False)),
        transport=transport,
        now_ms=now_ms,
        warnings=warnings,
    )


def main(argv=None, *, transport=None, now_ms=None, env=None) -> int:
    """CLI entry point. Returns the process exit code (argparse may SystemExit).

    ``transport``, ``now_ms`` and ``env`` are injection seams for tests; the
    real command line uses urllib, the wall clock and os.environ.
    """
    parser = _build_parser()
    args = parser.parse_args(argv)
    warnings = []
    api_key = resolve_api_key(env=env)
    if not api_key:
        resp = _response("error", None, "missing_api_key", warnings)
    else:
        try:
            resp = _dispatch(args, api_key, transport, now_ms, warnings)
        except Exception as exc:  # defensive: stdout stays one JSON object
            _stderr_note("internal error: %s" % type(exc).__name__)
            resp = _response(
                "error",
                None,
                "internal_error",
                warnings,
                detail=type(exc).__name__,
            )
    for warning in resp.get("warnings", []):
        _stderr_note("warning: %s" % warning)
    sys.stdout.write(json.dumps(resp) + "\n")
    sys.stdout.flush()
    return _EXIT_CODES.get(resp.get("status"), 2)


if __name__ == "__main__":
    sys.exit(main())
