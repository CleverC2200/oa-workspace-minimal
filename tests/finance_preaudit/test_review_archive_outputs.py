"""The real recursive-archive mistake, reproduced with neutral files only."""

import hashlib
import json
from pathlib import Path

import pytest

from apps.oa_finance_preaudit import review_archive_outputs as outputs


def populate(audit, relative, content=b"neutral evidence"):
    path = audit / relative
    folder = audit
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    for part in Path(relative).parts[:-1]:
        folder = folder / part
        folder.mkdir(exist_ok=True, mode=0o700)
    path.write_bytes(content)
    path.chmod(0o600)
    return path


def test_only_review_evidence_is_bound_and_runtime_caches_are_not_read(tmp_path, monkeypatch):
    audit = tmp_path / "reviewer"
    event = populate(audit, "events.jsonl")
    decision = populate(audit, "decision.json")
    receipt = "tool-receipts/12345678-1234-1234-1234-123456789abc.jsonl"
    native = populate(audit, receipt)
    caches = [populate(audit, name) for name in (
        "codex-home/plugins/cache/example/.git/index",
        "codex-home/skills/example/SKILL.md",
        "native-state/state_5.sqlite",
        "native-logs/native.log",
    )]
    read_bytes = Path.read_bytes

    def observe(path):
        assert path not in caches, "runtime cache must not become permanent evidence"
        return read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", observe)
    bound = {str(path): hashlib.sha256(path.read_bytes()).hexdigest()
             for path in outputs.evidence_paths(audit, [receipt])}

    assert set(bound) == {str(event), str(decision), str(native)}
    assert bound[str(event)] == hashlib.sha256(b"neutral evidence").hexdigest()
    assert all(path.exists() for path in caches)


@pytest.mark.parametrize("receipt", ["../events.jsonl", "tool-receipts/not-a-session.jsonl",
                                    "native-state/state_5.sqlite"])
def test_receipt_declarations_cannot_select_other_files(tmp_path, receipt):
    with pytest.raises(ValueError, match="review_output_scope_invalid"):
        outputs.evidence_paths(tmp_path, [receipt])


def test_missing_and_duplicate_declared_receipts_are_rejected(tmp_path):
    receipt = "tool-receipts/12345678-1234-1234-1234-123456789abc.jsonl"
    with pytest.raises(ValueError, match="review_output_scope_invalid"):
        outputs.evidence_paths(tmp_path, [receipt])
    populate(tmp_path, receipt)
    with pytest.raises(ValueError, match="review_output_scope_invalid"):
        outputs.evidence_paths(tmp_path, [receipt, receipt])


def test_symlinked_evidence_is_rejected(tmp_path):
    external = tmp_path / "external.txt"
    external.write_text("external")
    audit = tmp_path / "reviewer"
    audit.mkdir()
    (audit / "events.jsonl").symlink_to(external)
    with pytest.raises(ValueError, match="review_output_scope_invalid"):
        outputs.evidence_paths(audit, [])


def test_consumer_refuses_new_scope_with_the_real_old_recursive_binding(tmp_path):
    from apps.oa_finance_preaudit import review_batch_report as batch
    from tests.finance_preaudit.test_review_batch_report import archive, digest, save

    root = archive(tmp_path)
    cache = populate(root / "reviewer", "codex-home/plugins/cache/example/.git/index")
    result = json.loads((root / "result.json").read_text())
    # The old host bound every file under reviewer, including this cache.
    result["output_files"] = {str(path): digest(path) for path in
                              (root / "reviewer").rglob("*") if path.is_file()}
    result["output_files_scope"] = outputs.SCOPE
    save(root / "result.json", result)

    with pytest.raises(ValueError, match="review_archive_invalid"):
        batch.build_report(root)
    assert cache.exists()


def test_old_archive_keeps_original_recursive_contract_and_bytes(tmp_path):
    from apps.oa_finance_preaudit import review_batch_report as batch
    from tests.finance_preaudit.test_review_batch_report import archive, digest, save

    root = archive(tmp_path)
    cache = populate(root / "reviewer", "native-state/state_5.sqlite")
    result = json.loads((root / "result.json").read_text())
    result["output_files"] = {str(cache): digest(cache)}
    save(root / "result.json", result)
    before = (root / "result.json").read_bytes()

    assert batch.build_report(root)["usage"]["total_tokens"] == 15
    assert (root / "result.json").read_bytes() == before
    cache.write_bytes(b"changed old bound cache")
    with pytest.raises(ValueError, match="review_archive_invalid"):
        batch.build_report(root)


def test_new_scope_ignores_unbound_native_home_but_still_checks_evidence(tmp_path):
    from apps.oa_finance_preaudit import review_batch_report as batch
    from tests.finance_preaudit.test_review_batch_report import archive, digest, save

    root = archive(tmp_path)
    cache = populate(root / "reviewer", "codex-home/plugins/cache/example/cache.jsonl", b"not a receipt")
    result = json.loads((root / "result.json").read_text())
    result["output_files_scope"] = outputs.SCOPE
    result["output_files"] = {str(path): digest(path)
                              for path in outputs.evidence_paths(root / "reviewer", [])}
    save(root / "result.json", result)
    before = (root / "result.json").read_bytes()

    assert batch.build_report(root)["usage"]["total_tokens"] == 15
    cache.write_bytes(b"regenerated working cache")
    assert batch.build_report(root)["usage"]["total_tokens"] == 15
    assert (root / "result.json").read_bytes() == before
    (root / "reviewer/events.jsonl").write_bytes(b"changed original events")
    with pytest.raises(ValueError, match="review_archive_invalid"):
        batch.build_report(root)


@pytest.mark.parametrize("change", ["unknown_scope", "missing_event"])
def test_new_scope_cannot_silently_fall_back_or_drop_evidence(tmp_path, change):
    from apps.oa_finance_preaudit import review_batch_report as batch
    from tests.finance_preaudit.test_review_batch_report import archive, digest, save

    root = archive(tmp_path)
    result = json.loads((root / "result.json").read_text())
    result["output_files_scope"] = outputs.SCOPE
    result["output_files"] = {str(path): digest(path)
                              for path in outputs.evidence_paths(root / "reviewer", [])}
    if change == "unknown_scope":
        result["output_files_scope"] = "unknown"
    else:
        result["output_files"].pop(str(root / "reviewer/events.jsonl"))
    save(root / "result.json", result)
    with pytest.raises(ValueError, match="review_archive_invalid"):
        batch.build_report(root)
