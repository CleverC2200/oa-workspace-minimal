from __future__ import annotations

import argparse
import ast
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
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


def check_test_names(paths: Iterable[Path]) -> list[str]:
    """Reject test definitions hidden by a later definition in the same scope."""
    findings = []
    for path in paths:
        if not path.name.startswith('test_') or path.suffix != '.py':
            continue
        try:
            tree = ast.parse(path.read_text(encoding='utf-8'))
        except (OSError, UnicodeError, SyntaxError):
            findings.append(f'test_source_invalid:{path.name}')
            continue
        scopes = [(tree.body, '')]
        while scopes:
            body, scope = scopes.pop()
            names = set()
            pending = list(body)
            while pending:
                node = pending.pop()
                if isinstance(node, ast.ClassDef):
                    scopes.append((node.body, scope + node.name + '.'))
                elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    if node.name.startswith('test_'):
                        if node.name in names:
                            findings.append(f'duplicate_test_name:{path.name}:{scope}{node.name}')
                        names.add(node.name)
                else:
                    # Control blocks retain their enclosing Python scope.
                    pending.extend(ast.iter_child_nodes(node))
    return sorted(findings)


def check_payment_rule_ownership(
    path: Path = PROJECT_ROOT / 'apps/oa_finance_preaudit/codex_review_worker.py',
) -> list[str]:
    """Keep the previously duplicated relationship branch in its Skill owner."""
    owner = '.agents/skills/oa-finance-preaudit/references/payment-screenshot-exemption.md'
    try:
        tree = ast.parse(path.read_text(encoding='utf-8'))
    except (OSError, SyntaxError):
        return [f'{path.name}:payment_rule_source_invalid:fix Worker source before checking {owner}']
    findings = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if any(term in node.value for term in ('人员关系', '委托代付款', '委托收款')):
                findings.append(
                    f'{path.name}:{node.lineno}:payment_relationship_rule_in_worker:'
                    f'load {owner}; remove the duplicate relationship instruction'
                )
    return sorted(findings)


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


def check_yaml(paths: Iterable[Path]) -> list[str]:
    import yaml

    class Loader(yaml.SafeLoader):
        pass

    # YAML 1.2 booleans: GitHub Actions' "on" remains a mapping key.
    Loader.yaml_implicit_resolvers = {
        key: [(tag, pattern) for tag, pattern in values if tag != 'tag:yaml.org,2002:bool']
        for key, values in Loader.yaml_implicit_resolvers.items()
    }
    Loader.add_implicit_resolver('tag:yaml.org,2002:bool', re.compile(r'^(?:true|false)$', re.I), list('tTfF'))

    def unique_mapping(loader, node, deep=False):
        result = {}
        for key_node, value_node in node.value:
            key = loader.construct_object(key_node, deep=deep)
            if key in result:
                raise ValueError('yaml_duplicate_key')
            result[key] = loader.construct_object(value_node, deep=deep)
        return result
    Loader.add_constructor('tag:yaml.org,2002:map', unique_mapping)
    findings = []
    for path in paths:
        if path.suffix.lower() not in {'.yml', '.yaml'}:
            continue
        try:
            value = yaml.load(path.read_text(encoding='utf-8'), Loader=Loader)
            if not isinstance(value, dict):
                raise ValueError('yaml_mapping_required')
        except (OSError, yaml.YAMLError, ValueError, TypeError):
            findings.append(f'yaml_invalid:{path.name}')
            continue
        if path.parent.name == 'ISSUE_TEMPLATE' and path.name != 'config.yml':
            findings.extend(check_issue_form(value, path.name))
    return sorted(findings)


def check_issue_form(form: dict, name: str) -> list[str]:
    invalid = [f'issue_form_invalid:{name}']
    if (not isinstance(form.get('name'), str) or not form['name'].strip()
            or not isinstance(form.get('description'), str) or not form['description'].strip()
            or not isinstance(form.get('body'), list) or not form['body']):
        return invalid
    ids = set()
    for field in form['body']:
        if not isinstance(field, dict):
            return invalid
        kind = field.get('type')
        attributes = field.get('attributes')
        if (not isinstance(kind, str) or kind not in {'markdown', 'input', 'textarea', 'dropdown', 'checkboxes'}
                or not isinstance(attributes, dict)):
            return invalid
        if kind == 'markdown':
            if not isinstance(attributes.get('value'), str):
                return invalid
            continue
        field_id = field.get('id')
        if (not isinstance(field_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]+', field_id)
                or field_id in ids or not isinstance(attributes.get('label'), str)
                or not attributes['label'].strip()):
            return invalid
        ids.add(field_id)
        validations = field.get('validations', {})
        if (not isinstance(validations, dict)
                or set(validations) - {'required'}
                or ('required' in validations and type(validations['required']) is not bool)):
            return invalid
        if kind in {'dropdown', 'checkboxes'}:
            options = attributes.get('options')
            if not isinstance(options, list) or not options:
                return invalid
            if kind == 'dropdown' and (not all(isinstance(item, str) and item.strip() for item in options)
                                       or len(set(options)) != len(options)):
                return invalid
            if kind == 'checkboxes' and any(not isinstance(item, dict) or not isinstance(item.get('label'), str)
                    or ('required' in item and type(item['required']) is not bool) for item in options):
                return invalid
    return []


def check_review_schemas() -> list[str]:
    from jsonschema import Draft202012Validator
    from jsonschema.exceptions import SchemaError
    from apps.oa_finance_preaudit import codex_review_worker as worker, flow_engine, packaging_review

    findings = []
    with tempfile.TemporaryDirectory(prefix='oa-schema-check-') as temporary:
        for workflow in worker._FINANCE_RULES:
            def material(request, _attempt, key=workflow):
                return {'schema_version': 1, 'kind': 'preaudit_material', 'request_id': request,
                        'workflow_key': key, 'workflow_name': 'Offline schema check', 'declared_fields': [{'field_name': 'period', 'value': '2026-08'}],
                        'resources': [], 'prepared': {'images': [], 'structured_contents': [], 'manifest': []},
                        'authority_facts': [], 'warnings': []}
            engine = flow_engine.FlowEngine(root=Path(temporary) / workflow,
                discover_candidates=lambda key: [{'request_id': 'offline-case', 'workflow_key': key,
                                                  'received_at': '2026-09-01T00:00:00+00:00'}],
                collect_material=material)
            try:
                action = engine.next_action(workflow)
                schema = worker._decision_schema(Path(action['action_file']))
                Draft202012Validator.check_schema(schema)
                packaging_review.check_provider_schema(schema)
            except (ValueError, KeyError, SchemaError):
                findings.append(f'review_schema_invalid:{workflow}')
    try:
        packaging_review.draft_schema({
            'rule_version': 'packaging_layout_approval:2',
            'items': [{'item_key': key} for key in flow_engine._FLOW_SPECS['packaging_layout_approval']['item_keys']],
            'packaging_visual_contract': {'page_refs': ['material:prepared.images.offline.page-1']},
        })
    except (ValueError, KeyError, SchemaError):
        findings.append('review_schema_invalid:packaging_layout_approval')
    return findings


def _event_body(path: Path) -> object:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload["pull_request"]["body"]
    except (OSError, json.JSONDecodeError, KeyError, TypeError):
        return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="repository-quality")
    parser.add_argument("check", choices=("skill", "hygiene", "format", "links", "pr", "yaml", "schema", "lint", "environment", "production-config", "payment-rules", "test-names"))
    parser.add_argument("--event", type=Path)
    args = parser.parse_args(argv)
    files = tracked_files()
    if args.check == "skill":
        findings = check_skill()
    elif args.check == 'test-names':
        findings = check_test_names(files)
    elif args.check == 'payment-rules':
        findings = check_payment_rule_ownership()
    elif args.check == "hygiene":
        findings = check_hygiene(files)
    elif args.check == "format":
        findings = check_format(files)
    elif args.check == "links":
        findings = check_markdown_links(files)
    elif args.check == "yaml":
        findings = check_yaml(files)
    elif args.check == "schema":
        findings = check_review_schemas()
    elif args.check == "environment":
        from scripts.dev_environment import check_configuration
        findings = check_configuration(PROJECT_ROOT)
    elif args.check == "production-config":
        from apps.oa_finance_preaudit.production_config import repository_findings
        findings = repository_findings(PROJECT_ROOT)
    elif args.check == "lint":
        return subprocess.call([sys.executable, '-m', 'ruff', 'check', 'apps', 'scripts', 'tests'], cwd=PROJECT_ROOT)
    else:
        findings = check_pr_body(_event_body(args.event)) if args.event else ["pr_event_missing"]
    if findings:
        print("\n".join(findings))
        return 1
    print(f"{args.check}: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
