from __future__ import annotations

import os
import sys
import types
import unittest
from unittest.mock import patch

from apps.oa_approval import auth, official_sql


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


if __name__ == "__main__":
    unittest.main()
