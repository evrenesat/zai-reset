# Architecture

Two Z.ai-specific packages plus installed skills. The generic delegation
engine is external (private agent-exec project, `delegation` module) — see
the split decision in [plans/split-and-migration-20261020.md](plans/split-and-migration-20261020.md).

## zai-quota

Stdlib Python CLI (`quota_info`, `reset_please [--dry-run]`) + portable
Agent Skill. One JSON object on stdout (`schema_version` 1); statuses
`continue/wait/no_resets_available/success/uncertain/error` (+ `dry_run`,
`partial`); exit codes 0/2/3. Safety stack before any POST: exclusive
per-key flock → pending-operation record (pre-POST, resolves uncertain
outcomes read-only, never retransmits) → per-natural-window redemption
ledger (dual-usage guard, 120 s propagation margin) → decision table
thresholds (1% low, 60 min natural-reset wait). Auth chain: env var → env
file → helper command. No scheduling, no daemon, no session knowledge.

## zaigate

The provider side of the [run-dir contract](plans/run-dir-contract.md):
`gate` (verdict table: `dispatch_ok | wait_natural | watch | wait_no_cards |
redeem_candidate | needs_user`, with `wake_at_epoch_ms` hints the engine
schedules), `redeem` (re-evaluates the gate, then delegates to
`reset_please`; uncertain/error park the run), `pause`, `resume-check`
(bounded rechecks, then park). Owns its contract-side journal writer
(flock/seq/fsync — same rules as the engine, independent code). Imports
nothing from the engine; the engine invokes it via `COORD_POLICY_CMD`.

```
delegation engine (agent-exec)          zaigate (this repo)
  run init / jobs / wakes ── run dir ─▶ policy gate RUN_DIR
  policy hook execs COORD_POLICY_CMD    reads run.json + events.jsonl
  arms wakes from verdict fields        calls zai-quota CLI (reads/POST)
       ▲                                    │ verdict / events
       └──────────── events.jsonl ◀─────────┘ (journal is the contract)
```

## Verification status

| Claim | Status |
| --- | --- |
| zai-quota live read-only smoke (quota + cards + dry-run) | passed 2026-10-20 |
| zai-quota offline suite | 59 green |
| zaigate offline suite (incl. fixture quota CLI) | 31 green |
| Engine suites + real-systemd selftest | green in the agent-exec tree |
| Naive card-expiry timezone | unconfirmed vs dashboard until first real redemption decision |
| First live reset POST | deferred to an owner-authorized low-quota moment |
