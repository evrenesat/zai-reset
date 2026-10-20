# zai-reset

Z.ai Coding Plan quota tooling: read-only inspection of the five-hour usage
window, hard-gated redemption of five-hour reset cards, and the Z.ai quota
policy that plugs into event-sourced agent delegation.

## What is here

| Path | What |
| --- | --- |
| `zai-quota/` | Portable Agent Skill + stdlib Python CLI: `quota_info` (read-only) and `reset_please` (gated; `--dry-run` supported). Works standalone in ZCode, Pi and OpenCode. |
| `zaigate/` | Z.ai quota **policy** package implementing the [run-dir contract](plans/run-dir-contract.md): the dispatch verdict table, gated redemption orchestration, pause-on-exhaustion / resume-on-recovery rules. Invoked by the delegation engine via `COORD_POLICY_CMD="python3 -m zaigate"`. |
| `plans/` | Binding plans, the verbatim original zai-quota plan, and the run-dir contract. |

The **generic event-sourced delegation engine** (event journal, detached
systemd jobs, coalescing wake dispatcher, sleeping coordinator protocol,
harness adapters) is not in this repo: it lives in the private agent-exec
project (`delegation` module) per the 2026-10-20 split decision. This repo
carries only the Z.ai-specific parts.

## Quickstart

```sh
# quota inspection (read-only)
python3 zai-quota/scripts/zai_quota.py quota_info
# preview a redemption decision without POSTing
python3 zai-quota/scripts/zai_quota.py reset_please --dry-run
# policy verdict for a delegation run directory (engine invokes this)
python3 -m zaigate gate /path/to/run-dir --work-remaining 2
```

Auth: `ZAI_API_KEY` env → env file (`ZAI_QUOTA_ENV_FILE`, default
`~/.config/zai-quota/env`, mode 0600) → `$ZAI_API_KEY_CMD` helper.

## Tests

```sh
python3 -m unittest discover -s zai-quota/tests -v   # 59, offline
python3 -m unittest discover -s zaigate/tests -v     # 31, offline
```

No test touches the network. **No development, test or smoke path POSTs a
reset card** — the first live redemption is deferred to a real, explicitly
authorized low-quota situation.

## Install skills

`scripts/install.sh` symlinks `zai-quota` into `~/.zcode/skills/` and
`~/.agents/skills/`; set `ENGINE_DIR` to also install the delegation
engine's `agentic-delegation` skill.

## Status

See [DEVLOG.md](DEVLOG.md) and [ARCHITECTURE.md](ARCHITECTURE.md).
Not tracking git history before the first public snapshot (2026-10-20).
