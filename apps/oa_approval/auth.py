from __future__ import annotations

import base64
import errno
import json
import os
import stat
import subprocess
import tempfile
import urllib.request
from pathlib import Path
from typing import Dict

from apps.oa_approval import safe_http

PROJECT_ROOT = Path(__file__).resolve().parents[2]
TIMEOUT = 30
REQUIRED_AUTH_ENV = ("OA_BASE_URL", "OA_APP_ID", "OA_APP_SECRET", "OA_UID")


def stable_env_file() -> Path:
    configured = str(os.environ.get("OA_AUTH_ENV_FILE") or "").strip()
    if configured:
        candidate = Path(configured).expanduser()
    else:
        config_home = str(os.environ.get("XDG_CONFIG_HOME") or "").strip()
        root = (
            Path(config_home).expanduser()
            if config_home
            else Path.home() / ".config"
        )
        candidate = root / "oa-workspace-minimal" / "oa-auth.env"
    if not candidate.is_absolute():
        raise RuntimeError("oa_auth_file_outside_worktree_required")
    resolved = candidate.resolve(strict=False)
    project_root = PROJECT_ROOT.resolve(strict=False)
    if resolved == project_root or project_root in resolved.parents:
        raise RuntimeError("oa_auth_file_outside_worktree_required")
    return candidate


def env_files() -> tuple[tuple[str, Path], ...]:
    return (
        ("stable", stable_env_file()),
        ("worktree", PROJECT_ROOT / ".env"),
        ("worktree", PROJECT_ROOT / ".env.oa-auth"),
    )


def _read_private_env(path: Path) -> list[str] | None:
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    if not nofollow and path.is_symlink():
        raise RuntimeError("oa_auth_file_permissions_invalid")
    try:
        descriptor = os.open(path, os.O_RDONLY | nofollow)
    except FileNotFoundError:
        return None
    except OSError as error:
        if error.errno == errno.ELOOP:
            raise RuntimeError("oa_auth_file_permissions_invalid") from None
        raise
    with os.fdopen(descriptor, encoding="utf-8") as stream:
        metadata = os.fstat(stream.fileno())
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o077:
            raise RuntimeError("oa_auth_file_permissions_invalid")
        return stream.read().splitlines()


def load_local_env() -> None:
    if getattr(load_local_env, "_loaded", False):
        return
    sources = {
        name: "process" for name in REQUIRED_AUTH_ENV if str(os.environ.get(name) or "").strip()
    }
    for source, path in env_files():
        lines = _read_private_env(path)
        if lines is None:
            continue
        for line in lines:
            text = line.strip()
            if not text or text.startswith("#") or "=" not in text:
                continue
            key, value = text.split("=", 1)
            key = key.strip()
            value = value.strip().strip("\"'")
            if key and value and key not in os.environ:
                os.environ[key] = value
                if key in REQUIRED_AUTH_ENV and value:
                    sources[key] = source
    setattr(load_local_env, "_sources", sources)
    setattr(load_local_env, "_loaded", True)


def configuration_status() -> dict[str, object]:
    load_local_env()
    configured = [name for name in REQUIRED_AUTH_ENV if str(os.environ.get(name) or "").strip()]
    sources = getattr(load_local_env, "_sources", {})
    active_sources = {sources.get(name, "process") for name in configured}
    source = next(iter(active_sources)) if len(active_sources) == 1 else "mixed" if active_sources else "none"
    return {
        "status": "ok" if len(configured) == len(REQUIRED_AUTH_ENV) else "missing",
        "configured": len(configured),
        "required": len(REQUIRED_AUTH_ENV),
        "source": source,
    }


def env(name: str, default: str = "") -> str:
    load_local_env()
    return str(os.environ.get(name) or default).strip()


def host() -> str:
    value = env("OA_BASE_URL").rstrip("/")
    if not value:
        raise RuntimeError("缺少 OA_BASE_URL")
    return value


def auth_pair() -> tuple[str, str]:
    app_id = env("OA_APP_ID")
    secret = env("OA_APP_SECRET")
    if not app_id or not secret:
        raise RuntimeError("缺少 OA_APP_ID/OA_APP_SECRET")
    return app_id, secret


def _http_post(url: str, headers: Dict[str, str], body: str = "") -> dict:
    req = urllib.request.Request(url=url, data=body.encode("utf-8"), headers=headers, method="POST")
    with safe_http.urlopen(req, timeout=TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8", errors="ignore"))


def _openssl_encrypt(pub_pem_path: str, plaintext: str) -> str:
    proc = subprocess.run(
        [
            "openssl",
            "pkeyutl",
            "-encrypt",
            "-pubin",
            "-inkey",
            pub_pem_path,
            "-pkeyopt",
            "rsa_padding_mode:pkcs1",
        ],
        input=plaintext.encode("utf-8"),
        capture_output=True,
        check=True,
    )
    return base64.b64encode(proc.stdout).decode("utf-8")


def _client_public_key(tmp_dir: str) -> str:
    private_key = os.path.join(tmp_dir, "client.pem")
    public_key = os.path.join(tmp_dir, "client_pub.pem")
    subprocess.run(["openssl", "genrsa", "-out", private_key, "2048"], check=True, capture_output=True)
    subprocess.run(["openssl", "rsa", "-in", private_key, "-pubout", "-out", public_key], check=True, capture_output=True)
    return "".join(line.strip() for line in Path(public_key).read_text(encoding="utf-8").splitlines() if line.strip())


def _pem_file_from_b64(spk_b64: str, tmp_dir: str) -> str:
    path = os.path.join(tmp_dir, "server.pem")
    chunks = [spk_b64[i : i + 64] for i in range(0, len(spk_b64), 64)]
    Path(path).write_text(
        "-----BEGIN PUBLIC KEY-----\n" + "\n".join(chunks) + "\n-----END PUBLIC KEY-----\n",
        encoding="utf-8",
    )
    return path


def server_public_key() -> str:
    app_id, _secret = auth_pair()
    with tempfile.TemporaryDirectory(prefix="oa_auth_") as tmp:
        resp = _http_post(f"{host()}/api/ec/dev/auth/regist", {"appid": app_id, "cpk": _client_public_key(tmp)})
    spk = resp.get("spk") or (resp.get("data") or {}).get("spk")
    if not spk:
        raise RuntimeError(f"regist 未返回 spk: {resp}")
    return str(spk)


def get_token() -> str:
    app_id, secret = auth_pair()
    with tempfile.TemporaryDirectory(prefix="oa_token_") as tmp:
        encrypted_secret = _openssl_encrypt(_pem_file_from_b64(server_public_key(), tmp), secret)
    resp = _http_post(f"{host()}/api/ec/dev/auth/applytoken", {"appid": app_id, "secret": encrypted_secret})
    token = resp.get("token") or (resp.get("data") or {}).get("token")
    if not token:
        raise RuntimeError(f"applytoken 未返回 token: {resp}")
    return str(token)


def encrypt_user_id(oa_uid: str) -> str:
    if not str(oa_uid or "").strip():
        raise RuntimeError("缺少 OA_UID 或 --oa-uid")
    with tempfile.TemporaryDirectory(prefix="oa_uid_") as tmp:
        return _openssl_encrypt(_pem_file_from_b64(server_public_key(), tmp), str(oa_uid).strip())
