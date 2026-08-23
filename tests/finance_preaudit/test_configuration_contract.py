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


def _auth_environment(**updates: str) -> dict[str, str]:
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("OA_")
    }
    environment.update(updates)
    return environment


def _run_auth(code: str, environment: dict[str, str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-B", "-c", code],
        cwd=PROJECT_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )


def _write_auth(path: Path, content: str, *, mode: int = 0o600) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(mode)


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

    def test_worktree_external_auth_file_precedes_local_fallback(
        self,
        tmp_path: Path,
    ) -> None:
        stable_auth = tmp_path / "oa-auth.env"
        _write_auth(stable_auth, "OA_BASE_URL=https://stable.invalid\n")
        result = _run_auth(
            "from apps.oa_approval import auth; "
            "print(auth.env('OA_BASE_URL') == 'https://stable.invalid')",
            _auth_environment(OA_AUTH_ENV_FILE=str(stable_auth)),
        )

        assert result.returncode == 0
        assert result.stdout.strip() == "True"

    def test_auth_file_with_group_or_world_access_is_rejected(
        self,
        tmp_path: Path,
    ) -> None:
        stable_auth = tmp_path / "oa-auth.env"
        _write_auth(
            stable_auth,
            "OA_BASE_URL=https://stable.invalid\n",
            mode=0o644,
        )
        result = _run_auth(
            "from apps.oa_approval import auth; "
            "\ntry: auth.env('OA_BASE_URL'); print('not-rejected')"
            "\nexcept RuntimeError as error: print(str(error))",
            _auth_environment(OA_AUTH_ENV_FILE=str(stable_auth)),
        )

        assert result.returncode == 0
        assert result.stdout.strip() == "oa_auth_file_permissions_invalid"

    def test_symlink_auth_file_is_rejected(self, tmp_path: Path) -> None:
        target = tmp_path / "target.env"
        _write_auth(target, "OA_BASE_URL=https://stable.invalid\n")
        stable_auth = tmp_path / "oa-auth.env"
        stable_auth.symlink_to(target)
        result = _run_auth(
            "from apps.oa_approval import auth; "
            "\ntry: auth.env('OA_BASE_URL'); print('not-rejected')"
            "\nexcept RuntimeError as error: print(str(error))",
            _auth_environment(OA_AUTH_ENV_FILE=str(stable_auth)),
        )

        assert result.returncode == 0
        assert result.stdout.strip() == "oa_auth_file_permissions_invalid"

    def test_configuration_status_reports_source_without_values(
        self,
        tmp_path: Path,
    ) -> None:
        stable_auth = tmp_path / "oa-auth.env"
        _write_auth(
            stable_auth,
            "OA_BASE_URL=https://stable.invalid\n"
            "OA_APP_ID=recorded-app\n"
            "OA_APP_SECRET=recorded-secret\n"
            "OA_UID=recorded-user\n",
        )
        result = _run_auth(
            "import json; from apps.oa_approval import auth; "
            "print(json.dumps(auth.configuration_status(), sort_keys=True))",
            _auth_environment(OA_AUTH_ENV_FILE=str(stable_auth)),
        )

        assert result.returncode == 0
        assert result.stdout.strip() == (
            '{"configured": 4, "required": 4, '
            '"source": "stable", "status": "ok"}'
        )
        assert "recorded" not in result.stdout

    def test_empty_stable_value_does_not_block_worktree_fallback(
        self,
        tmp_path: Path,
    ) -> None:
        stable_auth = tmp_path / "stable.env"
        _write_auth(stable_auth, "OA_UID=\n")
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        fallback = worktree / ".env.oa-auth"
        _write_auth(fallback, "OA_UID=fallback-user\n")
        result = _run_auth(
            "import os; from pathlib import Path; "
            "from apps.oa_approval import auth; "
            "auth.PROJECT_ROOT=Path(os.environ['TEST_PROJECT_ROOT']); "
            "print(auth.env('OA_UID') == 'fallback-user')",
            _auth_environment(
                OA_AUTH_ENV_FILE=str(stable_auth),
                TEST_PROJECT_ROOT=str(worktree),
            ),
        )

        assert result.returncode == 0
        assert result.stdout.strip() == "True"

    @pytest.mark.parametrize(
        "configured_path",
        (
            "relative-auth.env",
            str(PROJECT_ROOT / "runtime" / "oa-auth.env"),
        ),
        ids=("relative", "inside-worktree"),
    )
    def test_stable_auth_path_must_be_absolute_and_outside_worktree(
        self,
        configured_path: str,
    ) -> None:
        result = _run_auth(
            "from apps.oa_approval import auth; "
            "\ntry: auth.stable_env_file(); print('not-rejected')"
            "\nexcept RuntimeError as error: print(str(error))",
            _auth_environment(OA_AUTH_ENV_FILE=configured_path),
        )

        assert result.returncode == 0
        assert result.stdout.strip() == (
            "oa_auth_file_outside_worktree_required"
        )

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

    def test_unconfigured_official_sql_does_not_require_oracle_driver(
        self,
    ) -> None:
        with (
            patch.dict(os.environ, {}, clear=True),
            patch.object(auth, "load_local_env"),
            patch.dict(sys.modules, {"oracledb": None}),
        ):
            assert official_sql._connect() is None

    def test_declared_environment_can_install_the_official_sql_adapter(
        self,
    ) -> None:
        dependencies = {
            line.strip()
            for line in (PROJECT_ROOT / "requirements-dev.txt")
            .read_text(encoding="utf-8")
            .splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        }

        assert "oracledb==4.0.1" in dependencies

    def test_env_template_contains_only_implemented_settings(self) -> None:
        template = (PROJECT_ROOT / ".env.example").read_text(encoding="utf-8")
        names = set(re.findall(r"^([A-Z][A-Z0-9_]*)=", template, re.MULTILINE))
        implementation = "\n".join(
            path.read_text(encoding="utf-8")
            for path in (PROJECT_ROOT / "apps").rglob("*")
            if path.suffix in {".py", ".js"}
        )

        assert sorted(name for name in names if name not in implementation) == []
