"""Immutable provenance for failed final outputs; contains no business observations."""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import stat

from apps.oa_approval import runtime_control

INPUT_NAMES = ("action.json", "state.json", "review-packet.json", "material.json")


@contextmanager
def _input_stream(path: Path, *, private: bool = False):
    path = Path(path).expanduser().absolute()
    if any(item.is_symlink() for item in (path, *path.parents)):
        raise ValueError("diagnostic_input_unsafe")
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as stream:
            metadata = os.fstat(stream.fileno())
            if not stat.S_ISREG(metadata.st_mode):
                raise ValueError("diagnostic_input_unsafe")
            if (metadata.st_mode & 0o022) or (private and stat.S_IMODE(metadata.st_mode) != 0o600):
                raise ValueError("diagnostic_input_permissions_invalid")
            yield stream
    except FileNotFoundError as error:
        raise ValueError("diagnostic_input_missing") from error
    except OSError as error:
        raise ValueError("diagnostic_input_unreadable") from error


def read_input(path: Path, *, private: bool = False) -> bytes:
    with _input_stream(path, private=private) as stream:
        if os.fstat(stream.fileno()).st_size > 16 * 1024 * 1024:
            raise ValueError("diagnostic_input_too_large")
        return stream.read()


def input_sha256(path: Path) -> str:
    with _input_stream(path) as stream:
        digest = hashlib.sha256()
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
        return digest.hexdigest()


def capture(action_file: Path, schema: dict) -> dict | None:
    try:
        fingerprints = {name: hashlib.sha256(read_input(action_file.with_name(name))).hexdigest()
                        for name in INPUT_NAMES}
    except ValueError:
        # Legacy/missing input context does not change existing production parsing.
        return None
    return {"schema_version": 1, "kind": "preaudit_output_context",
            "input_fingerprints": fingerprints, "schema": json.loads(json.dumps(schema))}


def persist(raw_file: Path, context: dict) -> str:
    digest = hashlib.sha256(read_input(raw_file, private=True)).hexdigest()
    payload = {**context, "raw_output_sha256": digest}
    body = (json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")) + "\n").encode()
    path = raw_file.with_name(f"review-context-{digest}.json")
    if os.path.lexists(path):
        if read_input(path, private=True) != body:
            raise ValueError("decision_diagnostic_invalid")
    else:
        runtime_control.write_control_bytes_atomic(path, body)
    return hashlib.sha256(body).hexdigest()
