from __future__ import annotations

import os
from pathlib import Path
import re
import sys
import types
import unittest
from unittest.mock import patch

from apps.oa_approval import auth, official_sql


PROJECT_ROOT = Path(__file__).resolve().parents[2]


class ConfigurationContractTest(unittest.TestCase):
    def test_oa_host_requires_local_configuration(self) -> None:
        with (
            patch.dict(os.environ, {}, clear=True),
            patch.object(auth, "load_local_env"),
        ):
            with self.assertRaisesRegex(RuntimeError, "OA_BASE_URL"):
                auth.host()

    def test_official_sql_has_no_implicit_database_identity(self) -> None:
        fake_oracledb = types.SimpleNamespace(
            init_oracle_client=lambda **_kwargs: None,
            makedsn=lambda *_args, **_kwargs: self.fail("unexpected DSN creation"),
            connect=lambda **_kwargs: self.fail("unexpected database connection"),
        )
        with (
            patch.dict(os.environ, {}, clear=True),
            patch.object(auth, "load_local_env"),
            patch.dict(sys.modules, {"oracledb": fake_oracledb}),
        ):
            self.assertIsNone(official_sql._connect())

    def test_env_template_contains_only_implemented_settings(self) -> None:
        template = (PROJECT_ROOT / ".env.example").read_text(encoding="utf-8")
        names = set(re.findall(r"^([A-Z][A-Z0-9_]*)=", template, re.MULTILINE))
        implementation = "\n".join(
            path.read_text(encoding="utf-8")
            for path in (PROJECT_ROOT / "apps").rglob("*")
            if path.suffix in {".py", ".js"}
        )

        self.assertEqual(
            sorted(name for name in names if name not in implementation),
            [],
        )


if __name__ == "__main__":
    unittest.main()
