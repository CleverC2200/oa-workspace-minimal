"""Generate a sanitized single-batch report from an existing private archive."""

import argparse
import json
from pathlib import Path

from apps.oa_finance_preaudit.review_batch_report import (
    build_report,
    write_private_report,
)


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("archive", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    try:
        report = build_report(args.archive)
        if args.output:
            write_private_report(report, args.output, evidence_roots=(args.archive,))
        print(json.dumps(report, ensure_ascii=False))
        return 0
    except (OSError, ValueError, RuntimeError):
        print(json.dumps({"error": "review_batch_report_failed"}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
