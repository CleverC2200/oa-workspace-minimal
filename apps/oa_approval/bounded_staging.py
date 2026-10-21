from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
import json
import os
from pathlib import Path
import plistlib
import subprocess
import sys
import tempfile


class BoundedStagingCleanupError(RuntimeError):
    pass


def _run(*arguments: str) -> bytes:
    return subprocess.run(
        ['/usr/bin/hdiutil', *arguments],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=60,
    ).stdout


def _devices(image_path: Path) -> list[str]:
    result = plistlib.loads(_run('info', '-plist'))
    return [
        entity['dev-entry']
        for image in result['images']
        if Path(image['image-path']).resolve() == image_path
        for entity in image['system-entities']
        if entity.get('dev-entry')
    ]


def _cleanup(image_path: Path, mount: Path, *, attach_started: bool) -> None:
    try:
        if attach_started:
            devices = _devices(image_path)
            if devices:
                _run('detach', devices[0])
            if _devices(image_path) or os.path.ismount(mount):
                raise RuntimeError('bounded_staging_still_attached')
        mount.rmdir()
        image_path.unlink(missing_ok=True)
    except Exception as error:
        marker = image_path.with_name(image_path.name + '.cleanup.json')
        try:
            descriptor = os.open(marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(descriptor, 'w', encoding='utf-8') as output:
                json.dump({
                    'image_path': str(image_path),
                    'mount_path': str(mount),
                    'status': 'cleanup_unknown',
                }, output)
        except OSError:
            pass
        raise BoundedStagingCleanupError('bounded_staging_cleanup_unknown') from error


@contextmanager
def workspace(image_path: Path, *, max_bytes: int) -> Iterator[Path]:
    if sys.platform != 'darwin':
        raise ValueError('bounded_staging_unavailable')
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 16 * 1024 * 1024:
        raise ValueError('bounded_staging_quota_too_small')
    image_path = Path(image_path)
    if image_path.suffix != '.dmg' or image_path.exists() or image_path.is_symlink():
        raise ValueError('bounded_staging_path_invalid')
    image_path = image_path.parent.resolve(strict=True) / image_path.name
    mount = Path(tempfile.mkdtemp(prefix='.oa-bounded-', dir=image_path.parent))
    attach_started = False
    try:
        try:
            _run(
                'create', '-sectors', str((max_bytes - 65536) // 512),
                '-fs', 'HFS+', '-layout', 'NONE', '-type', 'UDIF',
                '-nospotlight', '-volname', 'OAStaging', str(image_path),
            )
            image_path.chmod(0o600)
            if image_path.stat().st_size > max_bytes:
                raise ValueError('bounded_staging_quota_exceeded')
            attach_started = True
            _run('attach', str(image_path), '-mountpoint', str(mount), '-nobrowse', '-noautoopen', '-plist')
            if not os.path.ismount(mount):
                raise ValueError('bounded_staging_mount_invalid')
        except (OSError, subprocess.SubprocessError, plistlib.InvalidFileException) as error:
            raise ValueError('bounded_staging_unavailable') from error
        yield mount
    finally:
        _cleanup(image_path, mount, attach_started=attach_started)
