"""Contract-side event journal for zaigate (plans/run-dir-contract.md).

Implements the append/read rules both sides of the contract follow:
exclusive flock on ``events.lock``, ``seq = max existing + 1``, fsync before
release, tolerant reads. Deliberately independent of the delegation engine's
implementation — the journal format is the contract, not shared code.
"""

import fcntl
import json
import os
from datetime import datetime, timezone

EVENTS_FILE = "events.jsonl"
LOCK_FILE = "events.lock"


def utc_now_iso():
    return datetime.now(timezone.utc).isoformat()


def _lines(run_dir):
    path = os.path.join(run_dir, EVENTS_FILE)
    if not os.path.isfile(path):
        return
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            yield line


def _coerce(obj):
    if (isinstance(obj, dict) and isinstance(obj.get("seq"), int)
            and not isinstance(obj.get("seq"), bool) and obj["seq"] >= 1):
        return {"seq": obj["seq"], "ts": obj.get("ts"), "type": obj.get("type"),
                "payload": obj.get("payload")}
    return None


def append_event(run_dir, type, payload):
    """Append one event under the contract lock; return the stored event."""
    if not isinstance(type, str) or not type:
        raise ValueError("event type must be a non-empty string")
    if payload is None:
        payload = {}
    if not isinstance(payload, dict):
        raise ValueError("event payload must be a JSON object")
    os.makedirs(run_dir, exist_ok=True)
    lock_path = os.path.join(run_dir, LOCK_FILE)
    with open(lock_path, "a", encoding="utf-8") as lockf:
        os.chmod(lock_path, 0o600)
        fcntl.flock(lockf.fileno(), fcntl.LOCK_EX)
        try:
            highest = 0
            for line in _lines(run_dir):
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = _coerce(json.loads(line))
                except ValueError:
                    continue
                if ev and ev["seq"] > highest:
                    highest = ev["seq"]
            event = {"seq": highest + 1, "ts": utc_now_iso(), "type": type,
                     "payload": payload}
            with open(os.path.join(run_dir, EVENTS_FILE), "a",
                      encoding="utf-8") as f:
                f.write(json.dumps(event, ensure_ascii=False) + "\n")
                f.flush()
                os.fsync(f.fileno())
        finally:
            fcntl.flock(lockf.fileno(), fcntl.LOCK_UN)
    return event


def read_events(run_dir, since_seq=0):
    """Events with seq > since_seq, ordered; malformed lines skipped."""
    out = []
    for line in _lines(run_dir):
        line = line.strip()
        if not line:
            continue
        try:
            ev = _coerce(json.loads(line))
        except ValueError:
            continue
        if ev and ev["seq"] > since_seq:
            out.append(ev)
    out.sort(key=lambda e: e["seq"])
    return out
