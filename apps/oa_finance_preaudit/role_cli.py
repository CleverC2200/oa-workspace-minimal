from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
from typing import Sequence

from . import queue_cli


_ROLE_ACTIONS = {
    "controller": (
        "release-manifest",
        "runtime-snapshot",
        "list-production-pending",
        "capture-live-inputs",
        "refresh",
        "claim",
        "status",
        "next-submit",
        "stale",
    ),
    "reviewer": (
        "release-manifest",
        "collect-assignment",
        "collection-status",
        "seal-review",
    ),
    "writer": (
        "release-manifest",
        "submit-frozen-once",
    ),
    "ops": (
        "block-prepared",
        "mark-submit-failed",
        "resolve-submitting-external",
        "requeue-blocked",
        "requeue-blocked-external-manual",
        "recover-stale",
    ),
}

_SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")


def _option_value(arguments: Sequence[str], option: str) -> str:
    for index, value in enumerate(arguments):
        if value == option and index + 1 < len(arguments):
            return str(arguments[index + 1]).strip()
        prefix = option + "="
        if value.startswith(prefix):
            return value[len(prefix) :].strip()
    return ""


def _reviewer_release_failure(arguments: Sequence[str]) -> str:
    assignment_value = _option_value(arguments, "--assignment-file")
    if not assignment_value:
        return ""
    try:
        assignment = json.loads(
            Path(assignment_value).expanduser().read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError):
        return ""
    if not isinstance(assignment, dict):
        return ""
    reviewer_release_sha256 = str(
        assignment.get("reviewer_release_sha256") or ""
    ).strip()
    if not reviewer_release_sha256:
        return "reviewer_release_fingerprint_missing"
    if not _SHA256_HEX.fullmatch(reviewer_release_sha256):
        return "reviewer_release_fingerprint_mismatch"
    return ""


def allowed_actions(role: str) -> tuple[str, ...]:
    try:
        return _ROLE_ACTIONS[str(role)]
    except KeyError as error:
        raise ValueError("role_invalid") from error


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="oa-finance-preaudit-role",
        description="Role-scoped entry point for the finance preaudit runtime.",
    )
    parser.add_argument("role", choices=tuple(_ROLE_ACTIONS))
    parser.add_argument("action")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args, remainder = parser.parse_known_args(argv)
    actions = allowed_actions(args.role)
    if args.action not in actions:
        parser.error(
            f"action {args.action!r} is unavailable for role {args.role!r}; "
            f"choose from {', '.join(actions)}"
        )
    if any(value == "--role" or value.startswith("--role=") for value in remainder):
        parser.error("role is fixed by the selected runtime entry")
    if args.role == "controller" and args.action == "claim":
        reviewer_release_sha256 = _option_value(
            remainder,
            "--reviewer-release-sha256",
        )
        if not _SHA256_HEX.fullmatch(reviewer_release_sha256):
            parser.error(
                "controller claim requires --reviewer-release-sha256 from the "
                "canonical reviewer release manifest"
            )
    if args.role == "reviewer" and args.action != "release-manifest":
        release_failure = _reviewer_release_failure(remainder)
        if release_failure:
            return queue_cli.emit_contract_failure(release_failure)

    forwarded = [args.action, *remainder]
    if args.action == "release-manifest":
        forwarded.extend(("--role", args.role))
    return queue_cli.main(forwarded)


if __name__ == "__main__":
    raise SystemExit(main())
