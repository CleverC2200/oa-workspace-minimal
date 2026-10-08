"""Task-private immutable original references, migrated from the sealed adapter.

No dispatcher, credentials, controller or record mutation lives here. Legacy
qualification remains unchanged; schema 3 uses its explicit new-window evidence.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from . import flow_evals, review_batch_report as batch, review_experiment as experiment


class ReferenceRejected(ValueError):
    pass


def require(value, reason):
    if not value:
        raise ReferenceRejected(reason)


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def reference(path):
    path = Path(path).absolute()
    return {"file": str(path), "sha256": sha(path)}


def bound(ref):
    require(isinstance(ref, dict) and set(ref) == {"file", "sha256"}, "reference_invalid")
    path = Path(ref["file"])
    require(path.is_absolute() and path.is_file() and not path.is_symlink(), "reference_path_invalid")
    require(sha(path) == ref["sha256"], "reference_bytes_changed")
    return batch._decode(path.read_text(encoding="utf-8"))


def archive_sources(manifest_ref):
    manifest = bound(manifest_ref)
    root = Path(manifest_ref["file"]).parent
    files = manifest["source_files"]
    require(isinstance(files, dict) and files, "source_files_missing")
    original_root = Path(os.path.commonpath([str(Path(p).parent) for p in files]))
    relative = {str(Path(p).relative_to(original_root)): h for p, h in files.items()}
    for rel, expected in relative.items():
        path = root / "calling-source" / rel
        require(path.is_file() and not path.is_symlink() and sha(path) == expected,
                "archived_source_bytes_changed")
    batch._hashes(root, manifest["originals"])
    inputs = manifest["inputs"]
    if (root / "result.json").exists():
        # The original consumer legitimately updates these mutable state files.
        # build_report below checks their complete consumed correspondence.
        action = Path(manifest["action_file"])
        mutable = {str(action), str(action.with_name("state.json")),
                   str(action.with_name("observability.json")),
                   str(action.parents[3] / "current-action.json")}
        inputs = {p: h for p, h in inputs.items() if p not in mutable}
    batch._hashes(root, inputs)
    snapshot = hashlib.sha256(
        json.dumps(relative, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return manifest, relative, snapshot


def _reasoning_receipts(root, usage, raw_usage):
    """Unknown is permitted only when matching native receipts omit the field."""
    observed = []
    if 'reasoning_output_tokens' in raw_usage:
        observed.append(raw_usage['reasoning_output_tokens'])
    for event in batch._events(root, root / 'reviewer/events.jsonl'):
        value = event.get('usage')
        if event.get('type') == 'turn.completed' and isinstance(value, dict) and 'reasoning_output_tokens' in value:
            observed.append(value['reasoning_output_tokens'])
    for path in batch._native_receipt_paths(root):
        for event in batch._events(root, path):
            payload = event.get('payload', {})
            if not isinstance(payload, dict) or event.get('type') != 'event_msg' or payload.get('type') != 'token_count':
                continue
            total = (payload.get('info') or {}).get('total_token_usage') or {}
            if (isinstance(total, dict) and 'reasoning_output_tokens' in total
                    and all(total.get(key) == usage[key] for key in
                            ('input_tokens', 'cached_input_tokens', 'output_tokens'))):
                observed.append(total['reasoning_output_tokens'])
    require(all(type(value) is int and 0 <= value <= usage['output_tokens'] for value in observed),
            'reference_reasoning_usage_invalid')
    reasoning = usage['reasoning_output_tokens']
    require((reasoning == 'unknown' and not observed)
            or (type(reasoning) is int and 0 <= reasoning <= usage['output_tokens']
                and bool(observed) and all(value == reasoning for value in observed)),
            'reference_reasoning_usage_invalid')


def original_record(refs):
    """Mechanically validate one fully audited consumed original, never rescore it."""
    plan = bound(refs["plan"])
    arm = refs["arm"]
    require(arm in ("baseline", "candidate"), "reference_arm_invalid")
    root = Path(plan["arms"][arm])
    manifest_ref = refs["manifest"]
    require(Path(manifest_ref["file"]) == root / "manifest.json", "reference_archive_mismatch")
    # The original host adds three bound derived inputs before its one launch.
    # Keep the prelaunch fingerprint, terminal manifest and original claims;
    # do not equate those two legitimately different manifest byte hashes.
    require(experiment._observed_cli_start(plan, arm) == 1,
            "original_start_plan_binding_unverified")
    manifest, source_files, snapshot = archive_sources(manifest_ref)
    require(snapshot == plan["preparations"][arm]["source_snapshot_sha256"],
            "reference_snapshot_mismatch")
    require(bound(reference(root / "input-delivery.json")) == plan["preparations"][arm],
            "original_preparation_mismatch")
    claim = root.parent / "plan.json.started.json"
    require(claim.is_file(), "original_plan_claim_missing")
    require(bound(reference(claim))["plan_sha256"] == refs["plan"]["sha256"],
            "original_plan_claim_mismatch")
    saved = bound(refs["report"])
    if refs.get("report_field") is not None:
        saved = saved[refs["report_field"]]
    rebuilt = batch.build_report(root)
    require(saved == rebuilt, "original_report_projection_changed")
    require(saved["source"]["revision"] == manifest["source_revision"]
            == plan["source_revisions"][arm], "reference_source_mismatch")
    require(saved["source"]["role"]["status"] == "bound_runtime_record"
            and saved["source"]["role"]["declared"] == manifest["source_role"]
            == plan["source_roles"][arm], "reference_role_mismatch")
    require(saved["source"]["archived_bytes"] == "verified", "reference_source_unverified")
    acceptance = saved["acceptance"]
    require(acceptance["process"]["state"] == "exited"
            and acceptance["process"]["exit_code"] == 0
            and acceptance["final_contract"] == "verified_snapshot_schema"
            and acceptance["preview"] == "verified_closed", "reference_not_consumed")
    require(saved["financial_binding"]["status"] == "verified_consumed_correspondence",
            "reference_consumption_unverified")
    require(saved["write_authorization"] is False, "reference_write_authorized")
    raw = bound(reference(root / "result.json"))
    require(raw["write_state"] == "not_requested" and raw["preview"] == "closed"
            and raw["completion_error"] is None, "reference_no_write_unverified")
    require(raw["cli_starts"] == raw["logical_review_calls"] == 1, "reference_start_count_invalid")
    require(saved["usage"]["status"] == "exact", "reference_usage_unknown")
    for key in ("input_tokens", "cached_input_tokens", "output_tokens", "total_tokens"):
        require(saved["usage"][key] == raw["execution"]["token_usage"][key],
                "reference_usage_mismatch")
    require(saved["usage"]["total_tokens"] == saved["usage"]["input_tokens"]
            + saved["usage"]["output_tokens"], "reference_total_invalid")
    require(saved["usage"]["cached_input_tokens"] <= saved["usage"]["input_tokens"],
            "reference_usage_subset_invalid")
    _reasoning_receipts(root, saved['usage'], raw['execution']['token_usage'])
    require(saved["timing"]["boundary"] == plan["timing_boundary"]
            == "input_preparation_to_preview_consumption", "reference_timing_mismatch")
    require(plan["timeout_seconds"] == 900 and manifest["maximum_seconds"] == 900,
            "reference_timeout_mismatch")
    require(sha(plan["reviewer"]) == plan["reviewer_sha256"], "reference_native_bytes_changed")
    quality = flow_evals.read_financial_quality_report(refs["quality"])
    assessment = bound(refs["assessment"])
    checklist = bound(refs["checklist"])
    freeze = bound(refs["freeze"])
    require(len(quality["cases"]) == 1, "reference_quality_not_single_case")
    case = quality["cases"][0]
    require(case["checklist_sha256"] == assessment["checklist_sha256"]
            == refs["checklist"]["sha256"], "reference_checklist_mismatch")
    require(case["assessment_sha256"] == refs["assessment"]["sha256"],
            "reference_assessment_mismatch")
    expected = {check["id"] for check in checklist["checks"]}
    actual = {check["id"]: check["outcome"] for check in assessment["checks"]}
    require(len(expected) == len(checklist["checks"]) == len(assessment["checks"]) == 4
            and expected == set(actual) and case["check_outcomes"] == actual,
            "reference_four_checks_mismatch")
    require(case["verdict"] == "pass" and all(v == "pass" for v in actual.values()),
            "reference_quality_failed_or_unverified")
    if quality["schema_version"] == 3:
        # New-window qualification is explicit. Rebuild it from the same
        # original freeze/resolution/assessment, never from saved flags alone.
        rebuilt = flow_evals.build_financial_quality_report([{
            "item_key": case["item_key"], "archive": manifest_ref,
            "checklist": refs["checklist"], "assessment": refs["assessment"],
            "expectations": refs["freeze"],
        }])
        require(quality == rebuilt, "original_quality_projection_changed")
        require(case["historical_crosscheck"] == freeze["historical_crosscheck"] == "conflict"
                and case["history_scope_resolution"]["status"] == "explained_scope_difference"
                and case["window_expectations_qualified"] is True
                and case["expectations_frozen_before_replay"] is False,
                "reference_history_not_qualified")
        qualified_expectations = case["window_expectations_qualified"]
    else:
        require(case["historical_crosscheck"] == "complete"
                and freeze["historical_crosscheck"] == "complete",
                "reference_history_not_qualified")
        qualified_expectations = case["expectations_frozen_before_replay"]
    # A legacy report's conjunction is not a separate timing proof. Verify the
    # original freeze against the original trusted host receipt, preserving the
    # legacy report's unknown projection rather than rewriting it.
    timing = flow_evals._expectations_timing(
        freeze, refs["checklist"]["sha256"], saved["timing"]["start_receipt"])
    configs = {Path(path).name: h for path, h in manifest["source_files"].items()
               if "/apps/oa_approval/configs/" in path and path.endswith(".json")}
    require(timing["status"] == "verified"
            and qualified_expectations is True
            and freeze["flow_configs"] == configs
            and freeze["checklist_sha256"] == refs["checklist"]["sha256"],
            "reference_expectations_unverified")
    require(case["complete_review_scope"] is True and case["result_audit_complete"] is True
            and case["source_kind"] == "real_archive", "reference_scope_incomplete")
    replay = flow_evals._financial_replay_binding(
        {"item_key": case["item_key"], "archive": manifest_ref}, checklist, assessment)
    require(replay["status"] == "verified_consumed_correspondence",
            "reference_assessment_consumption_mismatch")
    return {"refs": refs, "plan": plan, "arm": arm, "archive": str(root),
            "manifest": manifest, "source_files": source_files,
            "preparation": plan["preparations"][arm], "report": saved,
            "quality_case": case, "reported_eligible": quality["eligible"]}


def match_target(target_plan_ref, arm, refs, snapshot_audit_ref=None):
    target = bound(target_plan_ref)
    record = original_record(refs)
    original = record["plan"]
    target_root = Path(target["arms"][arm])
    target_manifest_ref = reference(target_root / "manifest.json")
    if (target_root / "result.json").exists():
        require(str(target_root) == record["archive"]
                and experiment._observed_cli_start(target, arm) == 1,
                "target_completed_archive_not_original_reference")
    else:
        require(target_manifest_ref["sha256"] == target["manifests"][arm],
                "target_manifest_mismatch")
    target_manifest, target_sources, target_snapshot = archive_sources(target_manifest_ref)
    require(target_snapshot == target["preparations"][arm]["source_snapshot_sha256"],
            "target_snapshot_mismatch")
    require(target["source_revisions"][arm] == original["source_revisions"][record["arm"]],
            "target_source_mismatch")
    require(target["source_roles"][arm] == original["source_roles"][record["arm"]],
            "target_role_mismatch")
    require(target["reviewer_sha256"] == original["reviewer_sha256"]
            and sha(target["reviewer"]) == target["reviewer_sha256"], "target_native_mismatch")
    model_keys = ("model", "provider", "reasoning_effort") + (
        ("requested_service_tier",) if "requested_service_tier" in record["manifest"] else ())
    present = [key in target_manifest for key in model_keys]
    require(all(present) or not any(present), "target_model_identity_partial")
    if all(present):
        for key in model_keys:
            require(target_manifest[key] == record["manifest"][key], "target_model_mismatch")
        model_proof = "terminal_manifest_model_tuple"
    else:
        # The original archive contract writes this tuple only at launch.
        # An unstarted target instead has the exact frozen CLI call profile
        # and complete source byte map, both checked below. No default model
        # is substituted and no new manifest is written.
        require(not (target_root / "result.json").exists(), "target_model_identity_missing")
        model_proof = "unstarted_frozen_source_and_call_profile"
    require(target["timeout_seconds"] == original["timeout_seconds"]
            and target["timing_boundary"] == original["timing_boundary"], "target_call_limits_mismatch")
    new, old = target["preparations"][arm], record["preparation"]
    require(new["conditions"] == old["conditions"], "target_conditions_mismatch")
    require(new["delivery"] == old["delivery"] and new["input_sha256"] == old["input_sha256"],
            "target_delivery_or_input_mismatch")
    delta = None
    if new["source_snapshot_sha256"] != old["source_snapshot_sha256"]:
        require(arm == "baseline" and snapshot_audit_ref is not None,
                "snapshot_difference_not_audited")
        audit = bound(snapshot_audit_ref)
        require(audit["kind"] == "readonly_cross_date_production_reference_audit"
                and audit["original_data_changed"] is False, "snapshot_audit_invalid")
        matches = [r for r in audit["records"] if r["old_plan"] == refs["plan"]["file"]
                   and r["old_archive"] == record["archive"]]
        require(len(matches) == 1, "snapshot_audit_record_missing")
        witness = matches[0]
        for ref in witness["evidence_refs"]:
            bound(ref) if Path(ref["file"]).suffix == ".json" else require(
                sha(ref["file"]) == ref["sha256"], "snapshot_witness_bytes_changed")
        audited_target = bound(reference(witness["new_baseline_plan"]))
        _audited_manifest, audited_sources, _ = archive_sources({
            "file": str(Path(audited_target["arms"]["baseline"]) / "manifest.json"),
            "sha256": audited_target["manifests"]["baseline"]})
        require(new == audited_target["preparations"]["baseline"]
                and target_sources == audited_sources, "snapshot_audit_target_mismatch")
        extra_new = sorted(set(target_sources) - set(record["source_files"]))
        extra_old = sorted(set(record["source_files"]) - set(target_sources))
        require(extra_new == witness["extra_source_new"] == ["scripts/quality.py"]
                and extra_old == witness["extra_source_old"] == [], "snapshot_delta_not_allowed")
        require(all(target_sources[k] == v for k, v in record["source_files"].items()),
                "snapshot_common_source_changed")
        require(all(witness[k] is True for k in (
            "common_source_equal", "archive_source_bytes_valid", "bound_refs_valid",
            "quality_output_bindings_valid", "original_decision_consumed_equal",
            "conditions_equal", "cli_sha_equal", "material_identity_images_equal")),
            "snapshot_audit_unverified")
        delta = {"old_snapshot": old["source_snapshot_sha256"],
                 "target_snapshot": new["source_snapshot_sha256"],
                 "extra_target_files": extra_new, "audit_ref": snapshot_audit_ref,
                 "comparison": "cross_date_actual_version_reference_only"}
    else:
        require(target_sources == record["source_files"], "source_map_mismatch")
    return {"archive_ref": refs["manifest"], "plan_ref": refs["plan"],
            "report_ref": refs["report"], "quality_ref": refs["quality"],
            "assessment_ref": refs["assessment"], "expectations_ref": refs["freeze"],
            "source_revision": record["manifest"]["source_revision"],
            "source_role": record["manifest"]["source_role"], "reported_eligible": record["reported_eligible"],
            "model_identity_verification": model_proof,
            "model_tuple": {key: record["manifest"][key] for key in model_keys},
            "snapshot_difference": delta, "new_starts": 0, "new_consumption": False}


def prepare_pair(target_plan_ref, reused_arms, snapshot_audit_ref=None):
    """Original archives are refs, never copied into the target arm."""
    plan = bound(target_plan_ref)
    require(set(plan["arms"]) == {"baseline", "candidate"}, "target_pair_invalid")
    require(plan["oa_write_authorized"] is False, "target_write_authorized")
    require(set(reused_arms) <= set(plan["arms"]), "reuse_arm_invalid")
    reused = {arm: match_target(target_plan_ref, arm, refs, snapshot_audit_ref)
              for arm, refs in reused_arms.items()}
    pending = [arm for arm in ("baseline", "candidate") if arm not in reused]
    for arm in pending:
        root = Path(plan["arms"][arm])
        require(not any((root / name).exists() for name in (
            "started.json", "cli-start.json", "result.json", "reviewer")), "pending_arm_already_owned")
    return {"target_plan_ref": target_plan_ref, "reused_originals": reused,
            "pending_arms": pending, "new_starts": 0, "reference_check_only": True,
            "dispatch_authority": "not_granted"}
