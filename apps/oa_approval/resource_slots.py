from __future__ import annotations

from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import re
import threading
import time
from typing import Iterator, Optional


PROJECT_ROOT = Path(__file__).resolve().parents[2]
_SAFE_RESOURCE_NAME = re.compile(r"^[a-z][a-z0-9_-]{1,63}$")
_LOCAL_LOCK = threading.RLock()
_LOCAL_HELD: set[str] = set()


class ResourceSlotTimeout(TimeoutError):
    pass


class _Lease:
    def __init__(self, path: Path, descriptor: int) -> None:
        self.path = path
        self.descriptor = descriptor


def coordination_root() -> Path:
    configured = str(
        os.environ.get("OA_FINANCE_PREAUDIT_COORDINATION_ROOT") or ""
    ).strip()
    if configured:
        return Path(configured).expanduser()
    return PROJECT_ROOT / "runtime" / "finance_preaudit_coordination"


def _try_acquire(name: str, capacity: int, root: Path) -> Optional[_Lease]:
    root.mkdir(parents=True, exist_ok=True)
    with _LOCAL_LOCK:
        for index in range(capacity):
            path = root / f"{name}.{index}.lock"
            key = str(path.resolve())
            if key in _LOCAL_HELD:
                continue
            descriptor = os.open(str(path), os.O_CREAT | os.O_RDWR, 0o600)
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                os.close(descriptor)
                continue
            except Exception:
                os.close(descriptor)
                raise
            _LOCAL_HELD.add(key)
            try:
                os.ftruncate(descriptor, 0)
                os.write(descriptor, f"pid={os.getpid()}\n".encode("ascii"))
            except Exception:
                _LOCAL_HELD.discard(key)
                fcntl.flock(descriptor, fcntl.LOCK_UN)
                os.close(descriptor)
                raise
            return _Lease(path, descriptor)
    return None


def _release(lease: _Lease) -> None:
    key = str(lease.path.resolve())
    with _LOCAL_LOCK:
        try:
            fcntl.flock(lease.descriptor, fcntl.LOCK_UN)
        finally:
            os.close(lease.descriptor)
            _LOCAL_HELD.discard(key)


@contextmanager
def acquire(
    name: str,
    *,
    capacity: int,
    timeout_sec: float,
    root: Optional[Path] = None,
) -> Iterator[Path]:
    resource_name = str(name or "").strip()
    if not _SAFE_RESOURCE_NAME.fullmatch(resource_name):
        raise ValueError("invalid_resource_slot_name")
    normalized_capacity = int(capacity)
    if normalized_capacity < 1 or normalized_capacity > 64:
        raise ValueError("invalid_resource_slot_capacity")
    normalized_timeout = max(0.0, float(timeout_sec))
    slot_root = Path(root or coordination_root())
    deadline = time.monotonic() + normalized_timeout
    lease: Optional[_Lease] = None
    while lease is None:
        lease = _try_acquire(resource_name, normalized_capacity, slot_root)
        if lease is not None:
            break
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ResourceSlotTimeout(f"resource_slot_timeout:{resource_name}")
        time.sleep(min(0.05, remaining))
    try:
        yield lease.path
    finally:
        _release(lease)
