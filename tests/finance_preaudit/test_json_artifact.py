from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest import mock

import pytest

from apps import json_artifact
from apps.oa_finance_preaudit import json_artifact as legacy_json_artifact


class TestJsonArtifact:
    def test_legacy_import_path_uses_shared_writer(self) -> None:
        assert legacy_json_artifact.write_json_atomic is json_artifact.write_json_atomic

    def test_atomic_write_publishes_normalized_json_and_leaves_no_temporary_file(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "artifact.json"

            written = json_artifact.write_json_atomic(
                target,
                {
                    "schema_version": 1,
                    "path": root / "bound-file.json",
                },
                indent=2,
            )

            assert written == target.resolve()
            assert json.loads(target.read_text(encoding="utf-8")) == {
                "schema_version": 1,
                "path": str((root / "bound-file.json")),
            }
            assert list(root.glob(".*.tmp")) == []

    def test_replace_failure_preserves_the_existing_artifact_and_cleans_up(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "artifact.json"
            target.write_text('{"version": 1}\n', encoding="utf-8")

            with (
                mock.patch.object(
                    json_artifact.os,
                    "replace",
                    side_effect=OSError("replace failed"),
                ),
                pytest.raises(OSError),
            ):
                json_artifact.write_json_atomic(target, {"version": 2})

            assert json.loads(target.read_text(encoding="utf-8")) == {"version": 1}
            assert list(root.glob(".*.tmp")) == []
