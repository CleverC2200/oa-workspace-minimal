from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def isolate_production_runtime_controls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "OA_FINANCE_PREAUDIT_STOP_FILE",
        str(tmp_path / "control" / "stop-preaudit.flag"),
    )
    monkeypatch.setenv(
        "OA_FROZEN_WRITER_LOCK_FILE",
        str(tmp_path / "control" / "frozen-writer.lock"),
    )
