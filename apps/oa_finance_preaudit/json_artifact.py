from __future__ import annotations

import json
import os
from pathlib import Path
import secrets


def _json_default(value: object) -> str:
    if isinstance(value, Path):
        return str(value)
    raise TypeError(
        f"Object of type {value.__class__.__name__} is not JSON serializable"
    )


def write_json_atomic(
    path: Path,
    payload: dict,
    *,
    indent: int | None = None,
) -> Path:
    target = Path(path).expanduser().resolve()
    temporary = target.with_name(
        f".{target.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp"
    )
    serialized = (
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            indent=indent,
            default=_json_default,
        )
        + "\n"
    )
    expected = json.loads(serialized)
    try:
        with temporary.open("w", encoding="utf-8") as stream:
            stream.write(serialized)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            written = json.loads(temporary.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise OSError("json_artifact_readback_failed") from error
        if written != expected:
            raise OSError("json_artifact_readback_mismatch")
        os.replace(temporary, target)
        directory = os.open(str(target.parent), os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
    return target
