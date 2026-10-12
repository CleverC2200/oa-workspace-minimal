"""Fixed production review settings and read-only repository drift checks."""
from __future__ import annotations

import ast
from collections import Counter
import json
import os
from pathlib import Path
import re
import shlex

MODEL = "deepseek-flash"
REASONING_EFFORT = "high"
MODEL_PROVIDER = "oa_deepseek"
SERVICE_TIER = "default"
SCHEDULER_MODEL = "gpt-6.1-sol"
SCHEDULER_REASONING_EFFORT = "medium"
MODEL_CATALOG = "config/deepseek-models.json"
DEFAULT_REVIEW_PROVIDER = "deepseek"
REVIEW_PROVIDER_ENV = "OA_REVIEW_PROVIDER"
INITIAL_CAPACITY = 3
MAX_CAPACITY = 3
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
    MODEL_CATALOG,
)


def review_settings(provider: str | None = None) -> dict:
    """Resolve the provider chosen for one fresh controller process."""
    provider = provider if provider is not None else os.environ.get(REVIEW_PROVIDER_ENV, DEFAULT_REVIEW_PROVIDER)
    if provider == "deepseek":
        return {"provider": provider, "model": MODEL, "reasoning_effort": REASONING_EFFORT,
                "model_provider": MODEL_PROVIDER, "requested_service_tier": SERVICE_TIER}
    if provider == "openai":
        return {"provider": provider, "model": "gpt-6.1-sol", "reasoning_effort": "medium",
                "model_provider": "openai", "requested_service_tier": "default"}
    raise ValueError("approval_review_provider_invalid")


def snapshot() -> dict:
    return {
        "model": MODEL, "reasoning_effort": REASONING_EFFORT,
        "model_provider": MODEL_PROVIDER, "requested_service_tier": SERVICE_TIER,
        "initial_capacity": INITIAL_CAPACITY, "max_capacity": MAX_CAPACITY,
        "workflows": list(FINANCE_WORKFLOWS),
    }


def _text_findings(text: str, source: str) -> list[str]:
    findings = []
    if re.search(r"审核默认使用.*原生 OpenAI|审核进程默认通过原生 OpenAI|使用原生 OpenAI 登录凭据", text):
        findings.append(f"production_config_provider_instruction_drift:{source}")
    commands = [line for line in text.splitlines() if "-m scripts.finance_preaudit_run " in line]
    if len(commands) != 1:
        return findings + [f"production_config_command_invalid:{source}"]
    try:
        tokens = shlex.split(commands[0])
        arguments = tokens[tokens.index("--") + 1:]
        providers = [tokens[index + 1] for index, value in enumerate(tokens[:-1]) if value == "--review-provider"]
        if len(providers) != 1:
            raise ValueError("provider_selection")
        settings = review_settings(providers[0])
    except (ValueError, IndexError):
        return findings + [f"production_config_provider_selection_drift:{source}"]
    for key, expected in (("model", settings["model"]), ("reasoning_effort", settings["reasoning_effort"]),
                          ("model_provider", settings["model_provider"]), ("service_tier", settings["requested_service_tier"])):
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
    for option, expected in (("--capacity", str(INITIAL_CAPACITY)), ("--max-capacity", str(MAX_CAPACITY))):
        values = [arguments[index + 1] for index, value in enumerate(arguments[:-1]) if value == option]
        if values != [expected] or arguments[-1:] == [option]:
            findings.append(f"production_config_capacity_drift:{source}")
            break
    workflows = [arguments[index + 1] for index, value in enumerate(arguments[:-1]) if value == "--workflow"]
    if sorted(workflows) != sorted(FINANCE_WORKFLOWS) or arguments[-1:] == ["--workflow"]:
        findings.append(f"production_config_workflows_drift:{source}")
    if arguments.count("--rolling") != 1 or arguments.count("--fixed-capacity") != 1 or "--adaptive" in arguments:
        findings.append(f"production_config_command_invalid:{source}")
    expected_arguments = ["--rolling", "--fixed-capacity", "--capacity", str(INITIAL_CAPACITY),
                          "--max-capacity", str(MAX_CAPACITY), "--confirm-write", "YES",
                          "--recover-confirmed-no-write-once", "YES"]
    for workflow in FINANCE_WORKFLOWS:
        expected_arguments.extend(("--workflow", workflow))
    if (tokens[:4] != [".venv/bin/python", "-m", "scripts.finance_preaudit_run", "--owner-task-id"]
            or tokens[5:7] != ["--review-provider", settings["provider"]]
            or len(tokens[:tokens.index("--")]) != 7 or Counter(arguments) != Counter(expected_arguments)):
        findings.append(f"production_config_command_invalid:{source}")
    return findings


def _source_findings(root: Path) -> list[str]:
    sources = {}
    for relative in (name for name in REPOSITORY_INPUTS[2:] if name.endswith(".py")):
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
                "INITIAL_CAPACITY", "MAX_CAPACITY", "FINANCE_WORKFLOWS",
                "SCHEDULER_MODEL", "SCHEDULER_REASONING_EFFORT", "MODEL_CATALOG",
                "DEFAULT_REVIEW_PROVIDER", "REVIEW_PROVIDER_ENV"):
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
    active = assignments["codex_review_worker"].get("REVIEW_SETTINGS")
    expected_active = ast.parse("production_config.review_settings()", mode="eval").body
    if active is None or ast.dump(active) != ast.dump(expected_active):
        findings.append("production_config_reviewer_drift")
    for key, field in (("MODEL", "model"), ("REASONING_EFFORT", "reasoning_effort")):
        value = assignments["codex_review_worker"].get(key)
        expected = ast.parse(f"REVIEW_SETTINGS[{field!r}]", mode="eval").body
        if value is None or ast.dump(value) != ast.dump(expected):
            findings.append("production_config_reviewer_drift")
    command = next((node for node in sources["codex_review_worker"].body
                    if isinstance(node, ast.FunctionDef) and node.name == "_codex_command"), None)
    returns = [node.value for node in command.body if isinstance(node, ast.Return)] if command else []
    arguments = returns[0].elts if len(returns) == 1 and isinstance(returns[0], ast.List) else []
    optional = [node.value for node in arguments if isinstance(node, ast.Starred)]
    condition = ast.parse('REVIEW_SETTINGS["provider"] == "deepseek"', mode="eval").body
    if (len(optional) != 1 or not isinstance(optional[0], ast.IfExp)
            or ast.dump(optional[0].test) != ast.dump(condition)
            or not isinstance(optional[0].body, ast.List) or not isinstance(optional[0].orelse, ast.List)
            or optional[0].orelse.elts):
        return findings + ["production_config_native_settings_drift"]
    arguments = [node for node in arguments if not isinstance(node, ast.Starred)] + optional[0].body.elts
    literals = [node.value if isinstance(node, ast.Constant) else None for node in arguments]
    settings = [arguments[index + 1] for index, value in enumerate(literals[:-1]) if value == "--config"]
    strings = [node.value for node in settings if isinstance(node, ast.Constant) and isinstance(node.value, str)]
    effort = ast.parse('f\'model_reasoning_effort="{REASONING_EFFORT}"\'', mode="eval").body
    catalog_setting = ast.parse(
        "'model_catalog_json=' + json.dumps(str(project_root / production_config.MODEL_CATALOG))",
        mode="eval",
    ).body
    provider_setting = ast.parse('f\'model_provider="{REVIEW_SETTINGS["model_provider"]}"\'', mode="eval").body
    models = [arguments[index + 1] for index, value in enumerate(literals[:-1]) if value == "--model"]
    if (literals.count("--ignore-user-config") != 1
            or any(value.startswith("model_provider=") for value in strings)
            or sum(ast.dump(node) == ast.dump(provider_setting) for node in settings) != 1
            or 'model_providers.oa_deepseek.env_key="OA_REVIEW_API_KEY"' not in strings
            or 'model_providers.oa_deepseek.requires_openai_auth=false' not in strings
            or 'model_providers.oa_deepseek.base_url="https://api.deepseek.com"' not in strings
            or 'model_providers.oa_deepseek.wire_api="responses"' not in strings
            or 'model_providers.oa_deepseek.request_max_retries=0' not in strings
            or 'model_providers.oa_deepseek.stream_max_retries=0' not in strings
            or sum(ast.dump(node) == ast.dump(catalog_setting) for node in settings) != 1
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
    try:
        catalog = json.loads((Path(root) / MODEL_CATALOG).read_text(encoding="utf-8"))
        model, = catalog["models"]
        if (model["slug"] != MODEL or set(model["input_modalities"]) != {"text", "image"}
                or model["default_reasoning_level"] != REASONING_EFFORT
                or REASONING_EFFORT not in {level["effort"] for level in model["supported_reasoning_levels"]}
                or model["prefer_websockets"] is not False):
            raise ValueError("model_catalog_drift")
    except (OSError, UnicodeError, ValueError, KeyError, TypeError):
        findings.append("production_config_catalog_drift")
    return sorted(set(findings))


def automation_findings(config: dict, *, production_project_id: str, expected_schedule: str,
                        production_root: Path | None = None) -> list[str]:
    """Check native automation metadata; return codes without disclosing its contents."""
    if not isinstance(config, dict) or not production_project_id or not expected_schedule:
        return ["production_config_automation_input_invalid"]
    findings = []
    if config.get("model") != SCHEDULER_MODEL or config.get("reasoning_effort") != SCHEDULER_REASONING_EFFORT:
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
