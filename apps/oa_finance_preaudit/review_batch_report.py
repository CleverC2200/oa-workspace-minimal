"""Read one frozen private replay archive; no model, OA or state-changing ports."""

from __future__ import annotations

import hashlib
import base64
import json
import math
import os
from pathlib import Path
import re
import stat
from datetime import datetime, timezone
from copy import deepcopy

from . import (
    codex_review_worker as worker,
    flow_engine,
    flow_evals,
    observability,
    review_failure_evidence,
    runtime_maintenance,
)
from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
from referencing import Registry
from referencing.exceptions import Unresolvable
from apps.oa_approval.runtime_control import pinned_directory

ERROR = "review_archive_invalid"


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _material_signature(root: Path, material: dict) -> str:
    # Frozen evidence has already bound these transport paths to this request.
    # Preserve every business value, owner, resource id, page order and unknown.
    relocated = deepcopy(material)
    for items, field in (
        (relocated["resources"], "local_readonly_path"),
        (relocated["prepared"]["images"], "path"),
    ):
        for item in items:
            path = _path(root, item[field])
            item[field] = {
                "archive_relative_path": str(path.relative_to(root)),
                "sha256": _sha(path),
            }
    return _digest(relocated)


def _bound_object(root: Path, manifest: dict, name: str) -> dict | None:
    path = root / name
    if not any(
        str(path) in manifest.get(key, {}) or name in manifest.get(key, {})
        for key in ("inputs", "outputs")
    ):
        return None
    return _object(root, path)


def _comparison_facts(
    root, manifest, started, result, packet, material, sources, source_root
):
    relative_sources = {
        str(Path(p).relative_to(source_root)): h for p, h in sources.items()
    }
    snapshot = _digest(relative_sources)
    runtime = _bound_object(root, manifest, "runtime-source.json")
    role = {
        "status": "unverified",
        "declared": manifest.get("source_role")
        if manifest.get("source_role")
        in {"local_candidate", "production_run", "production_code_replay", "quality_reference"}
        else "unknown",
        "reason": "runtime_role_not_established",
    }
    if runtime is not None:
        revision = manifest["source_revision"]
        if (
            role["declared"] != "unknown"
            and runtime.get("source_role")
            == role["declared"]
            == started.get("source_role")
            and runtime.get("loaded_source_revision")
            == result.get("loaded_source_revision")
            == started.get("source_revision")
            == revision
            and runtime.get("loaded_source_fingerprint")
            == started.get("source_snapshot_sha256")
            == snapshot
            and result.get("runtime_source_sha256")
            == _sha(root / "runtime-source.json")
        ):
            role.update(
                status="bound_runtime_record",
                reason="frozen_runtime_record_matches_source",
            )
        else:
            role["reason"] = "runtime_revision_or_snapshot_mismatch"
    context = _bound_object(root, manifest, "comparison-context.json")
    if (
        context is not None
        and context.get("source_revision") != manifest["source_revision"]
    ):
        raise ValueError(ERROR)
    context = context or {}
    window = context.get("sample_window")
    if window is not None:
        if (
            not isinstance(window, dict)
            or set(window) != {"start", "end", "sample_keys_sha256"}
            or not re.fullmatch("[0-9a-f]{64}", str(window["sample_keys_sha256"]))
        ):
            raise ValueError(ERROR)
        start, end = (datetime.fromisoformat(window[k]) for k in ("start", "end"))
        if start.tzinfo is None or end.tzinfo is None or start > end:
            raise ValueError(ERROR)
        window = {
            **window,
            "start": start.astimezone(timezone.utc).isoformat(),
            "end": end.astimezone(timezone.utc).isoformat(),
        }
    rules = {
        p: h
        for p, h in relative_sources.items()
        if p.startswith(".agents/skills/oa-finance-preaudit/") and p.endswith(".md")
    }
    flow_rule = worker._FINANCE_RULES.get(packet["workflow_key"], ("", "", ""))[2]
    required = {
        ".agents/skills/oa-finance-preaudit/SKILL.md",
        ".agents/skills/oa-finance-preaudit/" + flow_rule,
        ".agents/skills/oa-finance-preaudit/references/result-contract.md",
        ".agents/skills/oa-finance-preaudit/references/observation-contract.md",
    }
    target = context.get("target_scope_sha256")
    if not isinstance(target, str) or not re.fullmatch("[0-9a-f]{64}", target):
        target = "unknown"
    audit = _bound_object(root, manifest, "semantic-audit.json")
    target_bound = bool(
        audit
        and audit.get("source_revision") == manifest["source_revision"]
        and target != "unknown"
        and audit.get("target_scope_sha256") == target
    )
    qualification = _bound_object(root, manifest, "qualification.json")
    metrics = "unknown"
    if qualification is not None:
        if (
            qualification.get("source_revision") != manifest["source_revision"]
            or qualification.get("target_scope_sha256") != target
        ):
            raise ValueError(ERROR)
        accepted = flow_evals.build_acceptance_report(
            qualification["attempt_samples"],
            replay_report=qualification["replay_report"],
            cli_samples=qualification["cli_samples"],
            review_timing_receipts=qualification.get("review_timing_receipts", ()),
            runtime_evidence=qualification.get("runtime_evidence"),
        )
        metrics = {
            k: accepted[k]
            for k in (
                "schema_version",
                "kind",
                "overall",
                "quality_gate",
                "sample_eligible",
                "token_tuning",
                "mcp_admission",
            )
        }
    settings = context.get("settings")
    if settings is not None:
        settings = flow_evals._validate_settings(settings)
    return role, {
        "conditions": {
            "sample_window": window or "unknown",
            "workflow": packet["workflow_key"],
            "material_sha256": _material_signature(root, material),
            "images_sha256": _digest(
                [
                    _sha(_path(root, p["path"]))
                    for p in material.get("prepared", {}).get("images", [])
                ]
            ),
            "rules_sha256": _digest(rules) if required.issubset(rules) else "unknown",
            "target_scope_sha256": target,
        },
        "settings": settings or "unknown",
        "target_bound": target_bound,
        "acceptance_metrics": metrics,
        "financial_signoff": "bound_signoff_record"
        if target_bound
        and audit.get("full_financial_signoff") == "passed"
        and (root / "reviewer/decision.json").exists()
        and audit.get("financial_signoff_decision_sha256")
        == _sha(_path(root, "reviewer/decision.json"))
        else "not_established",
    }


def _path(root: Path, name: str | Path) -> Path:
    p = Path(name)
    p = p if p.is_absolute() else root / p
    if not p.is_relative_to(root) or p != p.resolve() or not p.is_file():
        raise ValueError(ERROR)
    for item in (p, *p.parents):
        if item == root.parent:
            break
        if stat.S_IMODE(item.stat().st_mode) & 0o077:
            raise ValueError(ERROR)
    return p


def _sha(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _decode(raw: str) -> dict:
    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError(ERROR)
            value[key] = item
        return value

    value = json.loads(raw, object_pairs_hook=unique)
    if not isinstance(value, dict):
        raise ValueError(ERROR)
    return value


def _object(root: Path, name: str | Path) -> dict:
    return _decode(_path(root, name).read_text())


def _hashes(root: Path, files: object) -> None:
    if not isinstance(files, dict) or not files:
        raise ValueError(ERROR)
    for name, expected in files.items():
        if (
            not isinstance(name, str)
            or not isinstance(expected, str)
            or not re.fullmatch("[0-9a-f]{64}", expected)
        ):
            raise ValueError(ERROR)
        if _sha(_path(root, name)) != expected:
            raise ValueError(ERROR)


def _events(root: Path, path: Path):
    with _path(root, path).open() as stream:
        for line in stream:
            if not line.strip():
                continue
            yield _decode(line)


def _usage(events: list[dict]) -> dict:
    terminal = [e for e in events if e.get("type") == "turn.completed"]
    if len(terminal) != 1:
        return {"status": "unknown", "reason": "terminal_usage_not_unique"}
    value = terminal[0].get("usage")
    if not isinstance(value, dict):
        return {"status": "unknown", "reason": "original_usage_absent"}
    keys = ("input_tokens", "cached_input_tokens", "output_tokens")
    if any(type(value.get(k)) is not int or value[k] < 0 for k in keys):
        return {"status": "unknown", "reason": "original_usage_incomplete"}
    counted = {k: value[k] for k in keys}
    counted.update(
        status="recorded", total_tokens=value["input_tokens"] + value["output_tokens"]
    )
    if not worker._valid_token_usage(counted):
        return {"status": "unknown", "reason": "original_usage_invalid"}
    reasoning = value.get("reasoning_output_tokens")
    return {
        **counted,
        "status": "exact",
        "reasoning_output_tokens": reasoning
        if type(reasoning) is int and 0 <= reasoning <= value["output_tokens"]
        else "unknown",
    }


def _native(root: Path, material: dict, usage: dict) -> tuple[dict, int | str, dict]:
    images = material.get("prepared", {}).get("images", [])
    expected = {
        str(_path(root, p["path"])): _sha(_path(root, p["path"])) for p in images
    }
    assets = {
        str(_path(root, r["local_readonly_path"])): _sha(
            _path(root, r["local_readonly_path"])
        )
        for r in material.get("resources", [])
        if r.get("local_readonly_path")
    }
    calls = {}
    returned = set()
    peak = None
    reasoning = "unknown"
    tool_calls = set()
    times = []
    providers = set()
    frames = 0
    consumed = False
    initial_frames = []
    manifest = _object(root, "manifest.json")
    prepared_input = _bound_object(root, manifest, "input-delivery.json") or {}
    initial_expected = prepared_input.get("delivery", {}).get("initial_image_sha256", [])
    home = root / "reviewer/codex-home"
    if home.exists():
        if home != home.resolve() or not home.is_dir():
            raise ValueError(ERROR)
        for p in home.rglob("*.jsonl"):
            for event in _events(root, p):
                frames += 1
                try:
                    timestamp = datetime.fromisoformat(event.get("timestamp", ""))
                    if timestamp.tzinfo is not None:
                        times.append(timestamp.astimezone(timezone.utc))
                except (TypeError, ValueError):
                    pass
                value = event.get("payload", {})
                if not isinstance(value, dict):
                    continue
                if (
                    event.get("type") == "response_item"
                    and (
                        value.get("type") == "reasoning"
                        or value.get("type") == "message"
                        and value.get("role") == "assistant"
                    )
                ) or (
                    event.get("type") == "event_msg"
                    and value.get("type")
                    in {
                        "agent_message",
                        "agent_reasoning",
                        "task_started",
                        "task_complete",
                        "turn_started",
                        "turn_complete",
                    }
                ):
                    consumed = True
                if event.get("type") == "session_meta":
                    provider = {
                        "oa_deepseek": "deepseek",
                        "deepseek": "deepseek",
                        "openai": "openai",
                    }.get(value.get("model_provider"))
                    if provider:
                        providers.add(provider)
                if event.get("type") == "response_item" and value.get("type") == "message" and value.get("role") == "user":
                    parts = value.get("content")
                    if isinstance(parts, list):
                        for part in parts:
                            if not isinstance(part, dict) or part.get("type") != "input_image":
                                continue
                            url = part.get("image_url")
                            if isinstance(url, str) and url.startswith("data:image/") and ";base64," in url:
                                raw = base64.b64decode(url.split(";base64,", 1)[1], validate=True)
                                initial_frames.append(hashlib.sha256(raw).hexdigest())
                if (
                    event.get("type") == "response_item"
                    and value.get("type") == "function_call"
                    and isinstance(value.get("call_id"), str)
                ):
                    tool_calls.add(value["call_id"])
                if event.get("type") == "response_item":
                    if (
                        value.get("type") == "function_call"
                        and value.get("name") == "view_image"
                    ):
                        args = json.loads(value.get("arguments", "{}"))
                        name = args.get("path") if isinstance(args, dict) else None
                        call = value.get("call_id")
                        if (
                            isinstance(name, str)
                            and name in {*expected, *assets}
                            and isinstance(call, str)
                            and call
                        ):
                            if call in calls:
                                raise ValueError(ERROR)
                            calls[call] = name
                    elif value.get("type") == "function_call_output":
                        out = value.get("output")
                        if isinstance(out, list) and any(
                            isinstance(b, dict)
                            and b.get("type") == "input_image"
                            and isinstance(b.get("image_url"), str)
                            and b["image_url"].startswith("data:image/")
                            for b in out
                        ):
                            returned.add(value.get("call_id"))
                if (
                    event.get("type") == "event_msg"
                    and value.get("type") == "token_count"
                ):
                    info = value.get("info") or {}
                    last = info.get("last_token_usage") or {}
                    consumed = consumed or any(
                        type(v) is int and v > 0
                        for values in (last, info.get("total_token_usage") or {})
                        for k, v in values.items()
                        if k in {"input_tokens", "output_tokens", "cached_input_tokens"}
                    )
                    size = last.get("input_tokens")
                    if type(size) is int and size >= 0:
                        peak = max(peak or 0, size)
                    total = info.get("total_token_usage") or {}
                    if usage.get("status") == "exact" and all(
                        total.get(k) == usage[k]
                        for k in (
                            "input_tokens",
                            "cached_input_tokens",
                            "output_tokens",
                        )
                    ):
                        n = total.get("reasoning_output_tokens")
                        if type(n) is int and 0 <= n <= usage["output_tokens"]:
                            reasoning = n
    covered = set()
    for call, name in calls.items():
        if call in returned:
            if name in expected:
                covered.add(name)
            else:
                covered.update(p for p, h in expected.items() if h == assets[name])
    if usage.get("status") == "exact" and usage["reasoning_output_tokens"] == "unknown":
        usage["reasoning_output_tokens"] = reasoning
    view_returned = sum(call in returned for call in calls)
    manifest = _object(root, "manifest.json")
    _sufficient, reused, _bound = _reading_coverage(root, manifest, material)
    pages = dict(flow_engine._travel_pages(material))
    directly_covered = set(covered)
    for reference, representative in reused.items():
        if pages[representative]["path"] in directly_covered:
            covered.add(pages[reference]["path"])
    initial_delivered = len(initial_expected) if initial_expected and initial_frames == initial_expected and len(initial_expected) == len(expected) else 0
    return (
        {
            "status": "not_applicable"
            if not expected
            else "verified"
            if len(covered) == len(expected) or initial_delivered == len(expected)
            else "incomplete",
            "expected": len(expected),
            "covered": max(len(covered), initial_delivered),
            "initial_delivered": initial_delivered,
            "initial_payloads_observed": len(initial_frames),
            "view_returned": view_returned,
            "unique_images": len(set(expected.values())),
            "observation": "instrumented_view_receipts" if covered else "not_established_initial_delivery_only" if initial_delivered else "not_established",
        },
        peak if peak is not None else "unknown",
        {
            "tool_events": {"status": "observed", "count": len(tool_calls)}
            if frames
            else {"status": "unknown"},
            "observed_event_range": {
                "status": "observed",
                "start": min(times).isoformat(),
                "end": max(times).isoformat(),
            }
            if times
            else {"status": "unknown"},
            "provider": next(iter(providers)) if len(providers) == 1 else "unknown",
            "consumption_observed": consumed or bool(tool_calls),
        },
    )


def _reading_coverage(root: Path, manifest: dict, material: dict) -> tuple[set, dict, bool]:
    """Resolve existing bound reading assertions against original page bytes."""
    pages = dict(flow_engine._travel_pages(material))
    structured = material.get("prepared", {}).get("structured_contents", [])
    text = {"material:prepared.structured_contents." + item["resource_id"] + ".key_text." + str(index): value
            for item in structured if item.get("document_type") == "pdf_text"
            for index, value in enumerate(item.get("key_text", []))
            if isinstance(value, str) and value.strip()}
    assessment = _bound_object(root, manifest, "semantic-audit.json") or {}
    decision = root / "reviewer/decision.json"
    bound = (assessment.get("source_revision") == manifest["source_revision"]
             and decision.exists()
             and assessment.get("financial_signoff_decision_sha256") == _sha(_path(root, decision)))
    sufficient, reused = set(), {}
    if bound:
        for record in assessment.get("page_reading", []):
            if not isinstance(record, dict) or record.get("page_ref") not in pages or not isinstance(record.get("reason"), str) or not record["reason"].strip():
                continue
            reference = record["page_ref"]
            page = pages[reference]
            resource = page.get("parent_resource_id") or page["resource_id"]
            if record.get("method") == "structured_sufficient" and record.get("structured_ref") in text and record["structured_ref"].startswith("material:prepared.structured_contents." + resource + "."):
                sufficient.add(reference)
            elif record.get("method") == "exact_duplicate" and record.get("representative_page_ref") in pages:
                representative = record["representative_page_ref"]
                h = flow_engine._verified_page_sha256(page)
                if h and h == flow_engine._verified_page_sha256(pages[representative]) and representative != reference:
                    reused[reference] = representative
    return sufficient, reused, bound


def _page_reading(root: Path, manifest: dict, material: dict) -> dict:
    """Report explicit reading assertions without deciding business sufficiency."""
    sufficient, reused, bound = _reading_coverage(root, manifest, material)
    structured = material.get("prepared", {}).get("structured_contents", [])
    return {"structured_text_resources": len({item["resource_id"] for item in structured if item.get("document_type") == "pdf_text" and item.get("key_text")}),
            "reported_structured_sufficient_pages": len(sufficient),
            "reported_exact_duplicate_pages": len(reused),
            "assessment_binding": "bound_original_decision" if bound else "not_established",
            "quality": "not_established_by_reading_report"}


def _start_receipt(root: Path, started: dict, result: dict) -> dict:
    if (result.get("started_receipt_sha256") != _sha(root / "started.json")
            or not (root / "cli-start.json").exists()
            or result.get("cli_start_receipt_sha256") != _sha(_path(root, "cli-start.json"))):
        return {"status": "not_established"}
    cli = _object(root, "cli-start.json")
    try:
        replay, launched = (datetime.fromisoformat(value) for value in (started["replay_started_at"], cli["cli_started_at"]))
    except (KeyError, TypeError, ValueError):
        return {"status": "not_established"}
    if replay.tzinfo is None or launched.tzinfo is None or replay > launched or cli.get("source_revision") != result.get("source_revision"):
        return {"status": "not_established"}
    return {"status": "bound_source_host_receipt", "replay_started_at": replay.astimezone(timezone.utc).isoformat(), "cli_started_at": launched.astimezone(timezone.utc).isoformat()}


def _financial_binding(root: Path, manifest: dict, action: Path, state: dict, preview: str) -> dict:
    """Expose validated artifact correspondence, never a financial judgement."""
    facts = {
        "status": "not_established",
        "item_key": observability.attempt_key(str(state["request_id"]), str(state["received_at"]), state["attempt"]),
        "result_sha256": _sha(root / "result.json"),
        "original_file_sha256": sorted(manifest["originals"].values()),
        "rule_file_sha256": sorted(h for p, h in manifest["source_files"].items() if "/.agents/skills/oa-finance-preaudit/" in p and p.endswith(".md")),
        "preview": preview,
    }
    packet = _object(root, action.with_name("review-packet.json"))
    facts["required_review_item_keys"] = [item["item_key"] for item in packet["items"]]
    source_root = Path(os.path.commonpath([str(Path(p).parent) for p in manifest["source_files"]]))
    relative = {str(Path(p).relative_to(source_root)): h for p, h in manifest["source_files"].items()}
    docs = ("SKILL.md", "references/result-contract.md", "references/observation-contract.md",
            "references/flow-engine-contract.md", worker._FINANCE_RULES[state["workflow_key"]][2])
    required = [".agents/skills/oa-finance-preaudit/" + doc for doc in docs]
    facts["required_rule_file_sha256"] = sorted(relative[name] for name in required) if all(name in relative for name in required) else None
    # Reuse the existing producer's original consumed-attempt metric projection.
    # This disambiguates repeated replays sharing the canonical historical key.
    try:
        facts["case_metric"] = runtime_maintenance._project_case_metric(action.parent, state) if preview == "verified_closed" else None
    except (OSError, KeyError, TypeError, ValueError):
        facts["case_metric"] = None
    raw_file = root / "reviewer/decision.json"
    if preview != "verified_closed" or not raw_file.exists():
        return facts
    try:
        raw = worker._parse_decision_json(_path(root, raw_file).read_text())
        consumed = _object(root, action.with_name("review-decision.json"))
        # The attempt is closed; never reopen it merely to fill protocol fields.
        normalized = dict(raw) if isinstance(raw, dict) else {}
        for key in ("schema_version", "kind", "action_token", "workflow_key", "rule_version"):
            normalized.setdefault(key, consumed.get(key))
        keys = worker._frozen_item_keys(action, consumed["workflow_key"])
        keyed = normalized.get("items")
        if consumed["workflow_key"] == "small_marketing_fee" and isinstance(keyed, dict) and set(keyed) == set(keys) and all(isinstance(keyed[k], dict) for k in keys):
            normalized["items"] = [{**keyed[k], "item_key": k} for k in keys]
        same = isinstance(raw, dict) and normalized == consumed
    except (ValueError, UnicodeError):
        same = False
    return {**facts,
            "status": "verified_consumed_correspondence" if same else "original_consumed_mismatch",
            "raw_decision_sha256": _sha(_path(root, raw_file)),
            "consumed_decision_sha256": _sha(_path(root, action.with_name("review-decision.json"))),
            "remark_sha256": _sha(_path(root, action.with_name("remark.txt"))),
            "receipt_sha256": _sha(_path(root, action.with_name("execution-receipt.json"))),
            "closed_at": _object(root, "result.json").get("preview_consumed_at", "not_established")}


def _closed(root: Path, action: Path, state: dict, result: dict) -> str:
    if result.get("preview") != "closed":
        return "not_established"
    record = _object(root, action)

    # The engine's completion reader validates identity, fingerprints and no-write
    # receipts without entering discovery, collection, completion or Writer.
    def unavailable(*_args, **_kwargs):
        raise ValueError(ERROR)

    engine = flow_engine.FlowEngine(
        root=action.parents[4],
        discover_candidates=unavailable,
        collect_material=unavailable,
        submit_frozen=unavailable,
        recover_frozen=unavailable,
    )
    for name in (
        "execution-receipt.json",
        "review-decision.json",
        "remark.txt",
        "submission-envelope.json",
    ):
        _object(root, action.with_name(name)) if name.endswith(".json") else _path(
            root, action.with_name(name)
        )
    engine._completion_receipt(action.parent, state)
    if (
        record.get("action") != "closed"
        or state.get("action") != "closed"
        or state.get("mode") != "preview"
        or state.get("write_authorized") is not False
        or any(x.get("write_state") != "not_requested" for x in (record, state, result))
    ):
        raise ValueError(ERROR)
    return "verified_closed"


def _build(root: Path) -> dict:
    root = root.expanduser().absolute()
    if (
        root != root.resolve()
        or not root.is_dir()
        or stat.S_IMODE(root.stat().st_mode) & 0o077
    ):
        raise ValueError(ERROR)
    manifest = _object(root, "manifest.json")
    if manifest.get("schema_version") != 1 or not re.fullmatch(
        "[0-9a-f]{40}", str(manifest.get("source_revision", ""))
    ):
        raise ValueError(ERROR)
    started = _object(root, "started.json")
    if started.get("manifest_sha256") != _sha(root / "manifest.json"):
        raise ValueError(ERROR)
    result = _object(root, "result.json")
    if result.get("output_files"):
        _hashes(root, result["output_files"])
    if result.get("source_revision") != manifest["source_revision"]:
        raise ValueError(ERROR)
    _hashes(root, manifest.get("originals"))
    if "outputs" in manifest:
        _hashes(root, manifest["outputs"])
    sources = manifest.get("source_files")
    if (
        not isinstance(sources, dict)
        or not sources
        or not all(isinstance(p, str) and Path(p).is_absolute() for p in sources)
    ):
        raise ValueError(ERROR)
    source_root = Path(os.path.commonpath([str(Path(p).parent) for p in sources]))
    _hashes(
        root,
        {
            str(root / "calling-source" / Path(p).relative_to(source_root)): h
            for p, h in sources.items()
        },
    )
    inputs = manifest.get("inputs")
    if not isinstance(inputs, dict) or not inputs:
        raise ValueError(ERROR)
    action_name = manifest.get("action_file")
    if not action_name:
        actions = [p for p in inputs if Path(p).name == "action.json"]
        if len(actions) != 1:
            raise ValueError(ERROR)
        action_name = actions[0]
    action = _path(root, action_name)
    mutable = {
        str(action),
        str(action.with_name("state.json")),
        str(action.with_name("observability.json")),
        str(action.parents[3] / "current-action.json"),
    }
    _hashes(root, {p: h for p, h in inputs.items() if p not in mutable})
    state = _object(root, action.with_name("state.json"))
    packet = _object(root, action.with_name("review-packet.json"))
    if packet.get("workflow_key") not in flow_engine._FLOW_SPECS:
        raise ValueError(ERROR)
    material = flow_engine.validate_frozen_evidence(
        material_file=_path(root, action.with_name("material.json")),
        request_id=str(state.get("request_id", "")),
        workflow_key=packet["workflow_key"],
        attachment_root=action.parent / "attachments",
        prepared_root=action.parent / "prepared",
        material_fingerprint=str(state.get("material_fingerprint", "")),
        evidence_fingerprint=str(state.get("evidence_fingerprint", "")),
    )
    preview = _closed(root, action, state, result)
    if preview != "verified_closed":
        _hashes(root, {p: h for p, h in inputs.items() if p in mutable})
    event_file = root / "reviewer/events.jsonl"
    events = list(_events(root, event_file)) if event_file.exists() else []
    usage = _usage(events)
    reported_usage = (result.get("execution") or {}).get("token_usage") or {}
    if reported_usage.get("status") == "partial":
        fields = (
            "input_tokens",
            "cached_input_tokens",
            "output_tokens",
            "total_tokens",
        )
        if usage.get("status") == "exact" and all(
            reported_usage.get(k) == usage[k] for k in fields
        ):
            usage.update(
                status="partial", scope="observed_subtotal_whole_batch_unknown"
            )
        else:
            usage = {"status": "unknown", "reason": "partial_binding_incomplete"}
    elif (
        type(result.get("cli_starts")) is not int
        or result["cli_starts"] != 1
        or type(result.get("logical_review_calls")) is not int
        or result["logical_review_calls"] != 1
    ):
        usage = {"status": "unknown", "reason": "whole_batch_coverage_not_established"}
    images, peak, native = _native(root, material, usage)
    execution = result.get("execution") or {}
    if (
        execution.get("process_state") in {"not_started", "start_failed"}
        and type(result.get("cli_starts")) is int
        and result["cli_starts"] == 0
        and not events
        and not native["consumption_observed"]
    ):
        usage = {"status": "not_consumed", "reason": "explicit_no_process_start"}
    contract = "not_established"
    schema_path = root / "generation-schema.json"
    if str(schema_path) in inputs and (root / "reviewer/decision.json").exists():
        schema = _object(root, schema_path)
        Draft202012Validator.check_schema(schema)
        # Invalid final text is failed acceptance, not invalid metering evidence.
        # Read and bind the original bytes before parsing; never repair them.
        final_bytes = _path(root, "reviewer/decision.json").read_bytes()
        try:
            raw = worker._parse_decision_json(final_bytes.decode("utf-8"))
        except (ValueError, UnicodeError):
            raw = None
        contract = (
            "verified_snapshot_schema"
            if isinstance(raw, dict)
            and Draft202012Validator(schema, registry=Registry()).is_valid(
                raw
            )
            else "rejected"
        )
    target = (
        _object(root, "semantic-audit.json")
        if (root / "semantic-audit.json").exists()
        else {}
    )
    target_same_revision = target.get("source_revision") == manifest["source_revision"]
    role, comparison = _comparison_facts(
        root, manifest, started, result, packet, material, sources, source_root
    )
    model = manifest.get("model")
    elapsed = result.get("elapsed_seconds")
    return {
        "schema_version": 1,
        "kind": "preaudit_single_batch_report",
        "archive_fingerprint": _sha(root / "manifest.json"),
        "source": {
            "revision": manifest["source_revision"],
            "archived_bytes": "verified",
            "role": role,
            "model": model
            if model in review_failure_evidence.ACCEPTED_MODELS
            else "unknown",
            "provider": manifest.get("provider")
            if manifest.get("provider") in {"deepseek", "openai"}
            else native["provider"],
            "reasoning_effort": manifest.get("reasoning_effort")
            if manifest.get("reasoning_effort") in {"low", "medium", "high"}
            else "unknown",
        },
        "scope": {
            "workflow": packet["workflow_key"],
            "samples": 1,
            "sample_fingerprint": _digest(
                {
                    "request_id": state.get("request_id"),
                    "received_at": state.get("received_at"),
                    "workflow": packet["workflow_key"],
                }
            ),
        },
        "observed_event_range": native["observed_event_range"],
        "archived_plan": {
            "fingerprint": _sha(root / "manifest.json"),
            "source_revision": manifest["source_revision"],
            "maximum_cli_starts": manifest.get("maximum_cli_starts")
            if type(manifest.get("maximum_cli_starts")) is int
            and manifest["maximum_cli_starts"] >= 0
            else "unknown",
            "maximum_seconds": manifest.get("maximum_seconds")
            if type(manifest.get("maximum_seconds")) in {int, float}
            and math.isfinite(manifest["maximum_seconds"])
            and manifest["maximum_seconds"] >= 0
            else "unknown",
            "new_execution_authority": False,
        },
        "comparison_evidence": comparison,
        "financial_binding": _financial_binding(root, manifest, action, state, preview),
        "usage": usage,
        "timing": {
            "start_receipt": _start_receipt(root, started, result),
            "boundary": result.get("timing_boundary", "not_established"),
            "status": "reported"
            if type(elapsed) in {int, float} and math.isfinite(elapsed) and elapsed >= 0
            else "unknown",
            "wall_seconds": elapsed
            if type(elapsed) in {int, float} and math.isfinite(elapsed) and elapsed >= 0
            else "unknown",
        },
        "interactions": {
            "logical_review_calls": result.get("logical_review_calls")
            if type(result.get("logical_review_calls")) is int
            and result["logical_review_calls"] >= 0
            else "unknown",
            "cli_starts": result.get("cli_starts")
            if type(result.get("cli_starts")) is int
            and result["cli_starts"] >= 0
            and not (result["cli_starts"] == 0 and native["consumption_observed"])
            else "unknown",
            "provider_requests": "unknown",
            "tool_events": native["tool_events"],
            "observed_max_step_input": peak,
        },
        "acceptance": {
            "process": {
                "status": "reported",
                "state": execution.get("process_state")
                if execution.get("process_state")
                in {"exited", "timed_out", "cancelled", "not_started", "start_failed"}
                else "unknown",
                "exit_code": execution.get("exit_code")
                if type(execution.get("exit_code")) is int
                else "unknown",
            },
            "final_contract": contract,
            "preview": preview,
            "original_images": images,
            "page_reading": _page_reading(root, manifest, material),
            "limited_target": {
                "status": "reported" if target_same_revision else "not_established",
                "result": target.get(
                    "target_acceptance", target.get("business_target_acceptance")
                )
                if target_same_revision
                and target.get(
                    "target_acceptance", target.get("business_target_acceptance")
                )
                in {"passed", "failed", "failed_or_needs_manual_check"}
                else "unknown",
                "scope": "limited_declared_review_only"
                if target_same_revision
                else "unknown",
            },
            "full_financial_signoff": comparison["financial_signoff"],
            "all_flow_qualification": "not_established",
            "production_loaded": "not_established",
        },
        "write_authorization": False,
        "comparison": "not_requested_single_batch",
    }


def build_report(archive: Path) -> dict:
    """Validate one existing single-call archive and return a sanitized report."""
    try:
        return _build(Path(archive))
    except (
        OSError,
        KeyError,
        TypeError,
        ValueError,
        RecursionError,
        AttributeError,
        IndexError,
        SchemaError,
        Unresolvable,
    ):
        raise ValueError(ERROR) from None


def write_private_text(
    content: str, target: Path, *, evidence_roots: tuple[Path, ...]
) -> None:
    """Create one private derived report, refusing evidence-tree writes or overwrite."""
    target = target.expanduser().absolute()
    if target != target.resolve() or any(
        target.is_relative_to(p.expanduser().absolute())
        or target.is_relative_to(p.expanduser().resolve())
        for p in evidence_roots
    ):
        raise ValueError("review_report_output_invalid")
    with pinned_directory(target.parent, create=False) as parent:
        if os.fstat(parent).st_mode & 0o077:
            raise ValueError("review_report_output_invalid")
        fd = os.open(
            target.name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=parent,
        )
        with os.fdopen(fd, "w") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.fsync(parent)


def write_private_report(
    report: dict, target: Path, *, evidence_roots: tuple[Path, ...]
) -> None:
    write_private_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        target,
        evidence_roots=evidence_roots,
    )
