from __future__ import annotations

from contextlib import contextmanager
import fcntl
import hashlib
import os
from pathlib import Path
import stat
from typing import Iterator


STOP_CODE = "oa_run_stopped"
GLOBAL_WRITER_KEY = "finance-preaudit-global-writer"
PROJECT_ROOT = Path(__file__).resolve().parents[2]


def stop_file() -> Path:
    raw = str(os.environ.get("OA_FINANCE_PREAUDIT_STOP_FILE") or "").strip()
    return Path(raw).expanduser() if raw else Path("runtime") / "stop-preaudit.flag"


def stop_requested() -> bool:
    raw = str(
        os.environ.get("OA_FINANCE_PREAUDIT_STOP_REQUESTED") or ""
    ).strip().lower()
    return raw in {"1", "true", "yes", "y", "on"} or stop_file().exists()


def request_stop() -> None:
    target = stop_file().expanduser()
    if not target.is_absolute():
        target = PROJECT_ROOT / target
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(target, flags, 0o600)
    try:
        os.write(descriptor, b"stop_requested\n")
    finally:
        os.close(descriptor)


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
    base = Path(os.path.abspath(Path(base_path or writer_lock_file()).expanduser()))
    digest = hashlib.sha256(str(request_id).encode("utf-8")).hexdigest()[:16]
    return base.with_name(f"{base.stem}-{digest}{base.suffix or '.lock'}")


def _open_directory_nofollow(path: Path) -> int:
    target = Path(path)
    if not target.is_absolute() or not target.parts:
        raise RuntimeError("writer_lock_unsafe")
    flags = os.O_RDONLY | os.O_CLOEXEC
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor: int | None = None
    try:
        descriptor = os.open(target.parts[0], flags)
        for component in target.parts[1:]:
            if component in {"", ".", ".."}:
                raise RuntimeError("writer_lock_unsafe")
            try:
                child = os.open(component, flags, dir_fd=descriptor)
            except FileNotFoundError:
                try:
                    os.mkdir(component, 0o700, dir_fd=descriptor)
                except FileExistsError:
                    pass
                child = os.open(component, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        return descriptor
    except (OSError, RuntimeError) as error:
        if descriptor is not None:
            os.close(descriptor)
        raise RuntimeError("writer_lock_unsafe") from error


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
    parent_descriptor = _open_directory_nofollow(target.parent)
    flags = os.O_RDWR | os.O_CREAT | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    file_descriptor: int | None = None
    try:
        parent_stat = os.fstat(parent_descriptor)
        for _attempt in range(8):
            try:
                file_descriptor = os.open(
                    target.name,
                    flags,
                    0o600,
                    dir_fd=parent_descriptor,
                )
                break
            except FileNotFoundError:
                continue
        if file_descriptor is None:
            raise RuntimeError("writer_lock_unsafe")
        current_parent = os.stat(target.parent, follow_symlinks=False)
        if (
            not stat.S_ISDIR(current_parent.st_mode)
            or (current_parent.st_dev, current_parent.st_ino)
            != (parent_stat.st_dev, parent_stat.st_ino)
        ):
            raise RuntimeError("writer_lock_unsafe")
    except (OSError, RuntimeError) as error:
        if file_descriptor is not None:
            os.close(file_descriptor)
            file_descriptor = None
        raise RuntimeError("writer_lock_unsafe") from error
    finally:
        os.close(parent_descriptor)
    if file_descriptor is None:
        raise RuntimeError("writer_lock_unsafe")
    try:
        descriptor = os.fdopen(file_descriptor, "r+", encoding="utf-8")
    except Exception:
        os.close(file_descriptor)
        raise
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


@contextmanager
def global_writer_lease() -> Iterator[None]:
    """Acquire the one canonical lock shared by every OA write entrypoint."""
    with request_writer_lease(GLOBAL_WRITER_KEY, writer_lock_file()):
        yield
