# zai-quota

Z.ai Coding Plan five-hour quota helper: read-only inspection plus hard-gated
redemption of five-hour reset cards. Portable Agent Skill + stdlib Python CLI
(ZCode / Pi / OpenCode); no repo-external dependencies.

## Usage

```sh
python3 scripts/zai_quota.py quota_info
python3 scripts/zai_quota.py reset_please            # may POST; see gating
python3 scripts/zai_quota.py reset_please --dry-run  # GET-only simulation
```

Exactly one JSON object on stdout per run (`schema_version` 1); diagnostics go
to stderr and never contain secrets.

## Auth

`ZAI_API_KEY` from the process environment, else the first `ZAI_API_KEY=` line
of the env file at `$ZAI_QUOTA_ENV_FILE` (default `~/.config/zai-quota/env`,
recommended mode 0600), else the first stdout line of the command in
`$ZAI_API_KEY_CMD`. The key is never printed or logged. Note: the ZCode
harness's internally stored credentials do **not** authorize these business
endpoints (verified read-only: business 401) — a dashboard-issued Z.ai API key
is required.

## Statuses and exit codes

| status | exit | meaning |
| --- | --- | --- |
| `continue` / `wait` / `no_resets_available` / `success` | 0 | valid decision |
| `partial` (quota_info) | 0 | quota read ok, card inventory unknown (`cards_available: null`) |
| `dry_run` | 0 | simulation only; zero POSTs |
| `uncertain` | 3 | POST outcome unknown; never retransmitted automatically |
| `error` | 2 | missing auth, invalid data, business rejection, failed reads |

## Gating before any POST

Low quota (<=1%), natural reset >60 min away, a usable card, no unresolved
pending record, and — addendum — no successful redemption already recorded in
the current natural window (`ledger.jsonl`, one per window, 120 s propagation
margin). A pending uncertain POST is resolved by read-only verification only.
See [plans/zai-quota-implementation-plan.md](../plans/zai-quota-implementation-plan.md)
and the addendum in [plans/quota-and-delegation-20261019.md](../plans/quota-and-delegation-20261019.md) §A2.

**Policy: no POST during development, tests or smoke.** The first live
redemption happens only in a real, owner-authorized low-quota situation.

## State

`--state-dir` override for tests; default
`~/.local/state/zai-quota/<sha256(key)[:16]>/` (0700/0600): exclusive `lock`,
`pending.json`, append-only `ledger.jsonl`.

## Tests

```sh
cd <repo root> && python3 -m unittest discover -s zai-quota/tests -v
```

Fully offline (injected transport + frozen clock). One test stays skipped
until `SKILL.md` lands (checkpoint CK6).
