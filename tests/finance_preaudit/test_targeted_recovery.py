from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path

import pytest

from apps.oa_finance_preaudit import attempt_state, cli, codex_review_worker, decision_validation, runtime_doctor
from scripts import finance_preaudit_concurrency_probe as controller
from tests.finance_preaudit.review_profiles import retained_deepseek_cli  # noqa: F401
from tests.finance_preaudit.test_flow_engine_submission import (
    _grant, _verify_grant, _material, _needs_human_decision, _success,
)


class RepairEnvironment:
    def __init__(self, tmp_path, monkeypatch):
        self.project = tmp_path / "oa-workspace-minimal"
        self.root = self.project / "runtime/finance_preaudit_flows"
        self.pending = []
        self.handled = []
        self.collected = []
        self.submitted = []
        self.fail_review_for = set()
        self.after_submit = lambda: None
        self.configs = [{"flow_key": "travel_expense", "workflow_id": "synthetic-travel",
                         "workflow_name": "Synthetic travel"}]
        monkeypatch.setenv("OA_FINANCE_PREAUDIT_FLOW_ROOT", str(self.root))
        monkeypatch.setenv("OA_FINANCE_PREAUDIT_CACHE_DB", "off")
        monkeypatch.setenv("OA_REVIEW_API_KEY", "unit-test-key")
        monkeypatch.setattr(runtime_doctor.auth, "load_local_env", lambda: None)
        monkeypatch.setattr(controller.session_alert, "send_message", self.forbid_notification)
        monkeypatch.setattr(controller.session_alert, "alert_once", self.forbid_notification)
        monkeypatch.setattr(cli.legacy_flow, "load_config", self.forbid_notification)
        monkeypatch.setattr(controller, "PROJECT_ROOT", self.project)
        monkeypatch.setattr(cli, "PROJECT_ROOT", self.project)
        monkeypatch.setattr(controller, "_preparation_executor", lambda: ThreadPoolExecutor(max_workers=1))
        monkeypatch.setattr(controller.concurrency_control, "sample_local_health", lambda: {"status": "healthy"})
        monkeypatch.setattr(cli.legacy_flow, "load_all_configs", lambda: self.configs)
        monkeypatch.setattr(cli.legacy_api, "build_api", lambda _config: self)
        monkeypatch.setattr(cli, "collect_case", self.collect)
        monkeypatch.setattr(cli.production_write, "build_ports", lambda **_: {
            "authorization_source": _grant, "authorization_verifier": _verify_grant,
            "submit_frozen": self.submit,
        })
        original_review = codex_review_worker.review
        monkeypatch.setattr(codex_review_worker, "review", lambda task: original_review(task, executor=self.execute))
        original_prepare = controller._prepare_one
        def fake_prepare(selection):
            assert controller.PROJECT_ROOT == self.project
            return original_prepare(selection)
        original_dispatch = controller._run_dispatched_tasks
        def fake_dispatch(*args, **kwargs):
            return original_dispatch(*args, **kwargs, prepare_action=fake_prepare,
                                     executor_factory=lambda capacity: ThreadPoolExecutor(max_workers=capacity))
        monkeypatch.setattr(controller, "_run_dispatched_tasks", fake_dispatch)
        monkeypatch.setattr(controller, "_prepare_one", self.forbid_notification)
        monkeypatch.setattr(controller.approval_lifecycle, "submit_readonly",
                            lambda executor, function, *args, **kwargs: executor.submit(function, *args))

    @staticmethod
    def forbid_notification(*_args, **_kwargs):
        raise AssertionError("real notifications are forbidden in development tests")

    def add(self, name, hour, workflow="travel_expense"):
        config = next((item for item in self.configs if item["flow_key"] == workflow), None)
        if config is None:
            config = {"flow_key": workflow, "workflow_id": f"synthetic-{workflow}", "workflow_name": f"Synthetic {workflow}"}
            self.configs.append(config)
        row = {"request_id": name, "received_at": f"2026-08-22T{hour:02d}:00:00+08:00",
               "workflow_id": config["workflow_id"], "workflow_name": config["workflow_name"],
               "operator_id": "synthetic-user"}
        self.pending.append(row)
        return row

    def pending_rows(self, **_kwargs):
        return list(self.pending)

    def handled_rows(self, **_kwargs):
        return list(self.handled)

    def collect(self, request_id, **_kwargs):
        self.collected.append(request_id)
        row = next(row for row in self.pending if row["request_id"] == request_id)
        workflow = next(config["flow_key"] for config in self.configs if config["workflow_id"] == row["workflow_id"])
        return {**_material(request_id), "workflow_key": workflow}

    def seed(self, name, hour, workflow="travel_expense"):
        row = self.add(name, hour, workflow)
        action = cli._live_flow_engine(controlled=True, selected_identity=(name, row["received_at"])).next_action(
            workflow, mode="controlled_write")
        assert action["action"] == "review_required"
        assert json.loads(Path(action["state_file"]).read_text())["request_id"] == name
        return Path(action["action_file"])

    def execute(self, _project, action_file, _timeout):
        action = attempt_state.read_action(action_file)
        state = json.loads(action_file.with_name("state.json").read_text())
        if state["request_id"] in self.fail_review_for:
            decision_validation.record_rejection(action_file, {}, decision_validation.InvalidDecision("decision_json"))
            return codex_review_worker.CodexExecution(
                "codex_decision_invalid", {"status": "unknown"}, process_state="exited", exit_code=0,
                decision_state="unreadable",
                event_counts={"thread.started": 1, "turn.started": 1, "turn.completed": 1},
                event_status="recorded",
            )
        decision = _needs_human_decision(action)
        if action["workflow_key"] != "travel_expense":
            decision["workflow_key"] = action["workflow_key"]
            for item in decision["items"]:
                item.update(status="uncertain", conclusion="合成样本缺少此项证明，请财务核对。",
                            evidence_refs=[], missing_evidence=["合成证据缺口"])
        return codex_review_worker.CodexExecution(
            None, {"status": "unknown"}, decision=decision,
            process_state="exited", exit_code=0,
            event_counts={"thread.started": 1, "turn.started": 1, "turn.completed": 1},
            event_status="recorded",
        )

    def submit(self, job):
        self.submitted.append(job["request_id"])
        row = next(row for row in self.pending if row["request_id"] == job["request_id"])
        self.pending.remove(row)
        self.handled.append(row)
        self.after_submit()
        return _success(job)

    def live_status(self):
        return runtime_doctor.status(
            configuration_status=lambda: {"status": "ok"}, configs_loader=lambda: self.configs,
            api_factory=lambda _: self, identity_provider=lambda: "synthetic-user",
            web_session_provider=lambda: {"status": "ok", "authentication": "ok", "source": "rpa_page"},
        )


def test_targeted_run_recovers_isolation_and_resumes_open_attempt(tmp_path, monkeypatch):
    env = RepairEnvironment(tmp_path, monkeypatch)
    a = env.seed("selected-a", 8)
    b = env.seed("selected-b", 9)
    decision_validation.record_rejection(a, {}, decision_validation.InvalidDecision("decision_json"))
    cli._live_flow_engine(controlled=True).quarantine_invalid_review(a)
    original = {path: path.read_bytes() for path in a.parent.rglob("*") if path.is_file()}
    env.collected.clear()
    env.add("outside-c", 7)
    result = controller.run_rolling_drain(
        capacity=2, targets_only=True, recovery_action_files=(a,), resume_action_files=(b,),
        live_status=env.live_status,
    )
    assert env.submitted == ["selected-a", "selected-b"]
    assert env.collected == ["selected-a"]
    assert all(path.read_bytes() == content for path, content in original.items())
    outcome = result["target_outcome"]
    assert outcome["status"] == "complete" and outcome["completed"] == 2, outcome
    recovered = next(row for row in outcome["items"] if row["operation"] == "recover")
    assert recovered["source_attempt"] == 1 and recovered["attempt"] == 2
    assert len(list(b.parent.parent.glob("attempt-*"))) == 1



def test_supervisor_keeps_partial_delivery_separate_from_process_completion(tmp_path, monkeypatch):
    import sys
    from uuid import uuid4
    from scripts.finance_preaudit_run import supervise

    from scripts import finance_preaudit_run as supervisor
    env = RepairEnvironment(tmp_path, monkeypatch)
    selected = [env.seed("selected-a", 8), env.seed("selected-b", 9)]
    code = f'''import json
from pathlib import Path
import pytest
from tests.finance_preaudit.test_targeted_recovery import RepairEnvironment
from apps.oa_finance_preaudit import runtime_doctor
from scripts import finance_preaudit_concurrency_probe as controller
patch = pytest.MonkeyPatch()
env = RepairEnvironment(Path({str(tmp_path)!r}), patch)
a = env.seed("selected-a", 8)
b = env.seed("selected-b", 9)
env.fail_review_for.add("selected-b")
patch.setattr(runtime_doctor.auth, "configuration_status", lambda: {{"status": "ok"}})
patch.setattr(runtime_doctor.auth, "env", lambda key: "synthetic-user" if key == "OA_UID" else "")
patch.setattr(runtime_doctor.attachments, "rpa_web_session_status", lambda: {{
    "status": "ok", "authentication": "ok", "source": "rpa_page"}})
raise SystemExit(controller.main(["--rolling", "--capacity", "2", "--confirm-write", "YES", "--targets-only",
    "--resume-action-file", str(a), "--resume-action-file", str(b)]))
'''
    original_popen = supervisor.subprocess.Popen
    def recorded_controller(command, **kwargs):
        if list(command[1:3]) == ["-m", "scripts.finance_preaudit_concurrency_probe"]:
            command = [sys.executable, "-c", code]
        return original_popen(command, **kwargs)
    monkeypatch.setattr(supervisor.subprocess, "Popen", recorded_controller)
    monkeypatch.setattr(supervisor.approval_guard, "observe_project_errors", lambda _: None)
    command = [sys.executable, "-m", "scripts.finance_preaudit_concurrency_probe", "--rolling",
               "--capacity", "2", "--confirm-write", "YES", "--targets-only",
               "--resume-action-file", str(selected[0]), "--resume-action-file", str(selected[1])]
    receipt = supervise(command, directory=tmp_path / "control/executions" / uuid4().hex, owner_task_id=str(uuid4()))
    assert receipt["result"] == "completed"
    assert receipt["exit_code"] == 0 and receipt["children_reaped"] is True
    assert receipt["target_outcome"]["status"] == "partial"
    assert receipt["target_outcome"]["completed"] == 1
    output = json.loads(Path(receipt["receipt_file"]).with_name("stdout.log").read_text())
    assert receipt["target_outcome"] == output["target_outcome"]
    from scripts.finance_preaudit_run import read_execution
    report = read_execution(Path(receipt["receipt_file"]), owner_task_id=receipt["owner_task_id"],
                            invocation_id=receipt["invocation_id"], retrieve=True)
    assert report["status"] == "completed" and report["target_outcome"] == receipt["target_outcome"]



@pytest.mark.parametrize("outside_hour", [7, 10])
def test_targeted_run_leaves_new_pending_item_unclaimed(tmp_path, monkeypatch, outside_hour):
    env = RepairEnvironment(tmp_path, monkeypatch)
    a = env.seed("selected-a", 8)
    b = env.seed("selected-b", 9)
    env.collected.clear()

    def new_item():
        if not any(row["request_id"] == "outside-c" for row in env.pending):
            env.add("outside-c", outside_hour)

    env.after_submit = new_item
    result = controller.run_rolling_drain(
        capacity=2, targets_only=True, resume_action_files=(a, b), live_status=env.live_status,
    )
    assert result["controller_handoff"]["children_reaped"] is True
    assert env.submitted == ["selected-a", "selected-b"]
    assert result["late_visible"] == 0
    assert env.collected == []
    assert [row["request_id"] for row in env.pending] == ["outside-c"]
    assert len(list(env.root.glob("*/cases/*/attempt-*"))) == 2



def test_targeted_run_preserves_unselected_open_projection(tmp_path, monkeypatch):
    env = RepairEnvironment(tmp_path, monkeypatch)
    a = env.seed("selected-a", 8)
    outside = env.seed("outside-b", 7)
    projection = outside.parents[3] / "current-action.json"
    projection.write_bytes(b'{"historical": "preserve old projection"}\n ')
    before = projection.read_bytes()
    original = {p: p.read_bytes() for p in outside.parent.rglob("*") if p.is_file()}
    result = controller.run_rolling_drain(
        capacity=2, targets_only=True, resume_action_files=(a,), live_status=env.live_status,
    )
    assert result["target_outcome"]["status"] == "complete"
    assert env.submitted == ["selected-a"]
    assert projection.read_bytes() == before
    assert all(p.read_bytes() == content for p, content in original.items())



@pytest.mark.parametrize("mode", ["resume", "recover"])
@pytest.mark.parametrize("path_kind", ["symlink", "relative"])
def test_targeted_run_rejects_invalid_paths_before_claim(tmp_path, monkeypatch, mode, path_kind):
    env = RepairEnvironment(tmp_path, monkeypatch)
    a = env.seed("selected-a", 8)
    if mode == "recover":
        decision_validation.record_rejection(a, {}, decision_validation.InvalidDecision("decision_json"))
        cli._live_flow_engine(controlled=True).quarantine_invalid_review(a)
    if path_kind == "symlink":
        link = tmp_path / "action.json"
        link.symlink_to(a)
    else:
        monkeypatch.chdir(a.parent)
        link = Path("action.json")
    inputs = {"resume_action_files": (link,)} if mode == "resume" else {"recovery_action_files": (link,)}
    with pytest.raises(ValueError, match="production_target_action_invalid"):
        controller.run_rolling_drain(capacity=2, targets_only=True, live_status=env.live_status, **inputs)
    assert env.submitted == []
    assert len(list(a.parent.parent.glob("attempt-*"))) == 1



def test_global_unresolved_write_blocks_targeted_run_with_target_results(tmp_path, monkeypatch):
    env = RepairEnvironment(tmp_path, monkeypatch)
    a = env.seed("selected-a", 8)
    outside = env.seed("outside-b", 9)
    state = json.loads(outside.with_name("state.json").read_text())
    state["write_state"] = "unknown"
    outside.with_name("state.json").write_text(json.dumps(state))
    with pytest.raises(ValueError, match="production_rolling_reconciliation_invalid") as raised:
        controller.run_rolling_drain(
            capacity=2, targets_only=True, resume_action_files=(a,), live_status=env.live_status,
        )
    assert raised.value.target_outcome["status"] == "blocked"
    assert raised.value.target_outcome["target_count"] == 1
    assert raised.value.target_outcome["completed"] == 0
    assert env.submitted == []



def test_handled_recovery_target_is_not_retried(tmp_path, monkeypatch):
    env = RepairEnvironment(tmp_path, monkeypatch)
    a = env.seed("selected-a", 8)
    decision_validation.record_rejection(a, {}, decision_validation.InvalidDecision("decision_json"))
    cli._live_flow_engine(controlled=True).quarantine_invalid_review(a)
    env.handled.append(env.pending.pop())
    result = controller.run_rolling_drain(
        capacity=2, targets_only=True, recovery_action_files=(a,), live_status=env.live_status,
    )
    assert result["target_outcome"]["status"] == "complete"
    assert result["target_outcome"]["items"][0]["outcome"] == "confirmed_no_resubmit"
    assert env.submitted == []
    assert len(list(a.parent.parent.glob("attempt-*"))) == 1



def test_equivalent_resume_inputs_are_deduplicated(tmp_path, monkeypatch):
    env = RepairEnvironment(tmp_path, monkeypatch)
    a = env.seed("selected-a", 8)
    result = controller.run_rolling_drain(
        capacity=2, targets_only=True, resume_action_files=(a, a), live_status=env.live_status,
    )
    assert result["target_outcome"]["target_count"] == 1
    assert result["target_outcome"]["completed"] == 1
    assert env.submitted == ["selected-a"]



@pytest.mark.parametrize("case,reason", [
    ("empty", "production_target_scope_empty"),
    ("mode", "production_target_mode_required"),
    ("automatic", "production_target_scope_conflict"),
    ("workflow", "production_target_workflow_invalid"),
    ("changed", "production_target_resume_invalid"),
])
def test_invalid_selection_never_claims_or_submits(tmp_path, monkeypatch, case, reason):
    env = RepairEnvironment(tmp_path, monkeypatch)
    a = env.seed("selected-a", 8)
    args = {"targets_only": True, "resume_action_files": (a,)}
    if case == "empty":
        args["resume_action_files"] = ()
    elif case == "mode":
        args["targets_only"] = False
    elif case == "automatic":
        args["authorized_recovery_once"] = True
    elif case == "workflow":
        args["allowed_workflows"] = frozenset({"business_entertainment"})
    else:
        a.with_name("material.json").write_text("{}")
    before = {p: p.read_bytes() for p in a.parent.rglob("*") if p.is_file()}
    with pytest.raises(ValueError, match=reason):
        controller.run_rolling_drain(capacity=2, live_status=env.live_status, **args)
    assert env.submitted == []
    assert all(p.read_bytes() == contents for p, contents in before.items())



def test_scope_cannot_supply_missing_write_authorization(tmp_path, monkeypatch):
    env = RepairEnvironment(tmp_path, monkeypatch)
    a = env.seed("selected-a", 8)
    monkeypatch.setattr(cli.production_write, "build_ports", lambda **_: {})
    result = controller.run_rolling_drain(
        capacity=2, targets_only=True, resume_action_files=(a,), live_status=env.live_status,
    )
    assert result["target_outcome"]["status"] == "blocked"
    assert result["target_outcome"]["completed"] == 0
    assert env.submitted == []



def test_default_controller_still_admits_new_pending_items(tmp_path, monkeypatch):
    env = RepairEnvironment(tmp_path, monkeypatch)
    env.seed("normal-a", 8)
    env.after_submit = lambda: env.add("normal-b", 9) if len(env.submitted) == 1 else None
    result = controller.run_rolling_drain(capacity=2, live_status=env.live_status)
    assert "target_outcome" not in result
    assert env.submitted == ["normal-a", "normal-b"]



def test_already_submitted_target_is_confirmed_without_resubmit(tmp_path, monkeypatch):
    env = RepairEnvironment(tmp_path, monkeypatch)
    a = env.seed("selected-a", 8)
    decision = tmp_path / "decision.json"
    decision.write_text(json.dumps(_needs_human_decision(attempt_state.read_action(a))))
    cli._live_flow_engine(controlled=True).complete_review(a, decision)
    env.submitted.clear()
    env.add("outside-b", 9)
    result = controller.run_rolling_drain(
        capacity=2, targets_only=True, resume_action_files=(a,), live_status=env.live_status,
    )
    assert result["target_outcome"]["status"] == "complete"
    assert result["target_outcome"]["items"][0]["outcome"] == "already_completed"
    assert env.submitted == []



def test_invisible_target_remains_unresolved(tmp_path, monkeypatch):
    env = RepairEnvironment(tmp_path, monkeypatch)
    a = env.seed("selected-a", 8)
    env.pending.clear()
    result = controller.run_rolling_drain(
        capacity=2, targets_only=True, resume_action_files=(a,), live_status=env.live_status,
    )
    assert result["target_outcome"]["status"] == "partial"
    assert result["target_outcome"]["items"][0]["reason"] == "target_not_visible"
    assert env.submitted == []



def test_old_attempt_cannot_silently_select_a_new_attempt(tmp_path, monkeypatch):
    env = RepairEnvironment(tmp_path, monkeypatch)
    a = env.seed("selected-a", 8)
    decision_validation.record_rejection(a, {}, decision_validation.InvalidDecision("decision_json"))
    engine = cli._live_flow_engine(controlled=True)
    engine.quarantine_invalid_review(a)
    engine.recover_confirmed_no_write(a, mode="controlled_write", confirmed=True)
    with pytest.raises(ValueError, match="production_target_action_stale"):
        controller.run_rolling_drain(
            capacity=2, targets_only=True, recovery_action_files=(a,), live_status=env.live_status,
        )
    assert env.submitted == []



def test_mixed_workflow_targets_preserve_operation_time_order(tmp_path, monkeypatch):
    env = RepairEnvironment(tmp_path, monkeypatch)
    a = env.seed("travel-a", 9)
    b = env.seed("business-b", 8, "business_entertainment")
    env.seed("outside-c", 7)
    result = controller.run_rolling_drain(
        capacity=2, targets_only=True, resume_action_files=(a, b), live_status=env.live_status,
    )
    assert result["target_outcome"]["status"] == "complete"
    assert {row["workflow_key"] for row in result["target_outcome"]["items"]} == {"travel_expense", "business_entertainment"}
    assert env.submitted == ["business-b", "travel-a"]



def test_conflicting_resume_and_recovery_inputs_are_rejected(tmp_path, monkeypatch):
    env = RepairEnvironment(tmp_path, monkeypatch)
    a = env.seed("selected-a", 8)
    with pytest.raises(ValueError, match="production_target_scope_conflict"):
        controller.run_rolling_drain(capacity=2, targets_only=True, resume_action_files=(a,),
                                    recovery_action_files=(a,), live_status=env.live_status)
    assert env.submitted == []



def test_preview_target_is_never_promoted_to_write(tmp_path, monkeypatch):
    env = RepairEnvironment(tmp_path, monkeypatch)
    env.add("preview-a", 8)
    action = cli._live_flow_engine().next_action("travel_expense")
    a = Path(action["action_file"])
    with pytest.raises(ValueError, match="production_target_resume_invalid"):
        controller.run_rolling_drain(capacity=2, targets_only=True, resume_action_files=(a,), live_status=env.live_status)
    assert env.submitted == []
    assert json.loads(a.with_name("state.json").read_text())["write_state"] == "not_requested"



def test_missing_submission_receipt_cannot_be_counted_complete(tmp_path, monkeypatch):
    env = RepairEnvironment(tmp_path, monkeypatch)
    a = env.seed("selected-a", 8)
    decision = tmp_path / "decision.json"
    decision.write_text(json.dumps(_needs_human_decision(attempt_state.read_action(a))))
    cli._live_flow_engine(controlled=True).complete_review(a, decision)
    a.with_name("execution-receipt.json").unlink()
    with pytest.raises(ValueError, match="production_target_resume_invalid"):
        controller.run_rolling_drain(capacity=2, targets_only=True, resume_action_files=(a,), live_status=env.live_status)
    assert env.submitted == ["selected-a"]



@pytest.mark.parametrize("gate", ["manual_stop", "writer", "identity"])
def test_existing_global_start_gates_still_block_targets(tmp_path, monkeypatch, gate):
    from contextlib import nullcontext
    from apps.oa_approval import runtime_control

    env = RepairEnvironment(tmp_path, monkeypatch)
    a = env.seed("selected-a", 8)
    if gate == "manual_stop":
        runtime_control.request_stop()
    if gate == "identity":
        env.pending[0]["operator_id"] = "synthetic-other"
    held = runtime_control.global_writer_lease() if gate == "writer" else nullcontext()
    with held:
        if gate == "identity":
            with pytest.raises(ValueError) as raised:
                controller.run_rolling_drain(capacity=2, targets_only=True, resume_action_files=(a,), live_status=env.live_status)
            outcome = raised.value.target_outcome
        else:
            result = controller.run_rolling_drain(capacity=2, targets_only=True, resume_action_files=(a,), live_status=env.live_status)
            assert result["kind"] == "approval_startup_gate"
            outcome = result["target_outcome"]
    assert outcome["status"] == "blocked" and outcome["target_count"] == 1
    assert env.submitted == []



@pytest.mark.parametrize("damage", ["counter", "extra", "identity", "state", "attempt", "external_state", "unbound_valid"])
def test_supervisor_refuses_malformed_target_delivery(tmp_path, damage):
    import sys
    from uuid import uuid4
    from scripts.finance_preaudit_run import supervise

    outcome = {"schema_version": 1, "kind": "preaudit_target_outcome", "status": "complete",
               "target_count": 1, "completed": 1, "unresolved": 0, "items": [{
                   "item_key": "a" * 64, "workflow_key": "travel_expense", "source_attempt": 1,
                   "attempt": 1, "operation": "resume", "execution_state": "closed",
                   "business_state": "needs_human", "write_state": "submitted",
                   "outcome": "submitted_confirmed", "reason": "handled_not_pending", "evidence_verified": True}]}
    if damage == "counter":
        outcome["completed"] = 2
    elif damage == "extra":
        outcome["raw_private_value"] = "must-not-be-copied"
    elif damage == "identity":
        outcome["items"][0]["item_key"] = "raw-private-identity"
    elif damage == "state":
        outcome["items"][0].update(write_state="authorized", execution_state="unknown", business_state="not_available", attempt=None)
    elif damage == "attempt":
        outcome["items"][0].update(operation="recover", attempt=4)
    elif damage == "external_state":
        outcome["items"][0].update(outcome="confirmed_no_resubmit", execution_state="unknown", business_state="pass", write_state="authorized")
    code = '''import os,json,datetime
from pathlib import Path
Path(os.environ['OA_CONTROLLER_EXECUTION_MANIFEST_FILE']).write_text(json.dumps({
 'kind':'preaudit_controller_run_manifest','controller_pid':os.getpid(),
 'run_id':'a'*32,'controller_instance_id':'b'*32,'state':'completed',
 'children_reaped':True,'code_revision':'c'*40,
 'started_at':datetime.datetime.now(datetime.timezone.utc).isoformat()}))
'''
    code += "print(" + repr(json.dumps({"kind": "preaudit_production_rolling_drain", "target_outcome": outcome})) + ")"
    receipt = supervise([sys.executable, "-c", code], directory=tmp_path / uuid4().hex, owner_task_id=str(uuid4()))
    assert receipt["result"] == "completed"
    assert receipt["target_outcome"]["status"] == "blocked"
    assert "private" not in json.dumps(receipt["target_outcome"])



def test_external_handled_target_with_changed_frozen_material_is_not_complete(tmp_path, monkeypatch):
    env = RepairEnvironment(tmp_path, monkeypatch)
    a = env.seed("selected-a", 8)
    changed = False

    def live():
        nonlocal changed
        if not changed:
            env.handled.append(env.pending.pop())
            a.with_name("material.json").write_text("{}")
            changed = True
        return env.live_status()

    result = controller.run_rolling_drain(
        capacity=2, targets_only=True, resume_action_files=(a,), live_status=live,
    )
    assert env.submitted == []
    assert result["target_outcome"]["status"] == "blocked"
    assert result["target_outcome"]["completed"] == 0
    assert result["target_outcome"]["items"][0]["evidence_verified"] is False



def test_target_resume_preserves_an_active_completion(tmp_path, monkeypatch):
    import fcntl

    env = RepairEnvironment(tmp_path, monkeypatch)
    action = env.seed("selected-a", 8)
    before = action.with_name("state.json").read_bytes()
    engine = cli._live_flow_engine(controlled=True)
    with action.with_name("complete.lock").open("wb") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(ValueError, match="completion_in_progress"):
            engine.resume_target_review(action)
    assert action.with_name("state.json").read_bytes() == before
    assert env.submitted == []


@pytest.mark.parametrize("handled_time", ["2026-08-21T08:00:00+08:00", ""])
def test_older_or_unknown_handled_version_cannot_complete_new_target(tmp_path, monkeypatch, handled_time):
    env = RepairEnvironment(tmp_path, monkeypatch)
    action = env.seed("selected-a", 8)
    row = env.pending.pop()
    env.handled.append({**row, "received_at": handled_time})
    before = {path: path.read_bytes() for path in action.parent.iterdir() if path.is_file()}

    result = controller.run_rolling_drain(
        capacity=2, targets_only=True, resume_action_files=(action,), live_status=env.live_status,
    )

    outcome = result["target_outcome"]
    assert outcome["status"] in {"partial", "blocked"} and outcome["completed"] == 0
    assert outcome["items"][0]["outcome"] == "unresolved"
    assert outcome["items"][0]["evidence_verified"] is False
    assert env.submitted == []
    assert all(path.read_bytes() == raw for path, raw in before.items())


def test_target_inspection_rejects_another_valid_attempt_state(tmp_path, monkeypatch):
    from apps.oa_finance_preaudit import FlowEngine

    env = RepairEnvironment(tmp_path, monkeypatch)
    selected = env.seed("selected-a", 8)
    outside = env.seed("outside-b", 9)
    selected.with_name("state.json").write_bytes(outside.with_name("state.json").read_bytes())

    with pytest.raises(ValueError, match="production_target_action_invalid"):
        FlowEngine.inspect_target_action(env.root, selected)
    assert env.submitted == []


@pytest.mark.parametrize("gate", ["manual_stop", "writer", "active_controller"])
def test_supervised_early_stop_keeps_bound_target_results(tmp_path, monkeypatch, capsys, gate):
    from contextlib import ExitStack
    import sys
    from uuid import uuid4
    from apps.oa_approval import runtime_control
    from apps.oa_finance_preaudit import controller_handoff
    from scripts import finance_preaudit_run as supervisor

    env = RepairEnvironment(tmp_path, monkeypatch)
    actions = [env.seed("selected-a", 8), env.seed("selected-b", 9)]
    rows = list(env.pending)
    code = f'''import sys
from pathlib import Path
import pytest
from tests.finance_preaudit.test_targeted_recovery import RepairEnvironment
from scripts import finance_preaudit_concurrency_probe as controller
patch = pytest.MonkeyPatch()
env = RepairEnvironment(Path({str(tmp_path)!r}), patch)
env.pending = {rows!r}
raise SystemExit(controller.main(sys.argv[1:]))
'''
    original_popen = supervisor.subprocess.Popen
    def recorded_controller(command, **kwargs):
        if list(command[1:3]) == ["-m", "scripts.finance_preaudit_concurrency_probe"]:
            command = [sys.executable, "-c", code, *command[3:]]
        return original_popen(command, **kwargs)
    monkeypatch.setattr(supervisor.subprocess, "Popen", recorded_controller)
    monkeypatch.setattr(supervisor.approval_guard, "observe_project_errors", lambda _: None)
    command = [sys.executable, "-m", "scripts.finance_preaudit_concurrency_probe",
               "--rolling", "--capacity", "2", "--confirm-write", "YES", "--targets-only",
               "--resume-action-file", str(actions[0]), "--resume-action-file", str(actions[1])]
    with ExitStack() as stack:
        manifest = None
        if gate == "manual_stop":
            runtime_control.request_stop()
        elif gate == "writer":
            stack.enter_context(runtime_control.global_writer_lease())
        else:
            stack.enter_context(runtime_control.global_controller_lease())
            manifest = controller_handoff.begin({"capacity": 2})
            stack.enter_context(controller.approval_lifecycle.run_scope(manifest, env.project))
        if gate == "manual_stop":
            monkeypatch.setattr(supervisor, "_supervise_detached", lambda command, **kwargs: supervisor.supervise(command, **kwargs))
            assert supervisor.main(["--owner-task-id", str(uuid4()), "--review-provider", "deepseek", "--", *command[3:]]) == 1
            receipt = json.loads(capsys.readouterr().out.splitlines()[-1])
        else:
            receipt = supervisor.supervise(command, directory=tmp_path / "control/executions" / uuid4().hex,
                                           owner_task_id=str(uuid4()))
        path = Path(receipt["receipt_file"])
        outcome = receipt["target_outcome"]
        assert receipt["state"] == "exited" and receipt["result"] != "completed"
        assert outcome["status"] == "blocked" and outcome["target_count"] == 2
        assert outcome["completed"] == 0 and outcome["unresolved"] == 2
        assert receipt["run_id"] is None and receipt["children_reaped"] == "unknown"
        assert receipt["exit_code"] == (0 if gate == "active_controller" else 1)
        report = supervisor.read_execution(path, owner_task_id=receipt["owner_task_id"],
                                           invocation_id=receipt["invocation_id"])
        assert report["target_outcome"] == outcome and report["status"] != "completed"
        assert report["owner_task_id"] == receipt["owner_task_id"] and report["invocation_id"] == receipt["invocation_id"]
        assert report["exit_code"] == receipt["exit_code"] and report["children_reaped"] == "unknown"
        original = path.read_bytes()
        for damage in ("owner", "invocation", "target"):
            changed = json.loads(original)
            if damage == "target":
                changed["target_binding"]["outcome"]["items"][0]["item_key"] = "f" * 64
            else:
                changed["target_binding"]["owner_task_id" if damage == "owner" else "invocation_id"] = uuid4().hex
            path.write_text(json.dumps(changed))
            with pytest.raises(ValueError, match="target_binding"):
                supervisor.read_execution(path, owner_task_id=receipt["owner_task_id"], invocation_id=receipt["invocation_id"])
        path.write_bytes(original)
        output = path.with_name("stdout.log")
        raw_output = output.read_bytes()
        output.write_bytes(b"{}")
        with pytest.raises(ValueError, match="output_mismatch"):
            supervisor.read_execution(path, owner_task_id=receipt["owner_task_id"], invocation_id=receipt["invocation_id"])
        output.write_bytes(raw_output)
        if manifest is not None:
            assert receipt["skip_verification"]["run_id"] == manifest["run_id"]
            assert report["blocking_run_id"] == manifest["run_id"] and "run_id" not in report
            controller_handoff.finish(manifest, status="completed", children_reaped=True, health="healthy")
    assert env.submitted == []


def test_unbound_child_cannot_supply_early_target_results(tmp_path, monkeypatch):
    import sys
    from uuid import uuid4
    from apps.oa_finance_preaudit import target_scope
    from scripts import finance_preaudit_run as supervisor

    env = RepairEnvironment(tmp_path, monkeypatch)
    action = env.seed("selected-a", 8)
    scope = target_scope.TargetScope.bind(env.root, resume=(action,), recover=(), workflows=controller.ROLLING_WORKFLOW_KEYS)
    payload = {"kind": "approval_startup_gate", "status": "blocked", "reason": "existing_stop_preserved",
               "target_outcome": scope.outcome({}, controller_completed=False)}
    receipt = supervisor.supervise([sys.executable, "-c", "print(" + repr(json.dumps(payload)) + ")"],
                                   directory=tmp_path / "control/executions" / uuid4().hex, owner_task_id=str(uuid4()))
    assert receipt["target_outcome"] == {"status": "blocked", "reason": "production_target_outcome_unverified"}
    assert "target_binding" not in receipt
