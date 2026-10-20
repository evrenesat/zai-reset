# DEVLOG

## 2026-10-19 — design and bootstrap

- Owner delegated: review the zai-quota plan, then implement with flash
  subagents; expand into an event-sourced delegation coordinator where the
  main agent sleeps and wakes only on registered events (job finished,
  timers, quota transitions), with zai-quota wired in as event source +
  pre-dispatch gate. zai-quota still ships independently.
- Locked decisions: detached systemd-based sleep/wake; harness-agnostic core
  with adapter command templates (ZCode first, OpenCode/Pi stubs); automated
  reset redemption as the goal with hard gating and dual-usage prevention;
  owner provides `ZAI_API_KEY`; paused-on-quota runs must auto-resume when
  quota returns.
- Moving-target warning: `the agent-exec tree` is being edited by a separate
  review thread; this repo must not read or touch it.
- Wrote master plan (`plans/quota-and-delegation-20261019.md`), copied the
  original zai-quota plan verbatim, bootstrapped docs and the
  `flash-implementer` project agent.
- Facts: zcode 0.16.9 has headless `--prompt`, `--resume`, `-c`, `--cwd`,
  `--mode`, `--json`; user-scope config carries a `model` key (workspace
  pinning to be verified live in CK5); GLM-5.3-Flash available on the coding
  plan; skills install pattern = symlinks from `~/.zcode/skills` and
  `~/.agents/skills` (aflow precedent).

## 2026-10-20 — five-hour quota exhaustion mid-implementation (live dogfood)

- At 08:35 +08 the shared plan quota ran out: the CK4 flash worker died with
  `[1308] Usage limit reached for 5 hour` (reset 11:50 +08); CK1/CK3 flash
  workers stalled on the same limit and were stopped manually. This is the
  exact failure mode the coordinator's pause/resume lifecycle targets.
- CK1 (zai-quota) finished by the main session: all files were already
  written by flash; the 4 remaining test failures were test-authoring bugs
  (missing pre-POST quota re-fetch in one scenario, state-dir pollution
  between exit-code sub-tests, `used=0.4` vs remaining mixup, an
  unverifiable `unused()` assertion). Fixed the tests; implementation was
  plan-correct. 55 tests OK (1 intentional skip: SKILL.md lands in CK6).
  Added the two files the killed agent never wrote (references/,
  README.md).
- CK4 implemented by the main session per plan §B6 (flash unavailable):
  `gate.py` (pure verdict table), `quota.py` (gate_check/redeem/pause/
  resume_check with injectable quota+wake seams), fixtures/fake_zai_quota.py
  offline stub, 34 new tests; wired `quota gate|redeem`, `pause`,
  `resume-check` into the CLI. Coordinator suite: 60 tests OK. Gate smoke
  via the fixture: low_far_cards → `redeem_candidate`, ok → `dispatch_ok`.
- Deviation (documented earlier, now visible in code): `signatures.py` moved
  to CK3 ownership; quota.py does not import it.
- Self-wake scheduled (manual dogfood of the §B5 pattern, ahead of the
  dispatcher existing): `the self-wake timer` fires 03:52:30 UTC
  (≈2 min after the quota reset) and resumes this session headlessly to
  re-dispatch CK3 to flash with bounded retries (max 2), per the prompt
  recorded in the timer unit. Cancel with
  `systemctl stop the self-wake timer`.
- Remaining: CK3 (systemd jobs/wake/selftest), wake↔pause arm_wake
  integration, CK5 live zcode micro E2E + model pinning, CK6 skills/global
  AGENTS.md, CK7 live quota smoke (needs owner key).

## 2026-10-20 — harness credentials investigated; ZAI_API_KEY_CMD added

- Owner asked whether the harness's own auth can supply the quota API key.
  Investigated `~/.zcode/v2/credentials.json` (structure only; values never
  printed): it stores a coding-plan api-key entry, `oauth:zai:access_token`,
  and `zcodejwttoken`. Live read-only GETs against
  `api.z.ai/api/monitor/usage/quota/limit` and the card-list endpoint
  returned business 401 for all three (Bearer), and for raw-Authorization and
  x-api-key variants of the api-key entry. Conclusion: harness credentials
  authorize zcode's chat/coding API family only; the monitor/biz endpoints
  need a dashboard-issued API key (as the referenced source projects assumed).
- Added auth source `ZAI_API_KEY_CMD` (helper command; first stdout line) to
  `resolve_api_key` after env-var and env-file, so any harness-specific
  credential helper can supply the key without persistence. Tests: zai-quota
  now 58 OK (1 intentional skip). Plan addendum §A2 item 7; README updated.
## 2026-10-20 01:16 UTC — live key found; CK7 read-only smoke PASSED

- Owner pointed out `ZAI_API_KEY` is exported in `/root/.bashrc` on the build host (the
  zcode host process simply never sourced it). Extracted without printing,
  installed as the designed env file `~/.config/zai-quota/env` (0600).
- **Live read-only smoke (first real API calls; GET-only, zero POSTs):**
  `quota_info` → ok: used 76.0% / remaining 24.0%, minutes 260,
  next_reset 2026-10-20T05:36:49Z, cards_available 3, earliest card expiry
  2026-11-01 06:54:42. `reset_please --dry-run` → would_status `continue`.
  Pending owner confirmation: numbers vs dashboard (also resolves the
  naive-expiry timezone question).
- Live data shows the five-hour window rolled at ~00:36 UTC — quota was
  already available again, so waiting for the self-wake timer was pointless:
  CK3 was dispatched immediately to the `flash-implementer` project agent
  (now hot-loaded, full file tools — no heredoc constraint), and
  `the self-wake timer` was cancelled. Note: the timer's NEXT had
  drifted 03:52→04:13 UTC between observations, cause unknown, now moot.
