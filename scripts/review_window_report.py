"""Generate a sanitized window JSON and optional standalone status page."""

import argparse
import json
from pathlib import Path

from apps.oa_finance_preaudit import (
    review_batch_report as batch,
    review_window_report as window,
)


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("selection", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--html", type=Path)
    args = parser.parse_args(argv)
    try:
        report = window.build_window(args.selection)
        selection, roots = window._selection(args.selection)
        if selection["_selection_fingerprint"] != report["selection_fingerprint"]:
            raise ValueError("review_window_selection_changed")
        protected = (*roots, args.selection.expanduser().absolute().parent)
        if selection.get("state_reference"):
            protected = (
                *protected,
                Path(selection["state_reference"]["path"])
                .expanduser()
                .absolute()
                .parent,
            )
        if args.output:
            batch.write_private_report(report, args.output, evidence_roots=protected)
        if args.html:
            batch.write_private_text(
                window.render_window(report), args.html, evidence_roots=protected
            )
        print(json.dumps(report, ensure_ascii=False))
        return 0
    except (OSError, ValueError, RuntimeError):
        print(json.dumps({"error": "review_window_report_failed"}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
