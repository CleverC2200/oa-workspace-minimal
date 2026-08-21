#!/usr/bin/env python3
"""维护人工确认后的通用校对问题集。"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_PATH = Path(__file__).resolve().parent.parent / "references" / "problem-set.json"
CATEGORIES = {
    "latin-digit-confusion",
    "glyph-confusion",
    "semantic-character-substitution",
    "missing-character",
    "extra-character",
    "duplicate-content",
    "term-inconsistency",
    "number-unit-conflict",
    "punctuation-format",
    "grammar-structure",
    "reference-mismatch",
    "mapping-ambiguity",
    "low-confidence-glyph",
}
OUTCOMES = {"confirmed-error", "accepted-variant"}
SOURCE_MODES = {"image-only", "image-excel", "cross-instance", "human-copy"}
MAX_PATTERN_LENGTH = 80


def load_problem_set(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    validate_problem_set(data)
    return data


def validate_problem_set(data: dict[str, Any]) -> None:
    if data.get("schema_version") != 1:
        raise ValueError("schema_version 必须为 1")
    entries = data.get("entries")
    if not isinstance(entries, list):
        raise ValueError("entries 必须为数组")

    seen_ids: set[str] = set()
    required = {
        "id",
        "outcome",
        "category",
        "observed",
        "expected",
        "feature_signature",
        "detection_rules",
        "source_modes",
        "evidence_count",
        "status",
    }
    for index, entry in enumerate(entries):
        missing = required - set(entry)
        if missing:
            raise ValueError(f"entries[{index}] 缺少字段: {sorted(missing)}")
        if entry["id"] in seen_ids:
            raise ValueError(f"重复 id: {entry['id']}")
        seen_ids.add(entry["id"])
        if entry["outcome"] not in OUTCOMES:
            raise ValueError(f"非法 outcome: {entry['outcome']}")
        if entry["category"] not in CATEGORIES:
            raise ValueError(f"非法 category: {entry['category']}")
        if not isinstance(entry["detection_rules"], list) or not entry["detection_rules"]:
            raise ValueError(f"{entry['id']} 的 detection_rules 必须为非空数组")
        if not isinstance(entry["source_modes"], list) or not entry["source_modes"]:
            raise ValueError(f"{entry['id']} 的 source_modes 必须为非空数组")
        if not all(mode in SOURCE_MODES for mode in entry["source_modes"]):
            raise ValueError(f"{entry['id']} 包含非法 source_mode")
        if not isinstance(entry["evidence_count"], int) or entry["evidence_count"] < 1:
            raise ValueError(f"{entry['id']} 的 evidence_count 必须为正整数")
        if entry["status"] != "active":
            raise ValueError(f"{entry['id']} 的 status 必须为 active")


def make_id(outcome: str, category: str, observed: str, expected: str, feature: str) -> str:
    raw = "\u241f".join((outcome, category, observed, expected, feature))
    return "issue-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]


def atomic_write(path: Path, data: dict[str, Any]) -> None:
    temp_path = path.with_suffix(path.suffix + ".tmp")
    temp_path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temp_path, path)


def validate_pattern(name: str, value: str) -> None:
    if not value:
        raise ValueError(f"{name} 不能为空")
    if len(value) > MAX_PATTERN_LENGTH:
        raise ValueError(f"{name} 超过 {MAX_PATTERN_LENGTH} 个字符；请提取更通用的最小特征")


def add_entry(args: argparse.Namespace) -> None:
    if not args.human_confirmed:
        raise ValueError("只有人工确认后的结论才能写入；请提供 --human-confirmed")
    validate_pattern("observed", args.observed)
    if args.outcome == "confirmed-error":
        validate_pattern("expected", args.expected)
    elif len(args.expected) > MAX_PATTERN_LENGTH:
        raise ValueError(f"expected 超过 {MAX_PATTERN_LENGTH} 个字符")
    validate_pattern("feature_signature", args.feature_signature)
    validate_pattern("detection_rule", args.detection_rule)

    path = Path(args.path).resolve()
    data = load_problem_set(path)
    entry_id = make_id(
        args.outcome,
        args.category,
        args.observed,
        args.expected,
        args.feature_signature,
    )
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()

    for entry in data["entries"]:
        if entry["id"] != entry_id:
            continue
        entry["evidence_count"] += 1
        entry["last_confirmed_at"] = now
        if args.detection_rule not in entry["detection_rules"]:
            entry["detection_rules"].append(args.detection_rule)
        if args.source_mode not in entry["source_modes"]:
            entry["source_modes"].append(args.source_mode)
        atomic_write(path, data)
        print(f"updated {entry_id} evidence_count={entry['evidence_count']}")
        return

    data["entries"].append(
        {
            "id": entry_id,
            "outcome": args.outcome,
            "category": args.category,
            "observed": args.observed,
            "expected": args.expected,
            "feature_signature": args.feature_signature,
            "detection_rules": [args.detection_rule],
            "source_modes": [args.source_mode],
            "evidence_count": 1,
            "status": "active",
            "created_at": now,
            "last_confirmed_at": now,
        }
    )
    validate_problem_set(data)
    atomic_write(path, data)
    print(f"added {entry_id}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--path", default=str(DEFAULT_PATH), help="问题集 JSON 路径")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("validate", help="校验问题集结构")

    list_parser = subparsers.add_parser("list", help="列出问题集")
    list_parser.add_argument("--category", choices=sorted(CATEGORIES))

    feature_parser = subparsers.add_parser("features", help="导出不含历史答案的匿名检查特征")
    feature_parser.add_argument("--category", choices=sorted(CATEGORIES))

    add_parser = subparsers.add_parser("add", help="添加或累计人工确认的问题特征")
    add_parser.add_argument("--human-confirmed", action="store_true")
    add_parser.add_argument("--outcome", required=True, choices=sorted(OUTCOMES))
    add_parser.add_argument("--category", required=True, choices=sorted(CATEGORIES))
    add_parser.add_argument("--observed", required=True)
    add_parser.add_argument("--expected", default="")
    add_parser.add_argument("--feature-signature", required=True)
    add_parser.add_argument("--detection-rule", required=True)
    add_parser.add_argument("--source-mode", required=True, choices=sorted(SOURCE_MODES))
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        path = Path(args.path).resolve()
        if args.command == "validate":
            data = load_problem_set(path)
            print(f"valid entries={len(data['entries'])}")
        elif args.command == "list":
            data = load_problem_set(path)
            entries = data["entries"]
            if args.category:
                entries = [item for item in entries if item["category"] == args.category]
            print(json.dumps(entries, ensure_ascii=False, indent=2))
        elif args.command == "features":
            data = load_problem_set(path)
            entries = data["entries"]
            if args.category:
                entries = [item for item in entries if item["category"] == args.category]
            features = [
                {
                    "category": item["category"],
                    "feature_signature": item["feature_signature"],
                    "detection_rules": item["detection_rules"],
                    "outcome": item["outcome"],
                }
                for item in entries
            ]
            print(json.dumps(features, ensure_ascii=False, indent=2))
        elif args.command == "add":
            add_entry(args)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
