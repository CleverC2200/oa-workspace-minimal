"""Prepare or consume an explicitly authorized, single-pair preview experiment."""

import argparse
import json
from pathlib import Path

from apps.oa_finance_preaudit import review_experiment as experiment


def main(argv=None):
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    freeze = commands.add_parser("freeze")
    for key in ("baseline", "candidate", "plan"):
        freeze.add_argument(key, type=Path)
    freeze.add_argument("--repository", required=True, type=Path)
    freeze.add_argument("--reviewer", required=True, type=Path)
    freeze.add_argument("--purpose", required=True, choices=("effect", "diagnostic"))
    freeze.add_argument("--timeout-seconds", required=True, type=int)
    freeze.add_argument("--maximum-cli-starts", required=True, type=int)
    run = commands.add_parser("run")
    run.add_argument("plan", type=Path)
    run.add_argument("--authorization", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "freeze":
            experiment.freeze_pair(args.baseline, args.candidate, args.plan, repository=args.repository, reviewer=args.reviewer, purpose=args.purpose, timeout_seconds=args.timeout_seconds, maximum_cli_starts=args.maximum_cli_starts)
            result = {"status": "prepared", "cli_starts": 0, "execution_authority": "not_granted_by_preparation"}
        else:
            result = experiment.run_pair(args.plan, args.authorization)
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result["status"] in {"prepared", "observed"} else 1
    except (OSError, ValueError, KeyError, TypeError):
        print(json.dumps({"status": "rejected", "error": "review_experiment_invalid"}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
