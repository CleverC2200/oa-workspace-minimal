"""Read-only cost comparability; never changes qualification or execution state."""

from pathlib import Path

from . import flow_evals, review_batch_report

INTENTS = {"production_candidate", "candidate_iteration"}
ERROR = "review_comparison_invalid"


def _compare(baseline: dict, candidate: dict, intent: str) -> dict:
    if intent not in INTENTS:
        raise ValueError(ERROR)
    reasons = []
    baseline_role = baseline["source"]["role"]
    candidate_role = candidate["source"]["role"]
    if intent == "production_candidate" and (
        baseline_role["status"] != "bound_runtime_record"
        or baseline_role["declared"] not in {"production_run", "production_code_replay"}
    ):
        reasons.append("production_baseline_missing")
    if any(
        r["status"] != "bound_runtime_record" for r in (baseline_role, candidate_role)
    ):
        reasons.append("runtime_source_unverified")
    if candidate_role["declared"] != "local_candidate" or (
        intent == "candidate_iteration"
        and baseline_role["declared"] != "local_candidate"
    ):
        reasons.append("version_roles_not_matched_to_intent")
    if any(r["usage"]["status"] != "exact" for r in (baseline, candidate)):
        reasons.append("whole_cost_unknown")
    if any(
        r["acceptance"]["limited_target"]["result"] != "passed"
        for r in (baseline, candidate)
    ):
        reasons.append("limited_target_not_passed")
    for r in (baseline, candidate):
        acceptance = r["acceptance"]
        if (
            acceptance["process"]["state"] != "exited"
            or acceptance["process"]["exit_code"] != 0
            or acceptance["final_contract"] != "verified_snapshot_schema"
            or acceptance["preview"] != "verified_closed"
            or acceptance["original_images"]["status"]
            not in {"verified", "not_applicable"}
        ):
            reasons.append("technical_acceptance_incomplete")
        if (
            intent == "production_candidate"
            and acceptance["full_financial_signoff"] != "bound_signoff_record"
        ):
            reasons.append("financial_signoff_not_established")
    evidence = [r["comparison_evidence"] for r in (baseline, candidate)]
    for key in evidence[0]["conditions"]:
        values = [e["conditions"][key] for e in evidence]
        if "unknown" in values:
            reasons.append(key + "_missing")
        elif values[0] != values[1]:
            reasons.append(key + "_mismatch")
    for key in ("model", "provider", "reasoning_effort"):
        values = [r["source"][key] for r in (baseline, candidate)]
        if "unknown" in values or values[0] != values[1]:
            reasons.append("call_profile_" + key + "_unmatched")
    if any(not e["target_bound"] for e in evidence):
        reasons.append("target_scope_unbound")
    experiment = None
    if any(
        e["settings"] == "unknown" or e["acceptance_metrics"] == "unknown"
        for e in evidence
    ):
        reasons.append("qualification_or_settings_missing")
    else:
        if any(not e["acceptance_metrics"]["quality_gate"]["passed"] for e in evidence):
            reasons.append("formal_quality_gate_not_passed")
        try:
            experiment = flow_evals.evaluate_experiment(
                evidence[0]["acceptance_metrics"],
                evidence[1]["acceptance_metrics"],
                before_settings=evidence[0]["settings"],
                after_settings=evidence[1]["settings"],
            )
        except ValueError:
            reasons.append("single_variable_not_established")
        if experiment is not None:
            if experiment["decision"] != "keep":
                reasons.extend(experiment["reasons"])
            if not experiment["token_tuning"]["eligible"]:
                reasons.append("formal_token_qualification_not_met")
    benefit = "not_established"
    if not reasons:
        old = baseline["usage"]["total_tokens"]
        new = candidate["usage"]["total_tokens"]
        benefit = {
            "baseline_total_tokens": old,
            "candidate_total_tokens": new,
            "reduction_tokens": old - new,
            "reduction_percent": round(100 * (old - new) / old, 6)
            if old
            else "undefined_zero_baseline",
        }
    return {
        "schema_version": 1,
        "kind": "preaudit_cost_comparison",
        "intent": intent,
        "status": "not_comparable" if reasons else "comparable_observation",
        "reasons": sorted(set(reasons)),
        "observed": {"baseline": baseline["usage"], "candidate": candidate["usage"]},
        "sources": {"baseline": baseline["source"], "candidate": candidate["source"]},
        "acceptance": {
            "baseline": baseline["acceptance"],
            "candidate": candidate["acceptance"],
        },
        "benefit": benefit,
        "causal_attribution": "not_established",
        "quality_scope": "limited_target_with_existing_qualification",
        "formal_gate": {
            "decision": experiment["decision"],
            "reasons": experiment["reasons"],
        }
        if experiment
        else "not_established",
        "write_authorization": False,
    }


def compare_archives(baseline: Path, candidate: Path, *, intent: str) -> dict:
    """Recheck both original archives instead of trusting derived report JSON."""
    if intent not in INTENTS:
        raise ValueError(ERROR)
    return _compare(
        review_batch_report.build_report(baseline),
        review_batch_report.build_report(candidate),
        intent,
    )
