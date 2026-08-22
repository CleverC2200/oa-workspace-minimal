from __future__ import annotations

import io
import json
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import pytest

from apps.oa_finance_preaudit import cli
from apps.oa_approval import frozen_writer


PUBLIC_COMMANDS = {"next-action", "complete-review", "status", "prune"}


def test_public_cli_exposes_exactly_four_commands() -> None:
    parser = cli.build_parser()
    subparsers = next(
        action for action in parser._actions if action.dest == "command"
    )
    assert set(subparsers.choices) == PUBLIC_COMMANDS


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
