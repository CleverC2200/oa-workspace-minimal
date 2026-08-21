from __future__ import annotations

from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
from typing import Iterator, TextIO


STOP_CODE = "oa_run_stopped"
PROJECT_ROOT = Path(__file__).resolve().parents[2]


def stop_file() -> Path:
    raw = str(os.environ.get("OA_FINANCE_PREAUDIT_STOP_FILE") or "").strip()
    return Path(raw).expanduser() if raw else Path("runtime") / "stop-preaudit.flag"


def stop_requested() -> bool:
    raw = str(
        os.environ.get("OA_FINANCE_PREAUDIT_STOP_REQUESTED") or ""
    ).strip().lower()
    return raw in {"1", "true", "yes", "y", "on"} or stop_file().exists()


def stop_error(
    message: str = "已请求停止当前财务预审材料收集",
) -> dict[str, str]:
    return {"code": STOP_CODE, "message": message}


def writer_lock_file() -> Path:
    raw = str(os.environ.get("OA_FROZEN_WRITER_LOCK_FILE") or "").strip()
    return (
        Path(raw).expanduser()
        if raw
        else PROJECT_ROOT / "runtime" / "control" / "frozen-writer.lock"
    )


def _try_writer_lock(path: Path) -> TextIO:
    target = Path(path).expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor = target.open("a+", encoding="utf-8")
    try:
        fcntl.flock(descriptor.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        descriptor.close()
        raise RuntimeError("writer_already_active")
    return descriptor


def writer_active(path: Path | None = None) -> bool:
    target = Path(path or writer_lock_file()).expanduser().resolve()
    if not target.exists():
        return False
    try:
        descriptor = target.open("r+", encoding="utf-8")
    except OSError:
        return True
    try:
        try:
            fcntl.flock(descriptor.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(descriptor.fileno(), fcntl.LOCK_UN)
        return False
    finally:
        descriptor.close()


@contextmanager
def writer_lease(path: Path | None = None) -> Iterator[None]:
    descriptor = _try_writer_lock(path or writer_lock_file())
    try:
        descriptor.seek(0)
        descriptor.truncate()
        descriptor.write(str(os.getpid()))
        descriptor.flush()
        yield
    finally:
        fcntl.flock(descriptor.fileno(), fcntl.LOCK_UN)
        descriptor.close()
