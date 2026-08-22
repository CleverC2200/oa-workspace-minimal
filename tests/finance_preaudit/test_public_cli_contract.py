from __future__ import annotations

import io
import json
from contextlib import redirect_stdout

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
        )

    assert exit_code == 0
    assert json.loads(output.getvalue())["action"] == "closed"
    assert output.getvalue().count("\n") == 1


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
