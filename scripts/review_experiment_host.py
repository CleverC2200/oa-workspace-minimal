"""Private source host for the bound experiment runner; not a public OA entry."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import time
import signal
import tomllib
from dataclasses import asdict
from datetime import datetime, timezone


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def save(path, value):
    with path.open("x", encoding="utf-8") as stream:
        os.chmod(path, 0o600)
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")


def unavailable(*args, **kwargs):
    raise AssertionError("business_ports_forbidden")


def call_identity(command):
    """Project explicit frozen CLI settings; a model name never selects a provider."""
    models = [command[index + 1] for index, argument in enumerate(command[:-1])
              if argument in {"--model", "-m"}]
    settings = {}
    for index, argument in enumerate(command[:-1]):
        if argument not in {"--config", "-c"}:
            continue
        override = command[index + 1]
        key = override.partition("=")[0].strip()
        if key not in {"model", "model_provider", "model_reasoning_effort", "service_tier"}:
            continue
        if key in settings:
            raise ValueError("call_identity_ambiguous")
        settings.update(tomllib.loads(override))
    if (len(models) != 1 or "model" in settings or not models[0]
            or settings.get("model_provider") not in {"oa_deepseek", "deepseek", "openai"}
            or settings.get("model_reasoning_effort") not in {"low", "medium", "high"}):
        raise ValueError("call_identity_unverified")
    service = settings.get("service_tier", "not_configured")
    if not isinstance(service, str) or not service:
        raise ValueError("call_identity_unverified")
    return {"model": models[0],
            "provider": {"oa_deepseek": "deepseek", "deepseek": "deepseek", "openai": "openai"}[settings["model_provider"]],
            "reasoning_effort": settings["model_reasoning_effort"],
            "requested_service_tier": service}


def main():
    os.umask(0o077)
    sys.dont_write_bytecode = True
    plan_path, name, mode = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
    plan = json.loads(plan_path.read_text())
    root = Path(plan["arms"][name])
    manifest = json.loads((root / "manifest.json").read_text())
    source_root = Path(os.path.commonpath([str(Path(p).parent) for p in manifest["source_files"]]))
    relative_sources = {str(Path(p).relative_to(source_root)): h for p, h in manifest["source_files"].items()}
    for relative, expected in relative_sources.items():
        source = root / "calling-source" / relative
        if source != source.resolve() or digest(source.read_bytes()) != expected:
            raise ValueError("source_drift")
    for p, expected in manifest["inputs"].items():
        if digest(Path(p).read_bytes()) != expected:
            raise ValueError("input_drift")
    calling = root / "calling-source"
    if mode == "native-readiness" and name == "baseline" and manifest.get("source_role") == "production_code_replay":
        # The verifier is external tooling. Application imports stay in immutable
        # source children, so old production need not contain this new verifier.
        verifier = Path(__file__).resolve().parents[1] / "apps/oa_finance_preaudit/baseline_native_contract.py"
        spec = importlib.util.spec_from_file_location("oa_baseline_verifier", verifier)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        result = module.readiness(Path(sys.argv[4]), source=calling,
            source_revision=manifest["source_revision"], host_file=Path(__file__).resolve(),
            binary=Path(plan["reviewer"]), source_files=relative_sources)
        if result["baseline_comparison_ready"] and (result["binding"]["cli_sha256"] != plan["reviewer_sha256"]
                or result["binding"]["host_sha256"] != plan["host_sha256"]):
            result = {"status": "not_verified", "baseline_comparison_ready": False,
                "paid_dispatch_ready": False, "input_complete": False, "sample_eligible": False,
                "reason": "baseline_contract_not_current"}
        print(json.dumps(result))
        return
    sys.path.insert(0, str(calling))
    from apps import json_artifact
    from apps.oa_finance_preaudit import codex_review_worker as worker, flow_engine
    if mode == "native-readiness":
        from apps.oa_finance_preaudit.native_review_contract import readiness
        record_path = Path(sys.argv[4])
        result = readiness(record_path)
        if result["paid_dispatch_ready"]:
            binding = json.loads(record_path.read_text())["binding"]
            if (binding["cli_path"] != plan["reviewer"]
                    or binding["cli_sha256"] != plan["reviewer_sha256"]
                    or binding["source_files"]["scripts/review_experiment_host.py"] != plan["host_sha256"]):
                result = {"status": "not_verified", "paid_dispatch_ready": False,
                          "reason": "native_contract_not_current"}
        print(json.dumps(result))
        return
    action_path = Path(manifest["action_file"])
    engine = flow_engine.FlowEngine(root=action_path.parents[4], discover_candidates=unavailable, collect_material=unavailable, submit_frozen=unavailable, recover_frozen=unavailable)
    started_at = time.monotonic()
    action, state, packet = engine._open_attempt(action_path)
    if state.get("mode") != "preview" or state.get("write_authorized") is not False:
        raise ValueError("preview_required")
    material = json.loads(action_path.with_name("material.json").read_text())
    frozen = None
    if action["workflow_key"] == "business_entertainment" and (calling / "apps/oa_finance_preaudit/review_input.py").exists():
        from apps.oa_finance_preaudit import review_input
        frozen = review_input.prepare(action_path)
        if frozen.codec is not None and (str(frozen.codec[0]) != plan["reviewer"] or frozen.codec[1] != plan["reviewer_sha256"]):
            raise ValueError("reviewer_drift")
    prompt = worker._prompt(action_path, **({"reading_instruction": frozen.instruction} if frozen is not None else {}))
    schema = worker._decision_schema(action_path)
    originals = worker._codex_command
    def command(project, schema_path, decision_path, **options):
        result = originals(project, schema_path, decision_path, **options)
        result[0] = plan["reviewer"]
        result.insert(2, "--skip-git-repo-check")
        return result
    worker._codex_command = command
    def normalized(value):
        if isinstance(value, dict):
            return {k: normalized(v) for k, v in value.items()}
        if isinstance(value, list):
            return [normalized(v) for v in value]
        if isinstance(value, str):
            return value.replace(str(calling), "{SOURCE}").replace(str(root), "{ARM}").replace(action["action_token"], "{ACTION_TOKEN}")
        return value
    def fingerprint(value, *, relocate=True):
        return digest(json.dumps(normalized(value) if relocate else value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode())
    comparison_material = json.loads(json.dumps(material))
    for items, key in ((comparison_material["resources"], "local_readonly_path"), (comparison_material["prepared"]["images"], "path")):
        for item in items:
            item[key] = {"relative_path": str(Path(item[key]).relative_to(root)), "sha256": digest(Path(item[key]).read_bytes())}
    docs = ["SKILL.md", "references/result-contract.md", "references/observation-contract.md", "references/flow-engine-contract.md", worker._FINANCE_RULES[action["workflow_key"]][2]]
    rules = {d: (calling / ".agents/skills/oa-finance-preaudit" / d).read_text() for d in docs}
    delivery = {"mode": "complete_initial_input" if frozen and not frozen.fallback_reason else "existing_reading_path", "fallback_reason": frozen.fallback_reason if frozen else None, "images_supplied": len(frozen.images) if frozen else 0}
    initial_hashes = []
    if frozen and frozen.images:
        if frozen.encoded_images:
            initial_hashes = [h for _size, h in frozen.encoded_images]
        else:
            import fitz
            for path in frozen.images:
                pixmap = fitz.Pixmap(path)
                if max(pixmap.width, pixmap.height) <= 2048:
                    initial_hashes.append(digest(path.read_bytes()))
                else:
                    measured = review_input._native_image_sizes(frozen.images)
                    if measured is None or str(measured[0][0]) != plan["reviewer"] or measured[0][1] != plan["reviewer_sha256"]:
                        raise ValueError("image_payload_not_verified")
                    initial_hashes = [h for _size, h in measured[1]]
                    break
    delivery["initial_image_sha256"] = initial_hashes
    role_file = worker._worker_role_file(action_path) if hasattr(worker, "_worker_role_file") else None
    call_options = {"tool_output_token_limit": frozen.tool_output_token_limit} if frozen and getattr(frozen, "tool_output_token_limit", None) is not None else {}
    if role_file is not None:
        call_options["model_instructions_file"] = role_file
    profile = command(calling, Path("{SCHEMA}"), Path("{DECISION}"), **call_options)
    identity = call_identity(profile)
    prepared = {"conditions": {"workflow": action["workflow_key"], "sample": fingerprint({k: state[k] for k in ("request_id", "received_at")}), "material": fingerprint(comparison_material, relocate=False), "image_bytes": fingerprint([digest(Path(p["path"]).read_bytes()) for p in material["prepared"]["images"]]), "rules": fingerprint(rules), "schema": fingerprint(schema), "call_profile": fingerprint(profile), "call_identity": identity, "timeout_seconds": plan["timeout_seconds"], "timing_boundary": plan["timing_boundary"]}, "delivery": delivery, "input_sha256": fingerprint({"prompt": prompt, "images": [digest(p.read_bytes()) for p in frozen.images] if frozen else [], **({"worker_role": role_file.read_text()} if role_file is not None else {})}), "source_snapshot_sha256": digest(json.dumps(relative_sources, sort_keys=True, separators=(",", ":")).encode())}
    if mode == "prepare":
        print(json.dumps(prepared))
        return
    expected = plan["preparations"][name]
    if prepared != expected or digest(Path(plan["reviewer"]).read_bytes()) != plan["reviewer_sha256"]:
        raise ValueError("prepared_input_drift")
    claim = plan_path.with_name(plan_path.name + ".started.json")
    if not claim.is_file() or json.loads(claim.read_text())["plan_sha256"] != digest(plan_path.read_bytes()):
        raise ValueError("authorization_not_bound")
    role = manifest.get("source_role")
    if role not in {"production_code_replay", "local_candidate", "quality_reference"} or role != plan["source_roles"][name]:
        raise ValueError("source_role_drift")
    runtime = {"source_role": role, "loaded_source_revision": manifest["source_revision"], "loaded_source_fingerprint": prepared["source_snapshot_sha256"]}
    # These derived inputs are frozen before launch; original evidence is unchanged.
    for filename, value in (("runtime-source.json", runtime), ("generation-schema.json", schema), ("input-delivery.json", prepared)):
        save(root / filename, value)
        manifest["inputs"][str(root / filename)] = digest((root / filename).read_bytes())
    manifest.update(source_role=role, **identity, maximum_cli_starts=1, maximum_seconds=plan["timeout_seconds"])
    # This is a fresh, unstarted run manifest; no old archive is rewritten.
    (root / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
    save(root / "started.json", {"manifest_sha256": digest((root / "manifest.json").read_bytes()), "source_role": role, "source_revision": manifest["source_revision"], "source_snapshot_sha256": prepared["source_snapshot_sha256"], "replay_started_at": datetime.now(timezone.utc).isoformat()})
    audit = root / "reviewer"
    audit.mkdir(mode=0o700)
    executions = []
    processes = []
    stderr = {}
    original_start = worker.subprocess.Popen
    def start(*args, **kwargs):
        process = original_start(*args, **kwargs)
        processes.append(process)
        save(root / "cli-start.json", {"pid": process.pid, "source_revision": manifest["source_revision"], "input_sha256": prepared["input_sha256"], "cli_started_at": datetime.now(timezone.utc).isoformat()})
        return process
    worker.subprocess.Popen = start
    def executor(project, action_file, seconds):
        if executions:
            raise ValueError("review_retry_forbidden")
        def parse(raw):
            decision = worker._parse_decision_json(raw)
            return worker._normalize_decision(action_file, decision) if isinstance(decision, dict) else decision
        try:
            execution = worker._execute_codex_raw(calling, action_file, seconds, prompt_builder=lambda _: prompt, schema_builder=lambda _: schema, stderr_summary=stderr, read_only=True, decision_parser=parse, audit_directory=audit, **({"frozen_input": frozen} if frozen else {}))
        except (KeyboardInterrupt, SystemExit):
            reaped = not processes
            if processes:
                process = processes[-1]
                try:
                    os.killpg(process.pid, 0)
                except ProcessLookupError:
                    reaped = process.poll() is not None
            with (audit / "events.jsonl").open() as events:
                usage = worker._parse_token_usage(events)
            execution = worker.CodexExecution("codex_process_cancelled", usage, process_state="cancelled" if reaped else "cleanup_failed", decision_state="not_attempted", exit_code=process.returncode if processes else None)
        executions.append(execution)
        return execution
    reviewed = worker.review({"project_root": str(root), "action_file": str(action_path), "timeout_seconds": plan["timeout_seconds"]}, executor=executor)
    preview = "not_closed"
    completion_error = None
    if reviewed.get("status") == "reviewed":
        decision = root / "candidate-decision.json"
        json_artifact.write_json_atomic(decision, reviewed["decision"])
        try:
            closed = engine.complete_review(action_path, decision)
            if closed.get("action") == "closed" and closed.get("write_state") == "not_requested":
                preview = "closed"
        except ValueError:
            completion_error = "preview_rejected"
    execution = executions[0]
    result = {"source_revision": manifest["source_revision"], "loaded_source_revision": manifest["source_revision"], "runtime_source_sha256": digest((root / "runtime-source.json").read_bytes()), "execution": asdict(execution), "logical_review_calls": 1, "cli_starts": len(processes), "elapsed_seconds": time.monotonic() - started_at, "timing_boundary": plan["timing_boundary"], "preview": preview, "preview_consumed_at": datetime.now(timezone.utc).isoformat() if preview == "closed" else None, "completion_error": completion_error, "write_state": "not_requested", "stderr_summary": stderr, "started_receipt_sha256": digest((root / "started.json").read_bytes()), "cli_start_receipt_sha256": digest((root / "cli-start.json").read_bytes()) if (root / "cli-start.json").exists() else None, "output_files": {str(p): digest(p.read_bytes()) for p in audit.rglob("*") if p.is_file()}}
    save(root / "result.json", result)
    for path in root.rglob("*"):
        path.chmod(0o700 if path.is_dir() else 0o600)
    print(json.dumps(result))


if __name__ == "__main__":
    # The dispatcher blocks SIGTERM while assigning child ownership. Unblock in
    # the new host before it can launch any reviewer; worker then owns handoff.
    if hasattr(signal, "pthread_sigmask"):
        signal.pthread_sigmask(signal.SIG_UNBLOCK, {signal.SIGTERM})
    main()
