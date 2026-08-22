from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import subprocess
from typing import Iterable


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SKILL_PATH = PROJECT_ROOT / ".agents/skills/oa-finance-preaudit/SKILL.md"
TEXT_SUFFIXES = {".json", ".md", ".py", ".sh", ".toml", ".txt", ".yaml", ".yml"}
DATABASE_SUFFIXES = {".db", ".duckdb", ".sqlite", ".sqlite3"}
SECRET_PATTERNS = {
    "private_key": re.compile(r"-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----"),
    "github_token": re.compile(r"(?:ghp_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})"),
    "openai_key": re.compile(r"sk-(?:proj-)?[A-Za-z0-9_-]{20,}"),
    "aws_access_key": re.compile(r"AKIA[0-9A-Z]{16}"),
    "credential_uri": re.compile(r"(?:postgres(?:ql)?|mysql|mssql)://[^:/\s]+:[^@\s]+@"),
}
LINK_PATTERN = re.compile(r"!?\[[^\]]*\]\(([^)]+)\)")


def tracked_files(root: Path = PROJECT_ROOT) -> list[Path]:
    result = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=root,
        check=True,
        capture_output=True,
    )
    paths = [
        root / item.decode("utf-8")
        for item in result.stdout.split(b"\0")
        if item
    ]
    return [path for path in paths if path.is_file()]


def check_skill(path: Path = SKILL_PATH) -> list[str]:
    try:
        content = Path(path).read_text(encoding="utf-8")
    except OSError:
        return ["skill_missing"]
    match = re.match(r"^---\n(.*?)\n---(?:\n|$)", content, re.DOTALL)
    if not match:
        return ["skill_frontmatter_invalid"]
    fields = {}
    for line in match.group(1).splitlines():
        if not line.strip() or line.startswith((" ", "\t")) or ":" not in line:
            return ["skill_frontmatter_invalid"]
        key, value = line.split(":", 1)
        if key in fields:
            return ["skill_frontmatter_duplicate"]
        fields[key] = value.strip().strip('"\'')
    allowed = {"name", "description", "license", "allowed-tools", "metadata"}
    if set(fields) - allowed:
        return ["skill_frontmatter_unknown_field"]
    name = fields.get("name", "")
    description = fields.get("description", "")
    if (
        not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", name)
        or len(name) > 64
        or not description
        or len(description) > 1024
        or "<" in description
        or ">" in description
    ):
        return ["skill_frontmatter_invalid"]
    if re.search(r"(?m)^[ ]{0,3}\[TODO:[^\n]*\][ \t]*$", content[match.end():]):
        return ["skill_todo_placeholder"]
    return []


def check_hygiene(paths: Iterable[Path]) -> list[str]:
    findings = []
    for path in paths:
        if path.suffix.lower() in DATABASE_SUFFIXES:
            findings.append(f"tracked_database:{path.name}")
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for name, pattern in SECRET_PATTERNS.items():
            if pattern.search(text):
                findings.append(f"{name}:{path.name}")
    return sorted(findings)


def check_format(paths: Iterable[Path]) -> list[str]:
    findings = []
    for path in paths:
        if path.suffix.lower() not in TEXT_SUFFIXES:
            continue
        try:
            data = path.read_bytes()
        except OSError:
            findings.append(f"unreadable:{path.name}")
            continue
        if b"\r\n" in data:
            findings.append(f"crlf:{path.name}")
        if data and not data.endswith(b"\n"):
            findings.append(f"missing_final_newline:{path.name}")
        try:
            lines = data.decode("utf-8").splitlines()
        except UnicodeDecodeError:
            continue
        if any(line.endswith((" ", "\t")) for line in lines):
            findings.append(f"trailing_whitespace:{path.name}")
    return sorted(findings)


def check_markdown_links(paths: Iterable[Path], root: Path = PROJECT_ROOT) -> list[str]:
    root = Path(root).resolve(strict=False)
    findings = []
    for path in paths:
        if path.suffix.lower() != ".md":
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            findings.append(f"unreadable:{path.name}")
            continue
        for raw_target in LINK_PATTERN.findall(text):
            target = raw_target.strip().strip("<>").split("#", 1)[0]
            if not target or re.match(r"^(?:https?://|mailto:)", target):
                continue
            resolved = (path.parent / target).resolve(strict=False)
            if resolved != root and root not in resolved.parents:
                findings.append(f"outside_root:{path.name}")
            elif not resolved.exists():
                findings.append(f"missing_link:{path.name}:{target}")
    return sorted(findings)


def check_pr_body(body: object) -> list[str]:
    if not isinstance(body, str):
        return ["pr_body_missing"]
    issue_links = re.findall(
        r"(?im)^\s*(?:refs?|close[sd]?|fixe[sd]?|resolve[sd]?)\s+#\d+\s*$",
        body,
    )
    findings = []
    if len(issue_links) != 1:
        findings.append("pr_requires_one_issue")
    for heading, code in (("## 验证", "pr_validation_missing"), ("## 风险与回退", "pr_risk_missing")):
        if heading not in body:
            findings.append(code)
    return findings


def _event_body(path: Path) -> object:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload["pull_request"]["body"]
    except (OSError, json.JSONDecodeError, KeyError, TypeError):
        return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="repository-quality")
    parser.add_argument("check", choices=("skill", "hygiene", "format", "links", "pr"))
    parser.add_argument("--event", type=Path)
    args = parser.parse_args(argv)
    files = tracked_files()
    if args.check == "skill":
        findings = check_skill()
    elif args.check == "hygiene":
        findings = check_hygiene(files)
    elif args.check == "format":
        findings = check_format(files)
    elif args.check == "links":
        findings = check_markdown_links(files)
    else:
        findings = check_pr_body(_event_body(args.event)) if args.event else ["pr_event_missing"]
    if findings:
        print("\n".join(findings))
        return 1
    print(f"{args.check}: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
