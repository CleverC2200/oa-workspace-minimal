"""Recheck two private archives with an explicit comparison intention."""

import argparse
import json
from pathlib import Path

from apps.oa_finance_preaudit.review_batch_report import write_private_report
from apps.oa_finance_preaudit.review_cost_report import INTENTS, compare_archives


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("baseline", type=Path)
    parser.add_argument("candidate", type=Path)
    parser.add_argument("--intent", required=True, choices=sorted(INTENTS))
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    try:
        report = compare_archives(args.baseline, args.candidate, intent=args.intent)
        if args.output:
            write_private_report(
                report, args.output, evidence_roots=(args.baseline, args.candidate)
            )
        print(json.dumps(report, ensure_ascii=False))
        return 0
    except (OSError, ValueError, RuntimeError):
        print(json.dumps({"error": "review_cost_report_failed"}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
