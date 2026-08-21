from __future__ import annotations

import base64
import json
import os
import subprocess
import tempfile
import urllib.request
from pathlib import Path
from typing import Dict

PROJECT_ROOT = Path(__file__).resolve().parents[2]
ENV_FILES = (PROJECT_ROOT / ".env", PROJECT_ROOT / ".env.oa-auth")
TIMEOUT = 30


def load_local_env() -> None:
    if getattr(load_local_env, "_loaded", False):
        return
    for path in ENV_FILES:
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            continue
        for line in lines:
            text = line.strip()
            if not text or text.startswith("#") or "=" not in text:
                continue
            key, value = text.split("=", 1)
            key = key.strip()
            value = value.strip().strip("\"'")
            if key and key not in os.environ:
                os.environ[key] = value
    setattr(load_local_env, "_loaded", True)


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
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
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
