"""zaigate CLI (the engine's COORD_POLICY_CMD target; run-dir contract).

Every subcommand takes a RUN DIRECTORY and prints exactly one JSON object.
Exit codes: 0 valid decision, 2 error, 3 needs-user/uncertain.
"""

import argparse
import json
import sys

from . import policy


def _gate(args):
    result = policy.gate_check(args.run_dir,
                               work_remaining=args.work_remaining)
    print(json.dumps(result, ensure_ascii=False, default=str))
    return 3 if result["verdict"]["verdict"] == "needs_user" else 0


def _redeem(args):
    result = policy.redeem(args.run_dir, work_remaining=args.work_remaining)
    print(json.dumps(result, ensure_ascii=False, default=str))
    status = result.get("status")
    if result.get("executed") and status in ("uncertain", "error"):
        return 3 if status == "uncertain" else 2
    return 0


def _pause(args):
    result = policy.pause(args.run_dir, reason=args.reason,
                          eta_minutes=args.eta_minutes, note=args.note)
    print(json.dumps(result, ensure_ascii=False))
    return 0


def _resume_check(args):
    result = policy.resume_check(args.run_dir)
    print(json.dumps(result, ensure_ascii=False, default=str))
    if result.get("parked"):
        return 3
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="python3 -m zaigate",
        description="Z.ai quota policy for delegation runs "
                    "(plans/run-dir-contract.md)")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("gate", help="fresh quota read + §B6 verdict")
    p.add_argument("run_dir", metavar="RUN_DIR")
    p.add_argument("--work-remaining", type=int, default=1,
                   dest="work_remaining")
    p.set_defaults(func=_gate)

    p = sub.add_parser("redeem", help="gated reset_please execution")
    p.add_argument("run_dir", metavar="RUN_DIR")
    p.add_argument("--work-remaining", type=int, default=1,
                   dest="work_remaining")
    p.set_defaults(func=_redeem)

    p = sub.add_parser("pause", help="park a run (quota interruption)")
    p.add_argument("run_dir", metavar="RUN_DIR")
    p.add_argument("--reason", choices=["quota", "manual"], default="quota")
    p.add_argument("--eta-minutes", type=int, default=None,
                   dest="eta_minutes")
    p.add_argument("--note", default=None)
    p.set_defaults(func=_pause)

    p = sub.add_parser("resume-check",
                       help="re-gate a paused_quota run and resume/park")
    p.add_argument("run_dir", metavar="RUN_DIR")
    p.set_defaults(func=_resume_check)

    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except (ValueError, OSError, KeyError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    sys.exit(main())
