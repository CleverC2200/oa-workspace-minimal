from __future__ import annotations

from contextlib import contextmanager
import fcntl
import hashlib
import os
from pathlib import Path
from typing import Iterator


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


def request_writer_lock_file(request_id: str, base_path: Path | None = None) -> Path:
    base = Path(base_path or writer_lock_file()).expanduser().resolve()
    digest = hashlib.sha256(str(request_id).encode("utf-8")).hexdigest()[:16]
    return base.with_name(f"{base.stem}-{digest}{base.suffix or '.lock'}")


def active_request_writers(base_path: Path | None = None) -> int:
    base = Path(base_path or writer_lock_file()).expanduser().resolve()
    pattern = f"{base.stem}-*{base.suffix or '.lock'}"
    return sum(1 for path in base.parent.glob(pattern) if writer_active(path))


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
def request_writer_lease(
    request_id: str,
    path: Path | None = None,
) -> Iterator[None]:
    target = request_writer_lock_file(request_id, path)
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor = target.open("a+", encoding="utf-8")
    try:
        fcntl.flock(descriptor.fileno(), fcntl.LOCK_EX)
        descriptor.seek(0)
        descriptor.truncate()
        descriptor.write(str(os.getpid()))
        descriptor.flush()
        yield
    finally:
        fcntl.flock(descriptor.fileno(), fcntl.LOCK_UN)
        descriptor.close()
