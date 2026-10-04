"""Plan -> actual reviewer process -> preview -> report, with no business ports."""

import hashlib
import json
from pathlib import Path
import subprocess
import sys

import pytest

from apps.oa_finance_preaudit import flow_engine
from tests.finance_preaudit.test_flow_engine_submission import _material


ROOT = Path(__file__).resolve().parents[2]
BASE = "280fc829e7f30d13d399790e8c8adfc7a7d5612b"
CANDIDATE = "0ab87dc990d87ff1da30f352b71a289a241426b1"


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_text(json.dumps(value))
    path.chmod(0o600)


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def forbidden(*args, **kwargs):
    pytest.fail("OA, Writer, recovery and paid CLI are forbidden")


@pytest.fixture(scope="module")
def sources(tmp_path_factory):
    root = tmp_path_factory.mktemp("sources")
    names = subprocess.check_output(["git", "ls-tree", "-r", "--name-only", CANDIDATE], cwd=ROOT, text=True).splitlines()
    selected = [p for p in names if p.startswith("apps/") and p.endswith(".py") or p.startswith(".agents/skills/oa-finance-preaudit/") and p.endswith(".md") or p in {"requirements-dev.txt", "AGENTS.md"}]
    result = {}
    for revision in (BASE, CANDIDATE):
        folder = root / revision
        for name in selected:
            raw = subprocess.run(["git", "show", revision + ":" + name], cwd=ROOT, capture_output=True)
            if raw.returncode:
                continue
            path = folder / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(raw.stdout)
        result[revision] = folder
    return result


def arm(tmp_path, sources, name, workflow, revision, *, tail="", mismatch=False, image=False):
    import shutil
    root = tmp_path / name
    root.mkdir(mode=0o700)
    shutil.copytree(sources[revision], root / "calling-source")
    def collect(request, attempt):
        material = {**_material(request), "workflow_key": workflow, "declared_fields": [{"path": "main[0].field[1]", "value": tail + ("changed" if mismatch else "")}]}
        if image:
            import fitz
            source = attempt / "attachments/source.png"
            page = attempt / "prepared/page.png"
            pixmap = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 16, 16), False)
            pixmap.clear_with(255)
            source.write_bytes(pixmap.tobytes("png"))
            page.write_bytes(source.read_bytes())
            material.update(resources=[{"request_id": request, "resource_id": "image", "source_resource_id": "image", "local_readonly_path": str(source)}], prepared={"images": [{"resource_id": "image", "path": str(page), "page_number": 1, "kind": "image", "content_sha256": sha(page)}], "manifest": [{"resource_id": "image", "status": "image", "image_count": 1}], "structured_contents": []})
        return material
    engine = flow_engine.FlowEngine(root=root / "runtime/finance_preaudit_flows", discover_candidates=lambda key: [{"request_id": "synthetic", "received_at": "2026-10-04T08:00:00+08:00", "workflow_key": key}], collect_material=collect, submit_frozen=forbidden)
    action = engine.next_action(workflow)
    assert action["action"] == "review_required", action
    sources_map = {str(ROOT / p.relative_to(root / "calling-source")): sha(p) for p in (root / "calling-source").rglob("*") if p.is_file()}
    inputs = {str(p): sha(p) for p in (root / "runtime/finance_preaudit_flows").rglob("*") if p.is_file()}
    save(root / "manifest.json", {"schema_version": 1, "source_revision": revision, "source_role": "production_code_replay" if name == "baseline" else "local_candidate", "source_files": sources_map, "originals": {str(Path(action["action_file"]).with_name("material.json")): sha(Path(action["action_file"]).with_name("material.json"))}, "inputs": inputs, "action_file": action["action_file"]})
    for path in root.rglob("*"):
        path.chmod(0o700 if path.is_dir() else 0o600)
    return root


def fake_cli(tmp_path, *, final="valid", usage=True):
    script = tmp_path / "external-reviewer"
    script.write_text("#!" + sys.executable + "\n" + '''import json,sys,os,base64
from pathlib import Path
args=sys.argv[1:]
prompt=sys.stdin.read()
schema=json.loads(Path(args[args.index('--output-schema')+1]).read_text())
constants={k:v['const'] for k,v in schema['properties'].items() if 'const' in v}
action=constants['action_token']
workflow=constants['workflow_key']
root=Path(args[args.index('--cd')+1]).parent
packet=json.loads(next((root/'runtime').rglob('review-packet.json')).read_text())
items=[i['item_key'] for i in packet['items']]
decision={**constants,'overall':'needs_human','items':[{'item_key':k,'status':'uncertain','conclusion':'请财务核对。','evidence_refs':[],'missing_evidence':['费用对应关系未确认。']} for k in items]}
raw=json.dumps(decision) if ''' + repr(final) + ''' == 'valid' else ''' + repr(final) + '''
Path(args[args.index('--output-last-message')+1]).write_text(raw)
images=[args[i+1] for i,a in enumerate(args) if a=='--image']
content=[{'type':'input_image','image_url':'data:image/png;base64,'+base64.b64encode(Path(p).read_bytes()).decode()} for p in images]
Path(os.environ['CODEX_HOME'],'session.jsonl').write_text(json.dumps({'type':'response_item','payload':{'type':'message','role':'user','content':content}})+'\\n')
print(json.dumps({'type':'turn.completed',**({'usage':{'input_tokens':100,'cached_input_tokens':20,'output_tokens':10}} if ''' + repr(usage) + ''' else {})}),flush=True)
if ''' + repr(final) + ''' == 'cancel':
 import signal,time
 os.kill(os.getppid(),signal.SIGTERM)
 time.sleep(5)
''')
    compile(script.read_text(), str(script), "exec")
    script.chmod(0o700)
    return script


def plan(tmp_path, sources, workflow="business_entertainment", *, purpose="effect", final="valid", usage=True, tail="", mismatch=False, image=False):
    from apps.oa_finance_preaudit import review_experiment
    before = arm(tmp_path, sources, "baseline", workflow, BASE, tail=tail, image=image)
    after = arm(tmp_path, sources, "candidate", workflow, CANDIDATE, tail=tail, mismatch=mismatch, image=image)
    cli = fake_cli(tmp_path, final=final, usage=usage)
    path = tmp_path / "plan.json"
    review_experiment.freeze_pair(before, after, path, repository=ROOT, reviewer=cli, purpose=purpose, timeout_seconds=60, maximum_cli_starts=2)
    authorization = tmp_path / "authorization.json"
    save(authorization, {"plan_sha256": sha(path), "maximum_cli_starts": 2, "authority_reference": "explicit offline rehearsal", "oa_write_authorized": False})
    return path, authorization, before, after


def test_unchanged_flow_is_rejected_before_any_reviewer_start(tmp_path, sources, monkeypatch):
    from apps.oa_finance_preaudit import review_experiment
    monkeypatch.setenv("OA_REVIEW_API_KEY", "offline-placeholder")
    path, auth, before, after = plan(tmp_path, sources, "general_expense")
    result = review_experiment.run_pair(path, auth)
    assert result["status"] == "rejected"
    assert result["reason"] == "treatment_not_applied"
    assert result["cli_starts"] == 0
    assert not (before / "reviewer").exists()
    assert not (after / "reviewer").exists()


def test_real_treatment_reaches_preview_and_exact_report(tmp_path, sources, monkeypatch):
    from apps.oa_finance_preaudit import review_experiment
    monkeypatch.setenv("OA_REVIEW_API_KEY", "offline-placeholder")
    path, auth, before, after = plan(tmp_path, sources)
    result = review_experiment.run_pair(path, auth)
    assert result["status"] == "observed", result
    assert result["cli_starts"] == 2
    assert result["benefit"] == "not_established"
    for report in result["trials"].values():
        assert report["usage"].get("total_tokens") == 110, {"report": report, "stderr": (before / "reviewer/stderr.log").read_text()}
        assert report["acceptance"]["preview"] == "verified_closed"
        assert report["timing"]["boundary"] == "input_preparation_to_preview_consumption"
    assert "冻结数据：material" in (after / "reviewer/prompt.txt").read_text()
    assert "冻结数据：material" not in (before / "reviewer/prompt.txt").read_text()
    assert review_experiment.run_pair(path, auth)["cli_starts"] == 0


@pytest.mark.parametrize("final", ["not JSON", "[]", "null"])
def test_failed_final_is_charged_without_business_acceptance(tmp_path, sources, monkeypatch, final):
    from apps.oa_finance_preaudit import review_experiment
    monkeypatch.setenv("OA_REVIEW_API_KEY", "offline-placeholder")
    path, auth, before, after = plan(tmp_path, sources, final=final)
    result = review_experiment.run_pair(path, auth)
    assert result["status"] == "observed", result
    assert result["cli_starts"] == 2
    for report in result["trials"].values():
        assert report["usage"]["status"] == "exact"
        assert report["usage"]["total_tokens"] == 110
        assert report["acceptance"]["preview"] == "not_established"
        assert report["acceptance"]["final_contract"] == "rejected"
    assert (before / "reviewer/decision.json").read_text() == final
    assert (after / "reviewer/decision.json").read_text() == final


@pytest.mark.parametrize("change", ["authority", "source", "input", "schema", "reviewer", "budget", "cancel", "wrong_material"])
def test_drift_authority_and_budget_never_dispatch(tmp_path, sources, monkeypatch, change):
    from apps.oa_finance_preaudit import review_experiment
    monkeypatch.setenv("OA_REVIEW_API_KEY", "offline-placeholder")
    path, auth, before, after = plan(tmp_path, sources, mismatch=change == "wrong_material")
    if change == "authority":
        save(auth, {"plan_sha256": "0" * 64, "maximum_cli_starts": 2})
    elif change == "source":
        (after / "calling-source/apps/oa_finance_preaudit/codex_review_worker.py").write_text("wrong source")
    elif change == "input":
        p = next((after / "runtime").rglob("material.json"))
        p.write_text(p.read_text() + " ")
    elif change == "schema":
        value = json.loads(path.read_text())
        value["preparations"]["candidate"]["conditions"]["schema"] = "0" * 64
        save(path.with_name("changed.json"), value)
        path = path.with_name("changed.json")
        save(auth, {"plan_sha256": sha(path), "maximum_cli_starts": 2, "authority_reference": "offline", "oa_write_authorized": False})
    elif change == "reviewer":
        (tmp_path / "external-reviewer").write_text("#!/bin/false\n")
    elif change == "budget":
        authority = json.loads(auth.read_text())
        authority["maximum_cli_starts"] = 1
        save(auth, authority)
    result = review_experiment.run_pair(path, auth, stop_requested=lambda: change == "cancel")
    assert result["status"] == "rejected", result
    assert result["cli_starts"] == 0
    assert not (before / "reviewer").exists()
    assert not (after / "reviewer").exists()


def test_capacity_fallback_is_not_an_effect_trial(tmp_path, sources, monkeypatch):
    from apps.oa_finance_preaudit import review_experiment
    monkeypatch.setenv("OA_REVIEW_API_KEY", "offline-placeholder")
    path, auth, before, after = plan(tmp_path, sources, tail="x" * 100_001)
    result = review_experiment.run_pair(path, auth)
    assert result["reason"] == "candidate_capacity_fallback"
    assert result["cli_starts"] == 0


def test_explicit_same_path_diagnostic_has_no_optimization_benefit(tmp_path, sources, monkeypatch):
    from apps.oa_finance_preaudit import review_experiment
    monkeypatch.setenv("OA_REVIEW_API_KEY", "offline-placeholder")
    path, auth, before, after = plan(tmp_path, sources, "business_support_fee", purpose="diagnostic", usage=False)
    result = review_experiment.run_pair(path, auth)
    assert result["status"] == "observed", result
    assert result["purpose"] == "diagnostic"
    assert result["cli_starts"] == 2
    assert result["benefit"] == "not_established"
    assert all(r["usage"]["status"] == "unknown" for r in result["trials"].values())


def test_initial_image_delivery_is_not_zero_or_proven_observation(tmp_path, sources, monkeypatch):
    from apps.oa_finance_preaudit import review_experiment
    monkeypatch.setenv("OA_REVIEW_API_KEY", "offline-placeholder")
    path, auth, before, after = plan(tmp_path, sources, image=True)
    result = review_experiment.run_pair(path, auth)
    assert result["status"] == "observed", result
    images = result["trials"]["candidate"]["acceptance"]["original_images"]
    assert images["initial_delivered"] == 1
    assert images["view_returned"] == 0
    assert images["observation"] == "not_established_initial_delivery_only"


def test_cancelled_reviewer_preserves_cost_and_stops_next_dispatch(tmp_path, sources, monkeypatch):
    from apps.oa_finance_preaudit import review_experiment
    monkeypatch.setenv("OA_REVIEW_API_KEY", "offline-placeholder")
    path, auth, before, after = plan(tmp_path, sources, final="cancel")
    result = review_experiment.run_pair(path, auth)
    assert result["status"] == "stopped", result
    assert result["cli_starts"] == 1
    assert result["not_started"] == ["candidate"]
    assert result["trials"]["baseline"]["usage"]["total_tokens"] == 110
    assert result["trials"]["baseline"]["acceptance"]["process"]["state"] == "cancelled"
    assert not (after / "reviewer").exists()


def test_quality_reference_is_not_promoted_to_production_by_arm_name(tmp_path, sources, monkeypatch):
    from apps.oa_finance_preaudit import review_experiment
    monkeypatch.setenv("OA_REVIEW_API_KEY", "offline-placeholder")
    before = arm(tmp_path, sources, "baseline", "business_entertainment", BASE)
    after = arm(tmp_path, sources, "candidate", "business_entertainment", CANDIDATE)
    manifest = json.loads((before / "manifest.json").read_text())
    manifest["source_role"] = "quality_reference"
    save(before / "manifest.json", manifest)
    path = tmp_path / "plan.json"
    review_experiment.freeze_pair(before, after, path, repository=ROOT, reviewer=fake_cli(tmp_path), purpose="effect", timeout_seconds=60, maximum_cli_starts=2)
    auth = tmp_path / "authority.json"
    save(auth, {"plan_sha256": sha(path), "maximum_cli_starts": 2, "authority_reference": "offline", "oa_write_authorized": False})
    result = review_experiment.run_pair(path, auth)
    assert result["status"] == "observed", result
    report = result["trials"]["baseline"]
    assert report["source"]["role"]["declared"] == "quality_reference"
    assert report["acceptance"]["production_loaded"] == "not_established"
    assert report["timing"]["start_receipt"]["status"] == "bound_source_host_receipt"


def test_expired_explicit_authorization_never_starts_reviewer(tmp_path, sources, monkeypatch):
    from apps.oa_finance_preaudit import review_experiment
    monkeypatch.setenv("OA_REVIEW_API_KEY", "offline-placeholder")
    path, auth, before, after = plan(tmp_path, sources)
    value = json.loads(auth.read_text())
    value["expires_at"] = "2020-01-01T00:00:00+00:00"
    save(auth, value)
    result = review_experiment.run_pair(path, auth)
    assert result["reason"] == "authorization_expired"
    assert result["cli_starts"] == 0
    assert not (before / "reviewer").exists()
    assert not (after / "reviewer").exists()


@pytest.mark.parametrize("missing", ["AGENTS.md", ".agents/skills/oa-finance-preaudit/references/flows/business-entertainment.md"])
def test_unlisted_actual_instructions_cannot_be_used_by_source_host(tmp_path, sources, missing):
    from apps.oa_finance_preaudit import review_experiment
    before = arm(tmp_path, sources, "baseline", "business_entertainment", BASE)
    after = arm(tmp_path, sources, "candidate", "business_entertainment", CANDIDATE)
    manifest = json.loads((after / "manifest.json").read_text())
    del manifest["source_files"][str(ROOT / missing)]
    save(after / "manifest.json", manifest)
    (after / "calling-source" / missing).write_text("unlisted instructions drifted")
    with pytest.raises(ValueError):
        review_experiment.freeze_pair(before, after, tmp_path / "plan.json", repository=ROOT,
            reviewer=fake_cli(tmp_path), purpose="effect", timeout_seconds=60, maximum_cli_starts=2)
    assert not (after / "reviewer").exists()


@pytest.mark.parametrize("module", ["flow_evals.py", "flow_engine.py"])
def test_local_report_dependency_drift_is_rejected_in_fresh_process(tmp_path, sources, monkeypatch, module):
    monkeypatch.setenv("OA_REVIEW_API_KEY", "offline-placeholder")
    path, auth, before, after = plan(tmp_path, sources)
    dependency = ROOT / "apps/oa_finance_preaudit" / module
    original = dependency.read_bytes()
    try:
        dependency.write_bytes(original + b"\n# controlled local dependency drift\n")
        output = subprocess.check_output([sys.executable, "-c",
            "import json,sys; from pathlib import Path; from apps.oa_finance_preaudit.review_experiment import run_pair; print(json.dumps(run_pair(Path(sys.argv[1]),Path(sys.argv[2]))))",
            str(path), str(auth)], cwd=ROOT, text=True)
        result = json.loads(output)
    finally:
        dependency.write_bytes(original)
    assert result["reason"] == "host_drift"
    assert result["cli_starts"] == 0
    assert not (before / "reviewer").exists()
    assert not (after / "reviewer").exists()


def test_host_timeout_after_real_cli_launch_does_not_report_zero_or_not_started(tmp_path, sources, monkeypatch):
    import time
    from apps.oa_finance_preaudit import review_experiment
    monkeypatch.setenv("OA_REVIEW_API_KEY", "offline-placeholder")
    path, auth, before, after = plan(tmp_path, sources)
    original = subprocess.Popen
    class DispatchTimeout:
        def __init__(self, process, command):
            self.process, self.command = process, command
        def __getattr__(self, name):
            return getattr(self.process, name)
        def communicate(self, *args, **kwargs):
            deadline = time.monotonic() + 10
            while not (before / "cli-start.json").exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            assert (before / "cli-start.json").exists()
            raise subprocess.TimeoutExpired(self.command, kwargs.get("timeout"))
    def start(command, **kwargs):
        process = original(command, **kwargs)
        return DispatchTimeout(process, command) if command[-1] == "execute" else process
    monkeypatch.setattr(review_experiment.subprocess, "Popen", start)
    result = review_experiment.run_pair(path, auth)
    assert result["status"] == "stopped", result
    assert result["cli_starts"] in {1, "unknown"}
    assert "baseline" not in result["not_started"]
    assert not (after / "reviewer").exists()


def test_parent_cancel_in_real_host_spawn_window_reaps_host_and_never_dispatches_candidate(tmp_path, sources, monkeypatch):
    import os
    import signal
    from apps.oa_finance_preaudit import review_experiment
    monkeypatch.setenv("OA_REVIEW_API_KEY", "offline-placeholder")
    path, auth, before, after = plan(tmp_path, sources)
    driver = tmp_path / "cancel-driver.py"
    pid_file, result_file = tmp_path / "host-pid.json", tmp_path / "cancel-result.json"
    driver.write_text('''import json,os,signal,subprocess,sys,time
from pathlib import Path
sys.path.insert(0,sys.argv[1])
from apps.oa_finance_preaudit import review_experiment
original=subprocess.Popen
def start(command,**kwargs):
 process=original(command,**kwargs)
 if command[-1]=="execute":
  Path(sys.argv[4]).write_text(json.dumps(process.pid))
  os.kill(os.getpid(),signal.SIGTERM)
  time.sleep(.1)
 return process
subprocess.Popen=start
result=review_experiment.run_pair(Path(sys.argv[2]),Path(sys.argv[3]))
Path(sys.argv[5]).write_text(json.dumps(result))
''')
    process = subprocess.Popen([sys.executable, str(driver), str(ROOT), str(path), str(auth),
                                str(pid_file), str(result_file)], cwd=ROOT)
    try:
        assert process.wait(timeout=20) == 0
        result = json.loads(result_file.read_text())
        assert result["status"] == "stopped"
        assert not (after / "reviewer").exists()
        with pytest.raises(ProcessLookupError):
            os.kill(json.loads(pid_file.read_text()), 0)
    finally:
        for receipt in (pid_file, before / "cli-start.json"):
            if receipt.exists():
                value = json.loads(receipt.read_text())
                pid = value["pid"] if isinstance(value, dict) else value
                try:
                    os.killpg(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        if process.poll() is None:
            process.kill()
            process.wait()


def test_non_main_dispatch_without_signal_handoff_is_rejected_before_start(tmp_path, sources, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from apps.oa_finance_preaudit import review_experiment
    monkeypatch.setenv("OA_REVIEW_API_KEY", "offline-placeholder")
    path, auth, before, after = plan(tmp_path, sources)
    with ThreadPoolExecutor(max_workers=1) as pool:
        result = pool.submit(review_experiment.run_pair, path, auth).result(timeout=20)
    assert result["reason"] == "cancellation_guard_unavailable"
    assert result["cli_starts"] == 0
    assert not (before / "reviewer").exists()
    assert not (after / "reviewer").exists()


def test_cancellation_after_prepare_output_never_proceeds_to_any_model(tmp_path, sources, monkeypatch):
    from apps.oa_finance_preaudit import review_experiment
    monkeypatch.setenv("OA_REVIEW_API_KEY", "offline-placeholder")
    path, auth, before, after = plan(tmp_path, sources)
    original = subprocess.Popen
    class FinishedPrepare:
        def __init__(self, process): self.process = process
        def __getattr__(self, name): return getattr(self.process, name)
        def communicate(self, *args, **kwargs):
            output = self.process.communicate(*args, **kwargs)
            # Real source host has exited; exercise the installed owner handler.
            import os, signal
            os.kill(os.getpid(), signal.SIGTERM)
            return output
    def start(command, **kwargs):
        process = original(command, **kwargs)
        return FinishedPrepare(process) if command[-1] == "prepare" else process
    monkeypatch.setattr(review_experiment.subprocess, "Popen", start)
    result = review_experiment.run_pair(path, auth)
    assert result["reason"] == "source_host_cancelled"
    assert result["cli_starts"] == 0
    assert not (before / "reviewer").exists()
    assert not (after / "reviewer").exists()
