from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from apps.oa_finance_preaudit import json_artifact


class JsonArtifactTest(unittest.TestCase):
    def test_atomic_write_publishes_normalized_json_and_leaves_no_temporary_file(self) -> None:
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

            self.assertEqual(written, target.resolve())
            self.assertEqual(
                json.loads(target.read_text(encoding="utf-8")),
                {
                    "schema_version": 1,
                    "path": str((root / "bound-file.json")),
                },
            )
            self.assertEqual(list(root.glob(".*.tmp")), [])

    def test_replace_failure_preserves_the_existing_artifact_and_cleans_up(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "artifact.json"
            target.write_text('{"version": 1}\n', encoding="utf-8")

            with mock.patch.object(
                json_artifact.os,
                "replace",
                side_effect=OSError("replace failed"),
            ), self.assertRaises(OSError):
                json_artifact.write_json_atomic(target, {"version": 2})

            self.assertEqual(
                json.loads(target.read_text(encoding="utf-8")),
                {"version": 1},
            )
            self.assertEqual(list(root.glob(".*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
