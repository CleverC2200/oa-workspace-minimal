from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
import fcntl
import hashlib
import os
from pathlib import Path
import re
import secrets
import stat
from typing import Iterator


STOP_CODE = "oa_run_stopped"
GLOBAL_WRITER_KEY = "finance-preaudit-global-writer"
GLOBAL_CONTROLLER_KEY = "finance-preaudit-rolling-controller"
PROJECT_ROOT = Path(__file__).resolve().parents[2]
_STOP_REASON = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_ALLOWED_STOP_REASONS = frozenset(
    {
        "controller_cycle_stop",
        "production_probe_prepare_failed",
        "production_probe_renew_failed",
        "production_reconciliation_blocked",
        "write_authorization_invalid",
        "writer_result_unknown",
    }
)
_CYCLE_STOP_PAYLOAD = b"stop_requested:controller_cycle_stop\n"
_CYCLE_STOP_CONTEXT: ContextVar[bool] = ContextVar(
    "oa_finance_preaudit_cycle_stop",
    default=False,
)


def stop_file() -> Path:
    raw = str(os.environ.get("OA_FINANCE_PREAUDIT_STOP_FILE") or "").strip()
    return Path(raw).expanduser() if raw else Path("runtime") / "stop-preaudit.flag"


def stop_requested() -> bool:
    raw = str(
        os.environ.get("OA_FINANCE_PREAUDIT_STOP_REQUESTED") or ""
    ).strip().lower()
    return raw in {"1", "true", "yes", "y", "on"} or stop_file().exists()


def _write_stop(reason: str | None) -> None:
    target = stop_file().expanduser()
    if not target.is_absolute():
        target = PROJECT_ROOT / target
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(target, flags, 0o600)
    except FileExistsError:
        return
    try:
        payload = (
            "stop_requested\n"
            if reason is None
            else f"stop_requested:{reason}\n"
        )
        os.write(descriptor, payload.encode("ascii"))
    finally:
        os.close(descriptor)


def request_stop(reason: str | None = None) -> None:
    if reason is None and _CYCLE_STOP_CONTEXT.get():
        reason = "controller_cycle_stop"
    if reason is not None and (
        _STOP_REASON.fullmatch(reason) is None
        or reason not in _ALLOWED_STOP_REASONS
    ):
        raise ValueError("stop_reason_invalid")
    _write_stop(reason)


def request_cycle_stop() -> None:
    token = _CYCLE_STOP_CONTEXT.set(True)
    try:
        request_stop()
    finally:
        _CYCLE_STOP_CONTEXT.reset(token)


def stop_reason() -> str | None:
    target = stop_file().expanduser()
    if target.is_symlink() or not target.is_file():
        return None
    try:
        payload = target.read_bytes()
    except OSError:
        return None
    for reason in _ALLOWED_STOP_REASONS:
        if payload == f"stop_requested:{reason}\n".encode("ascii"):
            return reason
    return None


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


def controller_lock_file() -> Path:
    raw = str(
        os.environ.get("OA_FINANCE_PREAUDIT_CONTROLLER_LOCK_FILE") or ""
    ).strip()
    return (
        Path(raw).expanduser()
        if raw
        else PROJECT_ROOT / "runtime" / "control" / "rolling-controller.lock"
    )


def request_writer_lock_file(request_id: str, base_path: Path | None = None) -> Path:
    base = Path(os.path.abspath(Path(base_path or writer_lock_file()).expanduser()))
    digest = hashlib.sha256(str(request_id).encode("utf-8")).hexdigest()[:16]
    return base.with_name(f"{base.stem}-{digest}{base.suffix or '.lock'}")


def _open_directory_nofollow(path: Path, *, create: bool = True) -> int:
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
                if not create:
                    raise
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


@contextmanager
def pinned_directory(path: Path, *, create: bool = False) -> Iterator[int]:
    """Pin a directory inode and reject path replacement while it is in use."""
    target = Path(os.path.abspath(Path(path).expanduser()))
    descriptor = _open_directory_nofollow(target, create=create)
    pinned = os.fstat(descriptor)
    try:
        yield descriptor
        current = os.stat(target, follow_symlinks=False)
        if (
            not stat.S_ISDIR(current.st_mode)
            or (current.st_dev, current.st_ino) != (pinned.st_dev, pinned.st_ino)
        ):
            raise RuntimeError("writer_lock_unsafe")
    except OSError as error:
        raise RuntimeError("writer_lock_unsafe") from error
    finally:
        os.close(descriptor)


def read_control_bytes(path: Path, *, max_bytes: int = 1_048_576) -> bytes | None:
    """Read one control artifact without following its file or parent links."""
    target = Path(os.path.abspath(Path(path).expanduser()))
    if max_bytes < 1 or not target.parent.exists():
        return None
    try:
        parent_descriptor = _open_directory_nofollow(
            target.parent,
            create=False,
        )
    except RuntimeError as error:
        raise RuntimeError("control_file_unsafe") from error
    file_descriptor: int | None = None
    try:
        parent_stat = os.fstat(parent_descriptor)
        flags = os.O_RDONLY | os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            file_descriptor = os.open(
                target.name,
                flags,
                dir_fd=parent_descriptor,
            )
        except FileNotFoundError:
            return None
        file_stat = os.fstat(file_descriptor)
        if not stat.S_ISREG(file_stat.st_mode):
            raise RuntimeError("control_file_unsafe")
        chunks: list[bytes] = []
        remaining = max_bytes + 1
        while remaining:
            chunk = os.read(file_descriptor, min(65_536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        current_parent = os.stat(target.parent, follow_symlinks=False)
        if (
            len(payload) > max_bytes
            or not stat.S_ISDIR(current_parent.st_mode)
            or (current_parent.st_dev, current_parent.st_ino)
            != (parent_stat.st_dev, parent_stat.st_ino)
        ):
            raise RuntimeError("control_file_unsafe")
        return payload
    except OSError as error:
        raise RuntimeError("control_file_unsafe") from error
    finally:
        if file_descriptor is not None:
            os.close(file_descriptor)
        os.close(parent_descriptor)


def write_control_bytes_atomic(path: Path, payload: bytes) -> None:
    """Atomically replace one control artifact inside a pinned directory."""
    target = Path(os.path.abspath(Path(path).expanduser()))
    if not target.name or target.name in {".", ".."}:
        raise RuntimeError("control_file_unsafe")
    parent_descriptor = _open_directory_nofollow(target.parent)
    temporary_name = f".{target.name}.{secrets.token_hex(8)}.tmp"
    file_descriptor: int | None = None
    temporary_exists = False
    try:
        parent_stat = os.fstat(parent_descriptor)
        try:
            current_target = os.stat(
                target.name,
                dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            current_target = None
        if current_target is not None and not stat.S_ISREG(current_target.st_mode):
            raise RuntimeError("control_file_unsafe")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        file_descriptor = os.open(
            temporary_name,
            flags,
            0o600,
            dir_fd=parent_descriptor,
        )
        temporary_exists = True
        view = memoryview(payload)
        while view:
            written = os.write(file_descriptor, view)
            if written < 1:
                raise RuntimeError("control_file_unsafe")
            view = view[written:]
        os.fsync(file_descriptor)
        os.close(file_descriptor)
        file_descriptor = None
        current_parent = os.stat(target.parent, follow_symlinks=False)
        if (
            not stat.S_ISDIR(current_parent.st_mode)
            or (current_parent.st_dev, current_parent.st_ino)
            != (parent_stat.st_dev, parent_stat.st_ino)
        ):
            raise RuntimeError("control_file_unsafe")
        os.replace(
            temporary_name,
            target.name,
            src_dir_fd=parent_descriptor,
            dst_dir_fd=parent_descriptor,
        )
        temporary_exists = False
        os.fsync(parent_descriptor)
    except (OSError, RuntimeError) as error:
        raise RuntimeError("control_file_unsafe") from error
    finally:
        if file_descriptor is not None:
            os.close(file_descriptor)
        if temporary_exists:
            try:
                os.unlink(temporary_name, dir_fd=parent_descriptor)
            except FileNotFoundError:
                pass
        os.close(parent_descriptor)


def clear_cycle_stop(*, expected_identity: tuple[int, int, int] | None = None) -> bool:
    """Remove only the controller-owned, recoverable cycle stop marker."""
    return _clear_stop(_CYCLE_STOP_PAYLOAD, expected_identity=expected_identity)


def clear_failure_stop(
    *,
    expected_reason: str,
    expected_identity: tuple[int, int, int] | None = None,
) -> bool:
    """Remove one controller-owned failure stop after an operator reconciled it.

    Only a stop reason from the fixed allowlist is accepted, and the marker payload
    must match that reason byte for byte.
    """
    if (
        _STOP_REASON.fullmatch(str(expected_reason)) is None
        or expected_reason not in _ALLOWED_STOP_REASONS
        or expected_reason == "controller_cycle_stop"
    ):
        raise ValueError("stop_reason_invalid")
    payload = f"stop_requested:{expected_reason}\n".encode("ascii")
    return _clear_stop(payload, expected_identity=expected_identity)


def clear_manual_stop(
    *, target: Path, expected_identity: tuple[int, int, int],
) -> bool:
    """Clear an exact manual marker after audited, lease-held stop recovery.

    This file operation grants no recovery authority. Callers must first bind
    original terminal evidence and complete live reconciliation under both leases.
    """
    if (len(expected_identity) != 3
        or any(type(value) is not int or value < 0 for value in expected_identity)):
        raise ValueError("stop_identity_invalid")
    return _clear_stop(b"stop_requested\n", expected_identity=expected_identity, target=target)


def _clear_stop(
    expected_payload: bytes,
    *,
    expected_identity: tuple[int, int, int] | None = None,
    target: Path | None = None,
) -> bool:
    """Remove one controller-owned stop marker whose payload matches exactly."""
    raw = str(
        os.environ.get("OA_FINANCE_PREAUDIT_STOP_REQUESTED") or ""
    ).strip().lower()
    if raw in {"1", "true", "yes", "y", "on"}:
        return False
    target = Path(target or stop_file()).expanduser()
    if not target.is_absolute():
        target = PROJECT_ROOT / target
    target = Path(os.path.abspath(target))
    if not target.name or target.name in {".", ".."} or not target.parent.exists():
        return False
    try:
        parent_descriptor = _open_directory_nofollow(target.parent, create=False)
    except RuntimeError as error:
        raise RuntimeError("control_file_unsafe") from error
    file_descriptor: int | None = None
    try:
        flags = os.O_RDONLY | os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            file_descriptor = os.open(
                target.name,
                flags,
                dir_fd=parent_descriptor,
            )
        except FileNotFoundError:
            return False
        pinned = os.fstat(file_descriptor)
        if expected_identity is not None and (
            pinned.st_dev, pinned.st_ino, pinned.st_mtime_ns
        ) != expected_identity:
            return False
        if (
            not stat.S_ISREG(pinned.st_mode)
            or pinned.st_uid != os.getuid()
            or pinned.st_mode & 0o077
        ):
            raise RuntimeError("control_file_unsafe")
        payload = os.read(file_descriptor, len(expected_payload) + 1)
        if payload != expected_payload:
            return False
        current = os.stat(
            target.name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        if (
            not stat.S_ISREG(current.st_mode)
            or (current.st_dev, current.st_ino) != (pinned.st_dev, pinned.st_ino)
            or current.st_mtime_ns != pinned.st_mtime_ns
        ):
            raise RuntimeError("control_file_unsafe")
        os.unlink(target.name, dir_fd=parent_descriptor)
        os.fsync(parent_descriptor)
        return True
    except OSError as error:
        raise RuntimeError("control_file_unsafe") from error
    finally:
        if file_descriptor is not None:
            os.close(file_descriptor)
        os.close(parent_descriptor)


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


def controller_active(path: Path | None = None) -> bool:
    base = Path(path or controller_lock_file()).expanduser().resolve()
    return writer_active(request_writer_lock_file(GLOBAL_CONTROLLER_KEY, base))


def controller_owner_pid(path: Path | None = None) -> int | None:
    base = Path(path or controller_lock_file()).expanduser().resolve()
    target = request_writer_lock_file(GLOBAL_CONTROLLER_KEY, base)
    if not writer_active(target):
        return None
    try:
        raw = target.read_text(encoding="utf-8").strip()
        pid = int(raw)
    except (OSError, ValueError):
        return None
    return pid if pid > 0 else None


@contextmanager
def request_writer_lease(
    request_id: str,
    path: Path | None = None,
    *,
    blocking: bool = True,
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
    acquired = False
    try:
        flags = fcntl.LOCK_EX if blocking else fcntl.LOCK_EX | fcntl.LOCK_NB
        fcntl.flock(descriptor.fileno(), flags)
        acquired = True
        descriptor.seek(0)
        descriptor.truncate()
        descriptor.write(str(os.getpid()))
        descriptor.flush()
        yield
    finally:
        if acquired:
            fcntl.flock(descriptor.fileno(), fcntl.LOCK_UN)
        descriptor.close()


@contextmanager
def global_writer_lease() -> Iterator[None]:
    """Acquire the one canonical lock shared by every OA write entrypoint."""
    with request_writer_lease(GLOBAL_WRITER_KEY, writer_lock_file()):
        yield


@contextmanager
def global_controller_lease() -> Iterator[None]:
    """Acquire the one canonical lock shared by rolling controllers."""
    try:
        with request_writer_lease(
            GLOBAL_CONTROLLER_KEY,
            controller_lock_file(),
            blocking=False,
        ):
            yield
    except BlockingIOError as error:
        raise RuntimeError("controller_already_active") from error
