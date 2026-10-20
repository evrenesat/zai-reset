"""Z.ai quota policy for delegation runs (plan §B6, zaigate side).

Engine-independent by contract (plans/run-dir-contract.md): functions take a
RUN DIRECTORY, read/write run.json + events.jsonl + NOTIFY.md, and call the
zai-quota CLI for provider reads/redemption. Nothing here imports the
delegation engine, and the engine learns about Z.ai only through the verdict
JSON this package prints. Wake scheduling is the engine's job; verdicts carry
``wake_at_epoch_ms`` / watch guidance instead of callbacks.
"""

import json
import os
import shlex
import subprocess
import sys
import tempfile

from . import gate, journal

REDEMPTION_COOLDOWN_MARGIN_MS = 120_000


def repo_root():
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _quota_script():
    return os.path.join(repo_root(), "zai-quota", "scripts", "zai_quota.py")


def quota_command(subcommand="quota_info"):
    """Quota CLI argv; tests override via ZAI_QUOTA_CMD / ZAI_QUOTA_CMD_RESET."""
    env_var = ("ZAI_QUOTA_CMD" if subcommand == "quota_info"
               else "ZAI_QUOTA_CMD_RESET")
    override = os.environ.get(env_var)
    if override:
        return shlex.split(override)
    return [sys.executable, _quota_script(), subcommand]


def _run_quota_cmd(cmd, timeout_s):
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=timeout_s)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"status": "error",
                "reason": f"quota_cli_failed_{type(exc).__name__}",
                "minutes": None}
    for line in reversed((proc.stdout or "").splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
        except ValueError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return {"status": "error",
            "reason": f"quota_cli_failed_rc_{proc.returncode}", "minutes": None}


def run_quota_info(run_dir, *, cmd=None):
    """Read quota through the zai-quota CLI (read-only; never raises)."""
    return _run_quota_cmd(cmd or quota_command("quota_info"), 60)


def _read_manifest(run_dir):
    with open(os.path.join(run_dir, "run.json"), encoding="utf-8") as f:
        manifest = json.load(f)
    if not isinstance(manifest, dict):
        raise ValueError("run.json is not a JSON object")
    return manifest


def _write_manifest(run_dir, manifest):
    path = os.path.join(run_dir, "run.json")
    fd, tmp = tempfile.mkstemp(dir=run_dir, prefix=".run.json.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(manifest, f, indent=2, ensure_ascii=False)
            f.write("\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _set_status(run_dir, status):
    manifest = _read_manifest(run_dir)
    manifest["status"] = status
    _write_manifest(run_dir, manifest)


def _append_notify(run_dir, text):
    path = os.path.join(run_dir, "NOTIFY.md")
    old = ""
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            old = f.read()
    with open(path, "w", encoding="utf-8") as f:
        f.write(old + text)
    os.chmod(path, 0o600)


def _quota_event(run_dir, quota_info, source):
    journal.append_event(run_dir, "quota_state", {
        "status": quota_info.get("status"),
        "remaining_percent": quota_info.get("quota_remaining_percent"),
        "minutes": quota_info.get("minutes"),
        "cards_available": quota_info.get("cards_available"),
        "source": source,
    })


def cooldown_active(event_list, now_ms):
    """One redemption per natural window (mirrors the CLI-side ledger)."""
    for event in event_list:
        if not isinstance(event, dict) or event.get("type") != "redemption":
            continue
        payload = event.get("payload") or {}
        if payload.get("action") != "success":
            continue
        reset_ms = payload.get("next_reset_at_epoch_ms")
        if not isinstance(reset_ms, (int, float)) or isinstance(reset_ms, bool):
            return True
        if now_ms < reset_ms + REDEMPTION_COOLDOWN_MARGIN_MS:
            return True
    return False


def gate_check(run_dir, *, work_remaining, run_quota_fn=None, now_ms=None):
    """Fresh quota read + §B6 verdict for a run directory."""
    import time
    now_ms = int(time.time() * 1000) if now_ms is None else int(now_ms)
    policy = gate.load_policy(_read_manifest(run_dir))
    quota_info = run_quota_fn() if run_quota_fn else run_quota_info(run_dir)
    _quota_event(run_dir, quota_info, "gate_check")
    verdict = gate.evaluate(
        quota_info,
        allow_reset=policy["allow_reset"],
        work_remaining=work_remaining,
        cooldown_active=cooldown_active(journal.read_events(run_dir), now_ms),
        now_ms=now_ms,
        policy=policy,
    )
    return {"verdict": verdict, "quota_info": quota_info}


def redeem(run_dir, *, work_remaining=1, run_quota_fn=None, run_reset_fn=None,
           now_ms=None):
    """Run reset_please only when the gate says redeem_candidate."""
    check = gate_check(run_dir, work_remaining=work_remaining,
                       run_quota_fn=run_quota_fn, now_ms=now_ms)
    verdict = check["verdict"]
    if verdict["verdict"] != "redeem_candidate":
        return {"executed": False, "verdict": verdict,
                "quota_info": check["quota_info"]}
    resp = run_reset_fn() if run_reset_fn else _run_quota_cmd(
        quota_command("reset_please"), 120)
    status = resp.get("status")
    if status == "success":
        fresh = run_quota_fn() if run_quota_fn else run_quota_info(run_dir)
        next_reset = resp.get("next_reset_at_epoch_ms")
        if next_reset is None:
            next_reset = fresh.get("next_reset_at_epoch_ms")
        journal.append_event(run_dir, "redemption", {
            "action": "success", "card_id": resp.get("card_id"),
            "next_reset_at_epoch_ms": next_reset,
        })
        return {"executed": True, "status": "success", "quota_info": fresh,
                "verdict_hint": "dispatch_ok"}
    if status == "uncertain":
        journal.append_event(run_dir, "redemption", {
            "action": "uncertain", "card_id": resp.get("card_id")})
        journal.append_event(run_dir, "needs_user", {
            "reason": "redemption_uncertain",
            "detail": "POST outcome unknown; the zai-quota CLI never "
                      "retransmits; resolve via read-only verification"})
        _set_status(run_dir, "needs_user")
        return {"executed": True, "status": "uncertain", "quota_info": resp}
    reason = resp.get("reason")
    journal.append_event(run_dir, "redemption",
                         {"action": "error", "detail": reason})
    journal.append_event(run_dir, "needs_user",
                         {"reason": "redemption_error", "detail": reason})
    _set_status(run_dir, "needs_user")
    return {"executed": True, "status": "error", "reason": reason,
            "quota_info": resp}


def pause(run_dir, *, reason, eta_minutes=None, note=None):
    """Park a run on quota exhaustion; the engine arms the resume wake."""
    status = "paused_quota" if reason == "quota" else "needs_user"
    _set_status(run_dir, status)
    journal.append_event(run_dir, "run_paused",
                         {"reason": reason, "eta_minutes": eta_minutes})
    _append_notify(
        run_dir,
        f"\n## PAUSED ({journal.utc_now_iso()})\n\n"
        f"- reason: {reason}\n"
        f"- eta to quota return: {eta_minutes} minutes\n"
        f"- resume with: python3 -m zaigate resume-check {run_dir}\n"
        + (f"- note: {note}\n" if note else ""),
    )
    return {"paused": True, "reason": reason, "eta_minutes": eta_minutes,
            "status": status}


def resume_check(run_dir, *, run_quota_fn=None, now_ms=None):
    """Re-gate a paused_quota run; resume, advise recheck, or park."""
    import time
    now_ms = int(time.time() * 1000) if now_ms is None else int(now_ms)
    manifest = _read_manifest(run_dir)
    status = manifest.get("status")
    if status != "paused_quota":
        return {"resumed": False, "reason": f"status_{status}"}
    policy = gate.load_policy(manifest)
    quota_info = run_quota_fn() if run_quota_fn else run_quota_info(run_dir)
    _quota_event(run_dir, quota_info, "resume_check")
    remaining = quota_info.get("quota_remaining_percent")
    if (isinstance(remaining, (int, float)) and not isinstance(remaining, bool)
            and remaining >= policy["dispatch_min_remaining_percent"]):
        journal.append_event(run_dir, "quota_recovered",
                             {"remaining_percent": remaining})
        _set_status(run_dir, "active")
        manifest = _read_manifest(run_dir)
        manifest["resume_rechecks"] = 0
        _write_manifest(run_dir, manifest)
        return {"resumed": True, "remaining_percent": remaining,
                "verdict_hint": "dispatch_ok"}

    rechecks = int(manifest.get("resume_rechecks") or 0) + 1
    manifest["resume_rechecks"] = rechecks
    _write_manifest(run_dir, manifest)
    if rechecks > policy["max_resume_rechecks"]:
        journal.append_event(run_dir, "needs_user", {
            "reason": "resume_rechecks_exhausted", "rechecks": rechecks})
        _set_status(run_dir, "needs_user")
        _append_notify(
            run_dir,
            f"\n## PARKED ({journal.utc_now_iso()})\n\n"
            f"- quota has not returned after {rechecks} resume checks\n"
            f"- manual decision required\n")
        return {"resumed": False, "parked": True, "resume_rechecks": rechecks}
    minutes = quota_info.get("minutes")
    return {"resumed": False, "resume_rechecks": rechecks,
            "next_check_minutes": minutes,
            "recheck_wake_hint_epoch_ms": (
                now_ms + int((minutes + gate.RESUME_PROPAGATION_BUFFER_MIN)
                             * 60_000)
                if isinstance(minutes, (int, float))
                and not isinstance(minutes, bool) else None)}
