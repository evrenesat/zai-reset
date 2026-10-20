# zai-reset project guidance

Z.ai-specific quota tooling only. The generic delegation engine lives in the
private agent-exec project; do not add generic machinery to this repo.

Authoritative plans: [plans/quota-and-delegation-20261019.md](plans/quota-and-delegation-20261019.md)
(split decision: [plans/split-and-migration-20261020.md](plans/split-and-migration-20261020.md)),
integration contract: [plans/run-dir-contract.md](plans/run-dir-contract.md).

## Hard rules

- **Never POST a reset card** during development, tests or smoke. Only
  `zai-quota/scripts/zai_quota.py reset_please` may ever POST, and its first
  live use is a real, owner-authorized low-quota situation.
- Secrets: `ZAI_API_KEY` lives in the env file (default
  `~/.config/zai-quota/env`, 0600). Never print it, never commit it, never
  put it in logs, briefs, reports or unit properties.
- `zai-quota/` is **portable**: no repo-external imports; must work when
  symlinked as a standalone skill in any harness.
- `zaigate/` is **engine-independent**: it reads/writes run directories per
  the run-dir contract only and must never import the delegation engine.
- Stdlib-only Python (3.11+). Every CLI prints exactly one JSON object to
  stdout; diagnostics to stderr. Tests run fully offline.
- This repo creates no systemd units itself (engine units use the `deleg-`
  prefix and belong to the agent-exec side).

## Working discipline

- Implementation goes through flash subagents with checkpoint briefs; the
  main session reviews diffs, runs tests, and integrates.
- Update `DEVLOG.md` as work lands; `ARCHITECTURE.md` when structure changes;
  this file when operating rules change.
- Findings/defects not fixed immediately go to `plans/followups.md`.
