from __future__ import annotations

import io
import json
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import pytest

from apps.oa_finance_preaudit import cli
from apps.oa_approval import frozen_writer


PUBLIC_COMMANDS = {"next-action", "complete-review", "status", "prune", "clean"}


def test_public_cli_exposes_only_the_supported_commands() -> None:
    parser = cli.build_parser()
    subparsers = next(
        action for action in parser._actions if action.dest == "command"
    )
    assert set(subparsers.choices) == PUBLIC_COMMANDS


def test_status_live_check_stays_inside_existing_public_command(
    tmp_path: Path,
) -> None:
    root = tmp_path / "runtime"
    root.mkdir()
    output = io.StringIO()

    with redirect_stdout(output):
        exit_code = cli.main(
            ["status", "--root", str(root), "--check-live"],
            runtime_status=lambda _root: {
                "schema_version": 1,
                "kind": "preaudit_runtime_status",
            },
            live_status=lambda: {
                "status": "ok",
                "write_state": "not_requested",
            },
        )

    assert exit_code == 0
    payload = json.loads(output.getvalue())
    assert payload["live_check"] == {
        "status": "ok",
        "write_state": "not_requested",
    }


def test_status_reconciliation_stays_read_only_inside_status_command(
    tmp_path: Path,
) -> None:
    root = tmp_path / "runtime"
    root.mkdir()
    output = io.StringIO()
    calls: list[Path] = []

    with redirect_stdout(output):
        exit_code = cli.main(
            ["status", "--root", str(root), "--reconcile"],
            runtime_status=lambda _root: {
                "schema_version": 1,
                "kind": "preaudit_runtime_status",
            },
            reconcile_status=lambda current_root: (
                calls.append(current_root)
                or {
                    "schema_version": 1,
                    "kind": "preaudit_reconciliation",
                    "read_only": True,
                }
            ),
        )

    assert exit_code == 0
    assert calls == [root]
    assert json.loads(output.getvalue())["reconciliation"]["read_only"] is True


def test_status_combines_fresh_live_queue_counts_without_identities(
    tmp_path: Path,
) -> None:
    root = tmp_path / "runtime"
    root.mkdir()
    output = io.StringIO()

    with redirect_stdout(output):
        exit_code = cli.main(
            [
                "status",
                "--root",
                str(root),
                "--check-live",
                "--reconcile",
            ],
            runtime_status=lambda _root: {
                "schema_version": 1,
                "kind": "preaudit_runtime_status",
                "operations": {"remaining": 1},
            },
            live_status=lambda: {
                "schema_version": 1,
                "kind": "preaudit_live_status",
                "status": "ok",
                "oa": {
                    "execution_identity": "verified",
                    "supported": 5,
                    "unsupported": 1,
                },
            },
            reconcile_status=lambda _root: {
                "schema_version": 1,
                "kind": "preaudit_reconciliation",
                "read_only": True,
                "quarantine": {
                    "pending_quarantined": 2,
                    "actionable_pending": 3,
                },
            },
        )

    assert exit_code == 0
    payload = json.loads(output.getvalue())
    assert payload["operations"]["live_queue"] == {
        "evidence_status": "exact",
        "remaining": 5,
        "actionable": 3,
        "quarantined": 2,
        "unsupported": 1,
    }
    assert "request_id" not in json.dumps(payload)


def test_live_global_reconciliation_excludes_unsupported_pending(
    tmp_path: Path,
    monkeypatch,
) -> None:
    configs = [
        {
            "flow_key": "travel_expense",
            "workflow_id": "supported-workflow",
            "workflow_name": "supported",
        }
    ]

    class API:
        def pending_rows(self, **_kwargs) -> list[dict]:
            return [
                {
                    "request_id": "supported-request",
                    "received_at": "2026-08-25T08:00:00+08:00",
                    "workflow_id": "supported-workflow",
                    "workflow_name": "supported",
                },
                {
                    "request_id": "unsupported-request",
                    "received_at": "2026-08-25T07:00:00+08:00",
                    "workflow_id": "unsupported-workflow",
                    "workflow_name": "unsupported",
                },
            ]

        def handled_rows(self, **_kwargs) -> list[dict]:
            return []

    captured: list[list[dict]] = []
    monkeypatch.setattr(
        cli.legacy_flow,
        "load_all_configs",
        lambda: configs,
    )
    monkeypatch.setattr(
        cli.legacy_api,
        "build_api",
        lambda _config: API(),
    )
    monkeypatch.setattr(
        cli.reconciliation,
        "reconcile",
        lambda _root, *, pending_rows, handled_rows, workflow_key: (
            captured.append(pending_rows)
            or {
                "handled_rows": handled_rows,
                "workflow_key": workflow_key,
            }
        ),
    )

    cli._live_reconciliation(tmp_path)

    assert captured == [
        [
            {
                "request_id": "supported-request",
                "received_at": "2026-08-25T08:00:00+08:00",
            }
        ]
    ]


def test_live_flow_engine_can_pin_discovery_to_global_fifo_identity(
    tmp_path: Path,
    monkeypatch,
) -> None:
    configs = [
        {
            "flow_key": "travel_expense",
            "workflow_id": "travel-workflow",
            "workflow_name": "travel",
        }
    ]

    class API:
        def pending_rows(self, **_kwargs) -> list[dict]:
            return [
                {
                    "request_id": "fifo-head",
                    "received_at": "2026-08-25T08:00:00+08:00",
                    "workflow_id": "travel-workflow",
                    "workflow_name": "travel",
                },
                {
                    "request_id": "newer-request",
                    "received_at": "2026-08-25T09:00:00+08:00",
                    "workflow_id": "travel-workflow",
                    "workflow_name": "travel",
                },
            ]

        def handled_rows(self, **_kwargs) -> list[dict]:
            return []

    monkeypatch.setenv("OA_FINANCE_PREAUDIT_FLOW_ROOT", str(tmp_path))
    monkeypatch.setattr(
        cli.legacy_flow,
        "load_all_configs",
        lambda: configs,
    )
    monkeypatch.setattr(
        cli.legacy_api,
        "build_api",
        lambda _config: API(),
    )

    engine = cli._live_flow_engine(
        selected_identity=("fifo-head", "2026-08-25T08:00:00+08:00")
    )

    assert engine._discover_candidates("travel_expense") == [
        {
            "request_id": "fifo-head",
            "received_at": "2026-08-25T08:00:00+08:00",
            "workflow_key": "travel_expense",
        }
    ]


def test_status_live_check_fails_closed_without_configuration(
    tmp_path: Path,
) -> None:
    root = tmp_path / "runtime"
    root.mkdir()
    output = io.StringIO()
    stderr = io.StringIO()

    with redirect_stdout(output), redirect_stderr(stderr):
        exit_code = cli.main(
            ["status", "--root", str(root), "--check-live"],
            runtime_status=lambda _root: {
                "schema_version": 1,
                "kind": "preaudit_runtime_status",
            },
            live_status=lambda: (_ for _ in ()).throw(
                RuntimeError("runtime_live_configuration_missing")
            ),
        )

    assert exit_code == 1
    assert json.loads(output.getvalue())["failure"] == {
        "code": "runtime_live_configuration_missing",
        "stage": "status",
    }
    assert stderr.getvalue().strip() == "runtime_live_configuration_missing"


def test_frozen_writer_is_an_internal_adapter_without_a_cli() -> None:
    assert not hasattr(frozen_writer, "build_parser")
    assert not hasattr(frozen_writer, "main")


@pytest.mark.parametrize(
    "removed_command",
    (
        "collect-case",
        "list-pending",
        "list-handled",
        "claim",
        "begin-submit",
        "seal-review",
        "release-manifest",
    ),
)
def test_removed_mechanical_commands_are_not_parseable(removed_command: str) -> None:
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args([removed_command])


def test_next_action_uses_the_deep_module_and_prints_one_json() -> None:
    receipts: list[dict] = []

    class Engine:
        def next_action(self, workflow: str, *, mode: str) -> dict:
            return {
                "schema_version": 1,
                "kind": "preaudit_action",
                "action": "closed",
                "workflow_key": workflow,
                "mode": mode,
            }

    output = io.StringIO()
    with redirect_stdout(output):
        exit_code = cli.main(
            ["next-action", "--workflow", "travel_expense"],
            flow_engine_factory=Engine,
            invocation_sink=receipts.append,
        )

    assert exit_code == 0
    assert json.loads(output.getvalue())["action"] == "closed"
    assert output.getvalue().count("\n") == 1
    assert receipts[0]["workflow_key"] == "travel_expense"
    assert receipts[0]["command"] == "next-action"
    assert receipts[0]["outcome"] == "success"
    assert receipts[0]["human_reproduction"] == "not_attempted"
    assert set(receipts[0]) == {
        "workflow_key",
        "command",
        "occurred_at",
        "outcome",
        "human_reproduction",
    }


def test_controlled_travel_next_action_requires_rolling_controller() -> None:
    calls: list[tuple[str, str]] = []

    class Engine:
        def next_action(self, workflow: str, *, mode: str) -> dict:
            calls.append((workflow, mode))
            return {
                "schema_version": 1,
                "kind": "preaudit_action",
                "action": "closed",
                "workflow_key": workflow,
                "mode": mode,
            }

    denied = io.StringIO()
    with redirect_stdout(denied):
        denied_code = cli.main(
            [
                "next-action",
                "--workflow",
                "travel_expense",
                "--mode",
                "controlled-write",
            ],
            flow_engine_factory=Engine,
        )

    allowed = io.StringIO()
    with redirect_stdout(allowed):
        allowed_code = cli.main(
            [
                "next-action",
                "--workflow",
                "travel_expense",
                "--mode",
                "controlled-write",
                "--confirm-write",
                "YES",
            ],
            flow_engine_factory=Engine,
        )

    assert denied_code == 1
    assert json.loads(denied.getvalue())["failure"]["code"] == "run_mode_unauthorized"
    assert allowed_code == 1
    assert json.loads(allowed.getvalue())["failure"]["code"] == (
        "production_rolling_controller_required"
    )
    assert calls == []


def test_recovery_requires_explicit_confirmation_and_uses_engine_gate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "OA_FINANCE_PREAUDIT_CONTROLLER_LOCK_FILE",
        str(tmp_path / "rolling-controller.lock"),
    )
    calls: list[tuple[Path, str, bool, bool, str | None]] = []

    class Engine:
        def recover_confirmed_no_write(
            self,
            action_file: Path,
            *,
            mode: str,
            confirmed: bool,
            confirmed_unknown_no_write: bool = False,
            confirmed_request_id: str | None = None,
        ) -> dict:
            calls.append(
                (
                    action_file,
                    mode,
                    confirmed,
                    confirmed_unknown_no_write,
                    confirmed_request_id,
                )
            )
            return {
                "schema_version": 1,
                "kind": "preaudit_action",
                "action": "review_required",
                "workflow_key": "travel_expense",
            }

    action = tmp_path / "action.json"
    action.write_text(
        json.dumps(
            {
                "workflow_key": "travel_expense",
                "action": "needs_admin",
                "execution_state": "needs_admin",
                "write_state": "unknown",
                "reason": "writer_result_unknown",
                "request_id": "recorded-request",
            }
        ),
        encoding="utf-8",
    )
    action.with_name("state.json").write_text(
        action.read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    denied = io.StringIO()
    with redirect_stdout(denied):
        assert cli.main(
            [
                "next-action",
                "--workflow",
                "travel_expense",
                "--recover-action-file",
                str(action),
            ],
            flow_engine_factory=Engine,
        ) == 1
    assert json.loads(denied.getvalue())["failure"]["code"] == (
        "recovery_confirmation_required"
    )

    allowed = io.StringIO()
    with redirect_stdout(allowed):
        assert cli.main(
            [
                "next-action",
                "--workflow",
                "travel_expense",
                "--recover-action-file",
                str(action),
                "--confirm-recovery",
                "YES",
            ],
            flow_engine_factory=Engine,
        ) == 0
    assert calls == [(action, "preview", True, False, None)]
    assert json.loads(allowed.getvalue())["action"] == "review_required"

    controlled = io.StringIO()
    with redirect_stdout(controlled):
        assert cli.main(
            [
                "next-action",
                "--workflow",
                "travel_expense",
                "--mode",
                "controlled-write",
                "--confirm-write",
                "YES",
                "--recover-action-file",
                str(action),
                "--confirm-recovery",
                "YES",
                "--confirm-unknown-no-write",
                "YES",
                "--confirm-unknown-request-id",
                "recorded-request",
            ],
            flow_engine_factory=Engine,
        ) == 0
    assert calls[-1] == (
        action,
        "controlled_write",
        True,
        True,
        "recorded-request",
    )
    assert json.loads(controlled.getvalue())["action"] == "review_required"

    monkeypatch.setattr(cli.runtime_control, "controller_active", lambda: True)
    active = io.StringIO()
    with redirect_stdout(active):
        assert cli.main(
            [
                "next-action",
                "--workflow",
                "travel_expense",
                "--mode",
                "controlled-write",
                "--confirm-write",
                "YES",
                "--recover-action-file",
                str(action),
                "--confirm-recovery",
                "YES",
                "--confirm-unknown-no-write",
                "YES",
                "--confirm-unknown-request-id",
                "recorded-request",
            ],
            flow_engine_factory=Engine,
        ) == 1
    assert json.loads(active.getvalue())["failure"]["code"] == (
        "recovery_runtime_active"
    )
    assert len(calls) == 2

    monkeypatch.setattr(cli.runtime_control, "controller_active", lambda: False)
    monkeypatch.setattr(cli.runtime_control, "writer_active", lambda: True)
    writer_active = io.StringIO()
    with redirect_stdout(writer_active):
        assert cli.main(
            [
                "next-action",
                "--workflow",
                "travel_expense",
                "--mode",
                "controlled-write",
                "--confirm-write",
                "YES",
                "--recover-action-file",
                str(action),
                "--confirm-recovery",
                "YES",
                "--confirm-unknown-no-write",
                "YES",
                "--confirm-unknown-request-id",
                "recorded-request",
            ],
            flow_engine_factory=Engine,
        ) == 1
    assert json.loads(writer_active.getvalue())["failure"]["code"] == (
        "recovery_runtime_active"
    )
    assert len(calls) == 2

    monkeypatch.setattr(cli.runtime_control, "writer_active", lambda: False)
    for path in (action, action.with_name("state.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload.update(
            {
                "write_state": "not_requested",
                "reason": "material_collection_failed",
            }
        )
        path.write_text(json.dumps(payload), encoding="utf-8")
    other_reason = io.StringIO()
    with redirect_stdout(other_reason):
        assert cli.main(
            [
                "next-action",
                "--workflow",
                "travel_expense",
                "--mode",
                "controlled-write",
                "--confirm-write",
                "YES",
                "--recover-action-file",
                str(action),
                "--confirm-recovery",
                "YES",
                "--confirm-unknown-no-write",
                "YES",
                "--confirm-unknown-request-id",
                "recorded-request",
            ],
            flow_engine_factory=Engine,
        ) == 1
    assert json.loads(other_reason.getvalue())["failure"]["code"] == (
        "production_rolling_controller_required"
    )
    assert len(calls) == 2

    mismatch = io.StringIO()
    with redirect_stdout(mismatch):
        assert cli.main(
            [
                "next-action",
                "--workflow",
                "business_entertainment",
                "--recover-action-file",
                str(action),
                "--confirm-recovery",
                "YES",
            ],
            flow_engine_factory=Engine,
        ) == 1
    assert json.loads(mismatch.getvalue())["failure"]["code"] == (
        "recovery_not_safe"
    )


def test_cli_failure_emits_sanitized_classification() -> None:
    receipts: list[dict] = []

    class Engine:
        def next_action(self, workflow: str, *, mode: str) -> dict:
            raise ValueError("review_decision_invalid")

    output = io.StringIO()
    with redirect_stdout(output):
        exit_code = cli.main(
            ["next-action", "--workflow", "travel_expense"],
            flow_engine_factory=Engine,
            invocation_sink=receipts.append,
        )

    assert exit_code == 1
    assert receipts == [
        {
            "workflow_key": "travel_expense",
            "command": "next-action",
            "occurred_at": receipts[0]["occurred_at"],
            "outcome": "json_contract_error",
            "human_reproduction": "not_attempted",
        }
    ]
    assert "review_decision_invalid" not in json.dumps(receipts)


def test_metrics_sink_failure_never_changes_command_result() -> None:
    class Engine:
        def next_action(self, workflow: str, *, mode: str) -> dict:
            return {"workflow_key": workflow, "action": "closed"}

    output = io.StringIO()
    with redirect_stdout(output):
        exit_code = cli.main(
            ["next-action", "--workflow", "travel_expense"],
            flow_engine_factory=Engine,
            invocation_sink=lambda _receipt: (_ for _ in ()).throw(
                OSError("metrics unavailable")
            ),
        )

    assert exit_code == 0
    assert json.loads(output.getvalue())["action"] == "closed"


def test_module_entry_persists_global_command_once(
    tmp_path: Path,
) -> None:
    root = tmp_path / "runtime"
    root.mkdir()
    output = io.StringIO()
    with redirect_stdout(output):
        exit_code = cli.main(
            ["status", "--root", str(root)],
            runtime_status=lambda _root: {"kind": "preaudit_runtime_status"},
            persist_invocations=True,
        )

    assert exit_code == 0
    payload = json.loads(
        (root / ".metrics" / "cli-invocations.json").read_text(
            encoding="utf-8"
        )
    )
    assert len(payload["events"]) == 1
    assert payload["events"][0]["workflow_key"] == "global"
    assert all(event["command"] == "status" for event in payload["events"])


def test_module_entry_records_argument_error_without_argv(
    tmp_path: Path,
) -> None:
    root = tmp_path / "runtime"
    root.mkdir()
    stderr = io.StringIO()
    with redirect_stderr(stderr):
        exit_code = cli.entrypoint(
            ["status", "--root", str(root), "--unexpected"]
        )

    assert exit_code == 2
    text = (root / ".metrics" / "cli-invocations.json").read_text(
        encoding="utf-8"
    )
    payload = json.loads(text)
    assert len(payload["events"]) == 1
    assert all(event["outcome"] == "argument_error" for event in payload["events"])
    assert str(root) not in text
    assert "--unexpected" not in text


def test_module_entry_records_clean_argument_error_as_one_global_event(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = tmp_path / "runtime"
    root.mkdir()
    monkeypatch.setenv("OA_FINANCE_PREAUDIT_FLOW_ROOT", str(root))
    stderr = io.StringIO()

    with redirect_stderr(stderr):
        exit_code = cli.entrypoint(["clean"])

    assert exit_code == 2
    text = (root / ".metrics" / "cli-invocations.json").read_text(
        encoding="utf-8"
    )
    payload = json.loads(text)
    assert [
        (event["workflow_key"], event["command"], event["outcome"])
        for event in payload["events"]
    ] == [("global", "clean", "argument_error")]
    assert "--all-cache" not in text


def test_complete_review_uses_the_same_deep_module(tmp_path) -> None:
    action_file = tmp_path / "action.json"
    decision_file = tmp_path / "decision.json"
    calls: list[tuple[object, object]] = []

    class Engine:
        def complete_review(self, action: object, decision: object) -> dict:
            calls.append((action, decision))
            return {
                "schema_version": 1,
                "kind": "preaudit_action",
                "action": "closed",
            }

    output = io.StringIO()
    with redirect_stdout(output):
        exit_code = cli.main(
            [
                "complete-review",
                "--action-file",
                str(action_file),
                "--decision-file",
                str(decision_file),
            ],
            flow_engine_factory=Engine,
        )

    assert exit_code == 0
    assert calls == [(action_file, decision_file)]
    assert json.loads(output.getvalue())["action"] == "closed"


def test_controlled_complete_review_requires_confirmation(tmp_path: Path) -> None:
    action_file = tmp_path / "action.json"
    decision_file = tmp_path / "decision.json"
    action_file.write_text(
        json.dumps(
            {"mode": "controlled_write", "workflow_key": "travel_expense"}
        ),
        encoding="utf-8",
    )
    calls: list[tuple[object, object]] = []

    class Engine:
        def complete_review(self, action: object, decision: object) -> dict:
            calls.append((action, decision))
            return {"schema_version": 1, "kind": "preaudit_action", "action": "closed"}

    denied = io.StringIO()
    with redirect_stdout(denied):
        denied_code = cli.main(
            [
                "complete-review",
                "--action-file",
                str(action_file),
                "--decision-file",
                str(decision_file),
            ],
            flow_engine_factory=Engine,
        )

    allowed = io.StringIO()
    with redirect_stdout(allowed):
        allowed_code = cli.main(
            [
                "complete-review",
                "--action-file",
                str(action_file),
                "--decision-file",
                str(decision_file),
                "--confirm-write",
                "YES",
            ],
            flow_engine_factory=Engine,
        )

    assert denied_code == 1
    assert json.loads(denied.getvalue())["failure"]["code"] == "run_mode_unauthorized"
    assert allowed_code == 1
    assert json.loads(allowed.getvalue())["failure"]["code"] == (
        "production_rolling_controller_required"
    )
    assert calls == []
