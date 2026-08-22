from __future__ import annotations

import os
import re
import subprocess
import sys
import types
from pathlib import Path
from unittest.mock import patch

import pytest

from apps.oa_approval import api, auth, flow, official_sql

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class TestConfigurationContract:
    def test_finance_package_import_does_not_load_case_collection(self) -> None:
        result = subprocess.run(
            [
                sys.executable,
                "-B",
                "-c",
                (
                    "import sys; import apps.oa_finance_preaudit; "
                    "assert 'apps.oa_finance_preaudit.case_bundle' not in sys.modules"
                ),
            ],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )

        assert result.returncode == 0, result.stderr

    def test_oa_approval_does_not_depend_on_finance_orchestration(self) -> None:
        offenders = [
            str(path.relative_to(PROJECT_ROOT))
            for path in (PROJECT_ROOT / "apps" / "oa_approval").glob("*.py")
            if "apps.oa_finance_preaudit" in path.read_text(encoding="utf-8")
        ]

        assert offenders == []

    def test_ci_discovers_the_complete_test_tree(self) -> None:
        workflow = (PROJECT_ROOT / ".github/workflows/quality.yml").read_text(
            encoding="utf-8"
        )

        assert "python -m pytest -v" in workflow

    def test_oa_host_requires_local_configuration(self) -> None:
        with (
            patch.dict(os.environ, {}, clear=True),
            patch.object(auth, "load_local_env"),
        ):
            with pytest.raises(RuntimeError, match="OA_BASE_URL"):
                auth.host()

    def test_oa_user_identity_is_never_stored_in_flow_configs(
        self,
        tmp_path: Path,
    ) -> None:
        configs = flow.load_all_configs()

        assert all("oa_uid" not in config for config in configs)
        assert "OA_UID=" in (PROJECT_ROOT / ".env.example").read_text(
            encoding="utf-8"
        )
        for forbidden_field in ("oa_uid", "oa_uid_env"):
            invalid = tmp_path / f"{forbidden_field}.json"
            invalid.write_text(
                '{"flow_key":"test","workflow_id":"1",'
                f'"workflow_name":"test","{forbidden_field}":"forbidden"}}',
                encoding="utf-8",
            )
            with pytest.raises(SystemExit, match="OA_UID"):
                flow.load_config(str(invalid))

    def test_oa_api_uses_only_override_or_fixed_local_uid(self) -> None:
        def fake_env(name: str) -> str:
            return {"OA_UID": "local-user", "OTHER_UID": "wrong-user"}[name]

        with (
            patch.object(api.auth, "get_token", return_value="token"),
            patch.object(api.auth, "env", side_effect=fake_env),
            patch.object(
                api.auth,
                "encrypt_user_id",
                side_effect=lambda value: f"encrypted:{value}",
            ),
            patch.object(
                api,
                "OaApi",
                side_effect=lambda token, uid: (token, uid),
            ),
        ):
            assert api.build_api({"oa_uid_env": "OTHER_UID"}) == (
                "token",
                "encrypted:local-user",
            )
            assert api.build_api({}, oa_uid_override="override-user") == (
                "token",
                "encrypted:override-user",
            )

    def test_official_sql_has_no_implicit_database_identity(self) -> None:
        fake_oracledb = types.SimpleNamespace(
            init_oracle_client=lambda **_kwargs: None,
            makedsn=lambda *_args, **_kwargs: pytest.fail("unexpected DSN creation"),
            connect=lambda **_kwargs: pytest.fail("unexpected database connection"),
        )
        with (
            patch.dict(os.environ, {}, clear=True),
            patch.object(auth, "load_local_env"),
            patch.dict(sys.modules, {"oracledb": fake_oracledb}),
        ):
            assert official_sql._connect() is None

    def test_env_template_contains_only_implemented_settings(self) -> None:
        template = (PROJECT_ROOT / ".env.example").read_text(encoding="utf-8")
        names = set(re.findall(r"^([A-Z][A-Z0-9_]*)=", template, re.MULTILINE))
        implementation = "\n".join(
            path.read_text(encoding="utf-8")
            for path in (PROJECT_ROOT / "apps").rglob("*")
            if path.suffix in {".py", ".js"}
        )

        assert sorted(name for name in names if name not in implementation) == []
