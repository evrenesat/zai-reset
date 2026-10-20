---
name: zai-quota
description: Check the Z.ai Coding Plan five-hour quota, redeem five-hour reset cards when it is logically safe, and guide agents on waiting for natural quota renewal instead of burning failed calls or cards.
---

# zai-quota — Z.ai five-hour quota and reset cards

Portable skill (ZCode, Pi, OpenCode). The CLI is the only thing that talks to
the API; it prints exactly one JSON object and never prints the key.

## Commands

```sh
python3 <skill-dir>/scripts/zai_quota.py quota_info                 # read-only
python3 <skill-dir>/scripts/zai_quota.py reset_please --dry-run      # GET-only simulation
python3 <skill-dir>/scripts/zai_quota.py reset_please                # may POST (gated)
```

Auth: `ZAI_API_KEY` env → env file (`ZAI_QUOTA_ENV_FILE`, default
`~/.config/zai-quota/env`, mode 0600) → `$ZAI_API_KEY_CMD` helper command.

## How to act on `quota_info`

- Report the numbers verbatim (`quota_used_percent`, `quota_remaining_percent`,
  `minutes`, `cards_available`, `earliest_card_expires_at`). Never invent
  quota figures or reset times.
- `status: "partial"` means the card inventory is UNKNOWN — never present it
  as zero cards.
- `status: "error"` → report and stop; do not retry in a loop.

## When to call `reset_please`

Call it when quota is exhausted, the provider rejects calls for quota, or
remaining is at/below the 1% threshold — and only when you are authorized to
spend a card. Prefer `--dry-run` first to preview the decision. The CLI
itself enforces every safety rule (exclusive lock, pending-record handling,
one redemption per natural window); never try to redeem by any other means,
and never retransmit an `uncertain` POST.

## Acting on `reset_please` statuses

| status | action |
| --- | --- |
| `continue` | continue the current task; no wake-up scheduling |
| `success` | continue the task (verification may be pending; the field says so) |
| `wait` | schedule ONE wake-up at natural reset (minutes + 2 buffer), then exit |
| `no_resets_available` | schedule ONE wake-up at natural reset, then exit |
| `uncertain` | stop; inspect `quota_info` + card inventory; alert the user; never immediately re-submit |
| `error` | stop; report the reason; no blind retries |

- Schedule wake-ups ONLY for statuses that require sleep: `wait` and
  `no_resets_available`. Do not schedule wake-ups for `continue` or
  `success` — keep working.
- If the `agentic-delegation` skill / delegation engine is installed, prefer
  its run+policy machinery (`policy gate`) over hand-rolled cron entries —
  it exists exactly to sleep/wake without polling.
- Otherwise create ONE one-shot wake-up (at/cron) using the harness's
  verified session-resume command, plus the returned `minutes` + a 2-minute
  propagation buffer. Remove the job after it fires. Never busy-poll, never
  minute-by-minute re-checks.
- The agent owns its session identity and the resume command; if the harness
  cannot resume sessions, leave resumable notes instead of claiming a
  wake-up exists.
- Never write the API key into crontab, logs, or the wake-up command; the
  resumed process must read the protected env file itself.
