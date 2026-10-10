"""Own the retained evidence set for new private review archives.

Native runtime databases and dependency caches are working files. The retained
rollout, prompt, generated schema, raw decision and process events are evidence.
Old recursive output bindings remain owned by their original archive contract.
"""

from pathlib import Path
import re

SCOPE = "review_evidence_v1"
_OUTPUT_NAMES = ("prompt.txt", "decision-schema.json", "decision.json",
                 "events.jsonl", "stderr.log")
_RECEIPT = re.compile(r"tool-receipts/[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\.jsonl")


def evidence_paths(audit: Path, receipts: list[str]) -> tuple[Path, ...]:
    """Select invocation evidence; traverse only a legacy private session tree."""
    root = Path(audit).absolute()
    if (root != root.resolve() or not root.is_dir() or not isinstance(receipts, list)
            or not all(isinstance(name, str) and _RECEIPT.fullmatch(name) for name in receipts)
            or len(receipts) != len(set(receipts))):
        raise ValueError("review_output_scope_invalid")
    paths = []
    for name in (*_OUTPUT_NAMES, *receipts):
        path = root / name
        if path.is_symlink() or path != path.resolve():
            raise ValueError("review_output_scope_invalid")
        if not path.exists() and name in _OUTPUT_NAMES:
            # Interrupted reviews may lack a decision; the report retains failure.
            continue
        if not path.is_file():
            raise ValueError("review_output_scope_invalid")
        paths.append(path)
    # Supported old Workers retained the invocation rollout in a private home.
    # Keep these original receipts, never that home's plugins, skills or DBs.
    home = root / "codex-home"
    if home.is_symlink():
        raise ValueError("review_output_scope_invalid")
    legacy = home / "session.jsonl"
    sessions = home / "sessions"
    if legacy.exists() or legacy.is_symlink():
        paths.append(legacy)
    if sessions.exists() or sessions.is_symlink():
        if sessions != sessions.resolve() or not sessions.is_dir():
            raise ValueError("review_output_scope_invalid")
        paths.extend(sessions.rglob("*.jsonl"))
    if any(path != path.resolve() or not path.is_file() for path in paths):
        raise ValueError("review_output_scope_invalid")
    return tuple(sorted(paths))
