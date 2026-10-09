"""Fixed production review settings and read-only repository drift checks."""
from __future__ import annotations

import ast
from collections import Counter
from pathlib import Path
import re
import shlex

MODEL = "gpt-6.1-sol"
REASONING_EFFORT = "medium"
MODEL_PROVIDER = "openai"
SERVICE_TIER = "default"
INITIAL_CAPACITY = 3
MAX_CAPACITY = 6
FINANCE_WORKFLOWS = (
    "travel_expense", "business_entertainment", "business_support_fee",
    "small_marketing_fee", "tpm_small_marketing_fee", "general_expense",
)
REPOSITORY_INPUTS = (
    "config/automations/oa-skill-prompt.txt", "docs/runbooks/hourly-production.md",
    "apps/oa_finance_preaudit/production_config.py",
    "apps/oa_finance_preaudit/review_failure_evidence.py",
    "apps/oa_finance_preaudit/codex_review_worker.py",
    "scripts/finance_preaudit_concurrency_probe.py",
)


def snapshot() -> dict:
    return {
        "model": MODEL, "reasoning_effort": REASONING_EFFORT,
        "model_provider": MODEL_PROVIDER, "requested_service_tier": SERVICE_TIER,
        "initial_capacity": INITIAL_CAPACITY, "max_capacity": MAX_CAPACITY,
        "workflows": list(FINANCE_WORKFLOWS),
    }


def _text_findings(text: str, source: str) -> list[str]:
    findings = []
    if re.search(r"DeepSeek\s*独立配置|业务审核仍使用独立\s*DeepSeek|不能保持\s*DeepSeek|requires_openai_auth\s*=\s*false", text):
        findings.append(f"production_config_provider_instruction_drift:{source}")
    for key, expected in (("model", MODEL), ("reasoning_effort", REASONING_EFFORT),
                          ("model_provider", MODEL_PROVIDER), ("service_tier", SERVICE_TIER)):
        values = re.findall(rf"\b{key}=[\"']?([a-zA-Z0-9._-]+)", text)
        if not values and source == "automation":
            if key == "model":
                values = re.findall(r"原生 OpenAI ([a-zA-Z0-9._-]+)", text)
            elif key == "reasoning_effort":
                values = re.findall(r"推理\s+([a-zA-Z]+)", text)
            elif key == "model_provider" and "原生 OpenAI " in text:
                values = ["openai"]
        if values != [expected]:
            findings.append(f"production_config_{key}_drift:{source}")
    commands = [line for line in text.splitlines() if "-m scripts.finance_preaudit_run " in line]
    if len(commands) != 1:
        return findings + [f"production_config_command_invalid:{source}"]
    try:
        tokens = shlex.split(commands[0])
        arguments = tokens[tokens.index("--") + 1:]
    except (ValueError, IndexError):
        return findings + [f"production_config_command_invalid:{source}"]
    for option, expected in (("--capacity", str(INITIAL_CAPACITY)), ("--max-capacity", str(MAX_CAPACITY))):
        values = [arguments[index + 1] for index, value in enumerate(arguments[:-1]) if value == option]
        if values != [expected] or arguments[-1:] == [option]:
            findings.append(f"production_config_capacity_drift:{source}")
            break
    workflows = [arguments[index + 1] for index, value in enumerate(arguments[:-1]) if value == "--workflow"]
    if sorted(workflows) != sorted(FINANCE_WORKFLOWS) or arguments[-1:] == ["--workflow"]:
        findings.append(f"production_config_workflows_drift:{source}")
    if arguments.count("--rolling") != 1 or arguments.count("--adaptive") != 1 or "--fixed-capacity" in arguments:
        findings.append(f"production_config_command_invalid:{source}")
    expected_arguments = ["--rolling", "--adaptive", "--capacity", str(INITIAL_CAPACITY),
                          "--max-capacity", str(MAX_CAPACITY), "--confirm-write", "YES",
                          "--recover-confirmed-no-write-once", "YES"]
    for workflow in FINANCE_WORKFLOWS:
        expected_arguments.extend(("--workflow", workflow))
    if (tokens[:4] != [".venv/bin/python", "-m", "scripts.finance_preaudit_run", "--owner-task-id"]
            or len(tokens[:tokens.index("--")]) != 5 or Counter(arguments) != Counter(expected_arguments)):
        findings.append(f"production_config_command_invalid:{source}")
    return findings


def _source_findings(root: Path) -> list[str]:
    sources = {}
    for relative in REPOSITORY_INPUTS[2:]:
        try:
            sources[Path(relative).stem] = ast.parse((root / relative).read_text(encoding="utf-8"))
        except (OSError, UnicodeError, SyntaxError):
            return ["production_config_source_invalid"]
    assignments = {
        name: {target.id: node.value for node in tree.body if isinstance(node, ast.Assign)
               for target in node.targets if isinstance(target, ast.Name)}
        for name, tree in sources.items()
    }
    findings = []
    for key in ("MODEL", "REASONING_EFFORT", "MODEL_PROVIDER", "SERVICE_TIER",
                "INITIAL_CAPACITY", "MAX_CAPACITY", "FINANCE_WORKFLOWS"):
        try:
            actual = ast.literal_eval(assignments["production_config"][key])
        except (KeyError, ValueError, TypeError):
            actual = None
        if actual != globals()[key]:
            findings.append("production_config_defaults_drift")
    imports = {alias.name for node in sources["review_failure_evidence"].body
               if isinstance(node, ast.ImportFrom) and node.module == "production_config" and node.level == 1
               for alias in node.names if alias.asname is None}
    identity_keys = {"MODEL", "REASONING_EFFORT", "MODEL_PROVIDER", "SERVICE_TIER"}
    if not identity_keys.issubset(imports) or identity_keys.intersection(assignments["review_failure_evidence"]):
        findings.append("production_config_reviewer_drift")
    for key in ("MODEL", "REASONING_EFFORT"):
        value = assignments["codex_review_worker"].get(key)
        if not (isinstance(value, ast.Attribute) and isinstance(value.value, ast.Name)
                and value.value.id == "review_failure_evidence" and value.attr == key):
            findings.append("production_config_reviewer_drift")
    command = next((node for node in sources["codex_review_worker"].body
                    if isinstance(node, ast.FunctionDef) and node.name == "_codex_command"), None)
    arguments = _native_arguments(command)
    literals = [node.value if isinstance(node, ast.Constant) else None for node in arguments]
    settings = [arguments[index + 1] for index, value in enumerate(literals[:-1]) if value == "--config"]
    strings = [node.value for node in settings if isinstance(node, ast.Constant) and isinstance(node.value, str)]
    effort = ast.parse('f\'model_reasoning_effort="{REASONING_EFFORT}"\'', mode="eval").body
    models = [arguments[index + 1] for index, value in enumerate(literals[:-1]) if value == "--model"]
    if (literals.count("--ignore-user-config") != 1
            or [value for value in strings if value.startswith("model_provider=")] != ['model_provider="openai"']
            or [value for value in strings if value.startswith("service_tier=")] != ['service_tier="default"']
            or any(value.startswith("model_reasoning_effort=") for value in strings)
            or sum(ast.dump(node) == ast.dump(effort) for node in settings) != 1
            or len(models) != 1 or not isinstance(models[0], ast.Name) or models[0].id != "MODEL"):
        findings.append("production_config_native_settings_drift")
    workflows = assignments["finance_preaudit_concurrency_probe"].get("ROLLING_WORKFLOW_KEYS")
    expected = ast.parse("frozenset(production_config.FINANCE_WORKFLOWS)", mode="eval").body
    if workflows is None or ast.dump(workflows) != ast.dump(expected):
        findings.append("production_config_workflows_drift:source")
    return findings


def _native_arguments(command: ast.FunctionDef | None) -> list[ast.expr]:
    if command is None:
        return []
    if any((isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del))
            and node.id in {'MODEL', 'REASONING_EFFORT'})
           or (isinstance(node, ast.arg) and node.arg in {'MODEL', 'REASONING_EFFORT'})
           for node in ast.walk(command)):
        return []
    if any((alias.asname or alias.name.split('.')[0]) in {'MODEL', 'REASONING_EFFORT'}
           for node in ast.walk(command) if isinstance(node, (ast.Import, ast.ImportFrom))
           for alias in node.names):
        return []
    returns = [node for node in ast.walk(command) if isinstance(node, ast.Return)]
    if len(returns) != 1 or returns[0] is not command.body[-1]:
        return []
    value = returns[0].value
    if isinstance(value, ast.List):
        return value.elts
    if not isinstance(value, ast.Name) or value.id != 'command':
        return []
    definitions = [node for node in command.body if isinstance(node, ast.Assign)
                   and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name)
                   and node.targets[0].id == 'command' and isinstance(node.value, ast.List)]
    if len(definitions) != 1:
        return []
    index = command.body.index(definitions[0])
    before = command.body[:index]
    if any(isinstance(node, ast.Name) and node.id == 'command'
           for statement in before for node in ast.walk(statement)):
        return []
    # Only the existing optional reading settings may be inserted into this list.
    allowed = [ast.dump(node) for node in ast.parse('''
if tool_output_token_limit is not None:
    command[2:2] = ["--config", f"tool_output_token_limit={tool_output_token_limit}"]
if model_instructions_file is not None:
    command[2:2] = ["--config", "model_instructions_file=" + json.dumps(str(model_instructions_file)),
                    "--config", "project_doc_max_bytes=0"]
''').body]
    mutations = [ast.dump(node) for node in command.body[index + 1:-1]]
    if mutations != [node for node in allowed if node in mutations]:
        return []
    return definitions[0].value.elts


def repository_findings(root: Path) -> list[str]:
    """Inspect the target's tracked contract text without importing target code."""
    findings = []
    for relative, label in (("config/automations/oa-skill-prompt.txt", "template"),
                            ("docs/runbooks/hourly-production.md", "runbook")):
        try:
            text = (Path(root) / relative).read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            findings.append(f"production_config_input_invalid:{label}")
            continue
        findings.extend(_text_findings(text, label))
    findings.extend(_source_findings(Path(root)))
    return sorted(set(findings))


def automation_findings(config: dict, *, production_project_id: str, expected_schedule: str,
                        production_root: Path | None = None) -> list[str]:
    """Check native automation metadata; return codes without disclosing its contents."""
    if not isinstance(config, dict) or not production_project_id or not expected_schedule:
        return ["production_config_automation_input_invalid"]
    findings = []
    if config.get("model") != MODEL or config.get("reasoning_effort") != REASONING_EFFORT:
        findings.append("production_config_automation_model_drift")
    target = config.get("target")
    if (not isinstance(target, dict) or target.get("type") != "project"
            or target.get("project_id") != production_project_id
            or config.get("kind") != "cron" or config.get("execution_environment") != "local"):
        findings.append("production_config_automation_target_drift")
    if production_root is not None and config.get("cwds") != [str(Path(production_root))]:
        findings.append("production_config_automation_target_drift")
    if config.get("rrule") != expected_schedule:
        findings.append("production_config_automation_schedule_drift")
    prompt = config.get("prompt")
    if not isinstance(prompt, str):
        findings.append("production_config_automation_input_invalid")
    else:
        findings.extend(_text_findings(prompt, "automation"))
    return sorted(set(findings))
