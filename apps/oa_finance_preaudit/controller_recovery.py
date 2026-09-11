"""Explicit local handoff recovery; never starts a controller or submits OA."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from . import cli, controller_handoff, runtime_doctor


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--confirm-children-reaped", choices=("YES",), required=True)
    args = parser.parse_args(argv)
    root = Path(os.environ.get("OA_FINANCE_PREAUDIT_FLOW_ROOT") or
                controller_handoff.PROJECT_ROOT / "runtime" / "finance_preaudit_flows")

    def check_live() -> dict:
        return {"live_check": runtime_doctor.status(),
                "reconciliation": cli._live_reconciliation(root)}

    try:
        result = controller_handoff.recover_orphaned_run(
            confirmed_children_reaped=args.confirm_children_reaped == "YES",
            check_live=check_live,
        )
    except (OSError, ValueError, RuntimeError):
        print(json.dumps({"status": "blocked", "error_code": "controller_handoff_recovery_blocked"}))
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
