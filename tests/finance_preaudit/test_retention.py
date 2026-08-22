from __future__ import annotations

import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from apps.oa_finance_preaudit import retention

NOW = datetime(2026, 8, 22, 12, 0, tzinfo=timezone.utc)


def record(
    key: str,
    *,
    age_days: int,
    kind: str = "artifact",
    workflow_key: str = "travel_expense",
    closed: bool = True,
    write_state: str = "confirmed",
    relative_path: str | None = None,
) -> retention.RetentionRecord:
    return retention.RetentionRecord(
        key=key,
        workflow_key=workflow_key,
        kind=kind,
        created_at=NOW - timedelta(days=age_days),
        closed=closed,
        write_state=write_state,
        relative_path=relative_path,
    )


class TestRetention:
    def test_29_30_31_day_boundary_deletes_only_older_than_30_days(self) -> None:
        actions = retention.plan_retention(
            [
                record("d29", age_days=29, relative_path="logs/d29.json"),
                record("d30", age_days=30, relative_path="logs/d30.json"),
                record("d31", age_days=31, relative_path="logs/d31.json"),
            ],
            now=NOW,
        )

        assert [(action.key, action.action) for action in actions] == [
            ("d29", "keep"),
            ("d30", "keep"),
            ("d31", "delete_path"),
        ]

    def test_latest_task_unclosed_and_unknown_write_are_always_retained(self) -> None:
        actions = retention.plan_retention(
            [
                record("old", age_days=40, kind="task"),
                record("latest", age_days=35, kind="task"),
                record("open", age_days=90, kind="task", closed=False),
                record(
                    "unknown", age_days=90, write_state="unknown", relative_path="x"
                ),
            ],
            now=NOW,
        )
        by_key = {action.key: action for action in actions}

        assert by_key["old"].action == "delete_task"
        assert by_key["latest"].reason == "latest_task"
        assert by_key["open"].reason == "not_closed"
        assert by_key["unknown"].reason == "write_state_unknown"

    def test_closed_previous_task_is_archived_before_expiry(self) -> None:
        actions = retention.plan_retention(
            [
                record("previous", age_days=3, kind="task"),
                record("latest", age_days=1, kind="task"),
            ],
            now=NOW,
        )
        assert actions[0].action == "archive_task"
        assert actions[1].reason == "latest_task"

    def test_file_cleanup_is_idempotent_and_cannot_escape_marked_root(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "runtime"
            root.mkdir()
            retention.initialize_root(root)
            target = root / "logs" / "old.json"
            target.parent.mkdir()
            target.write_text("{}", encoding="utf-8")
            action = retention.RetentionAction(
                key="old",
                action="delete_path",
                reason="expired",
                relative_path="logs/old.json",
            )

            first = retention.apply_file_actions(root, [action], now=NOW)
            second = retention.apply_file_actions(root, [action], now=NOW)

            assert first["deleted"] == 1
            assert second["missing"] == 1
            assert str(root) not in str(first)
            outside = retention.RetentionAction(
                key="bad",
                action="delete_path",
                reason="expired",
                relative_path="../outside.json",
            )
            with pytest.raises(ValueError, match="retention_path_outside_root"):
                retention.apply_file_actions(root, [outside], now=NOW)

    def test_task_callbacks_receive_only_planned_archive_and_delete(self) -> None:
        calls: list[tuple[str, str]] = []
        actions = [
            retention.RetentionAction("a", "archive_task", "previous_task"),
            retention.RetentionAction("d", "delete_task", "expired"),
            retention.RetentionAction("k", "keep", "latest_task"),
        ]

        receipt = retention.apply_task_actions(
            actions,
            archive_task=lambda key: calls.append(("archive", key)),
            delete_task=lambda key: calls.append(("delete", key)),
            now=NOW,
        )

        assert calls == [("archive", "a"), ("delete", "d")]
        assert receipt["archived"] == 1
        assert receipt["deleted"] == 1
        assert "targets" not in receipt
        assert "task_ids" not in receipt
