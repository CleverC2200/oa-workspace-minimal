"""Immutable provenance for failed final outputs; contains no business observations."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from apps.attempt_action import read_input, input_sha256 as input_sha256
from apps.oa_approval import runtime_control

INPUT_NAMES = ("action.json", "state.json", "review-packet.json", "material.json")


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
