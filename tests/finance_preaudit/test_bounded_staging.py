import os
from pathlib import Path

import pytest

from apps.oa_approval import bounded_staging


def test_workspace_fails_closed_without_native_bounded_filesystem(tmp_path, monkeypatch):
    monkeypatch.setattr(bounded_staging.sys, 'platform', 'linux')
    with pytest.raises(ValueError, match='bounded_staging_unavailable'):
        with bounded_staging.workspace(tmp_path / 'image.dmg', max_bytes=32 * 1024 * 1024):
            pytest.fail('unbounded workspace exposed')
    assert not list(tmp_path.iterdir())


def test_workspace_rejects_too_small_reservation_before_starting_backend(tmp_path):
    with pytest.raises(ValueError, match='bounded_staging_quota_too_small'):
        with bounded_staging.workspace(tmp_path / 'image.dmg', max_bytes=1024):
            pytest.fail('undersized workspace exposed')
    assert not list(tmp_path.iterdir())


def test_workspace_bounds_native_image_and_cleans_up_after_body_failure(tmp_path, monkeypatch):
    import plistlib
    import subprocess

    image = tmp_path / 'image.dmg'
    calls = []
    mounted = set()

    def run(command, **kwargs):
        calls.append(command)
        if command[1] == 'create':
            image.write_bytes(b'bounded')
        if command[1] == 'attach':
            mount = Path(command[command.index('-mountpoint') + 1])
            mounted.add(mount)
            return subprocess.CompletedProcess(command, 0, plistlib.dumps({
                'system-entities': [{'dev-entry': '/dev/disk-test', 'mount-point': str(mount)}],
            }), b'')
        if command[1] == 'info' and mounted:
            return subprocess.CompletedProcess(command, 0, plistlib.dumps({'images': [{
                'image-path': str(image), 'system-entities': [{'dev-entry': '/dev/disk-test'}],
            }]}), b'')
        if command[1] == 'detach':
            mounted.clear()
        return subprocess.CompletedProcess(command, 0, plistlib.dumps({'images': []}), b'')

    monkeypatch.setattr(bounded_staging.sys, 'platform', 'darwin')
    monkeypatch.setattr(bounded_staging.subprocess, 'run', run)
    monkeypatch.setattr(bounded_staging.os.path, 'ismount', lambda path: Path(path) in mounted)
    with pytest.raises(RuntimeError, match='conversion_failed'):
        with bounded_staging.workspace(image, max_bytes=16 * 1024 * 1024) as work:
            assert work in mounted
            (work / 'output.pdf').write_bytes(b'pdf')
            (work / 'output.pdf').unlink()
            raise RuntimeError('conversion_failed')
    create = next(call for call in calls if call[1] == 'create')
    assert int(create[create.index('-sectors') + 1]) * 512 <= 16 * 1024 * 1024
    attach = next(call for call in calls if call[1] == 'attach')
    assert '-nobrowse' in attach
    assert not image.exists()
    assert not mounted
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize('marker_failure', [False, True])
def test_workspace_preserves_image_and_mount_when_detach_cannot_be_confirmed(tmp_path, monkeypatch, marker_failure):
    import json
    import plistlib
    import subprocess

    image = tmp_path / 'image.dmg'
    mounted = set()

    def run(command, **kwargs):
        if command[1] == 'create':
            image.write_bytes(b'bounded')
        if command[1] == 'attach':
            mounted.add(Path(command[command.index('-mountpoint') + 1]))
        if command[1] == 'detach':
            raise subprocess.CalledProcessError(1, command)
        return subprocess.CompletedProcess(command, 0, plistlib.dumps({'images': [{
            'image-path': str(image), 'system-entities': [{'dev-entry': '/dev/disk-test'}],
        }]}), b'')

    monkeypatch.setattr(bounded_staging.sys, 'platform', 'darwin')
    monkeypatch.setattr(bounded_staging.subprocess, 'run', run)
    monkeypatch.setattr(bounded_staging.os.path, 'ismount', lambda path: Path(path) in mounted)
    if marker_failure:
        original_open = bounded_staging.os.open

        def fail_marker(path, *args, **kwargs):
            if str(path).endswith('.cleanup.json'):
                raise OSError('host_filesystem_full')
            return original_open(path, *args, **kwargs)

        monkeypatch.setattr(bounded_staging.os, 'open', fail_marker)
    with pytest.raises(bounded_staging.BoundedStagingCleanupError, match='cleanup_unknown'):
        with bounded_staging.workspace(image, max_bytes=16 * 1024 * 1024) as work:
            assert work in mounted
    assert image.read_bytes() == b'bounded'
    if not marker_failure:
        marker = json.loads(image.with_name('image.dmg.cleanup.json').read_text())
        assert marker == {'image_path': str(image), 'mount_path': str(work), 'status': 'cleanup_unknown'}
    assert work.is_dir()


def test_workspace_refuses_existing_image_and_symlink(tmp_path, monkeypatch):
    monkeypatch.setattr(bounded_staging.sys, 'platform', 'darwin')
    image = tmp_path / 'existing.dmg'
    image.write_bytes(b'protected')
    link = tmp_path / 'link.dmg'
    link.symlink_to(image)
    for path in (image, link):
        with pytest.raises(ValueError, match='bounded_staging_path_invalid'):
            with bounded_staging.workspace(path, max_bytes=16 * 1024 * 1024):
                pytest.fail('existing evidence was exposed for overwrite')
    assert image.read_bytes() == b'protected'


@pytest.mark.skipif(
    os.environ.get('OA_BOUNDED_NATIVE_TEST') != '1',
    reason='explicit native disk-image acceptance',
)
def test_native_workspace_caps_aggregate_files_and_leaves_no_mount(tmp_path):
    import errno
    import os
    import plistlib
    import subprocess

    image = tmp_path / 'image.dmg'
    limit = 16 * 1024 * 1024
    with bounded_staging.workspace(image, max_bytes=limit) as work:
        assert image.stat().st_size <= limit
        assert os.path.ismount(work)
        for name in ('source', 'profile', 'tmp', 'output'):
            directory = work / name
            directory.mkdir()
            (directory / 'initial').write_bytes(b'x' * (1024 * 1024))
        with pytest.raises(OSError) as failure:
            with (work / 'tmp' / 'fill').open('wb', buffering=0) as output:
                for _ in range(32):
                    output.write(b'x' * (1024 * 1024))
        assert failure.value.errno == errno.ENOSPC
    assert not image.exists()
    assert not list(tmp_path.iterdir())
    info = plistlib.loads(subprocess.check_output(['/usr/bin/hdiutil', 'info', '-plist']))
    assert all(Path(item['image-path']).resolve() != image.resolve() for item in info['images'])
