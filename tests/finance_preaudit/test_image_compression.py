from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import sys
import struct
import zlib

from PIL import Image
import pytest

from apps.oa_approval.image_compression import compress_image


def invoke(source, destination, *options):
    result = subprocess.run([sys.executable, '-B', '-c', "from apps.oa_approval import material_store; material_store.discover_project = lambda root: {'checkouts': [], 'scopes': {}, 'files': set()}; from apps.oa_approval.image_compression import main; raise SystemExit(main())",
        '--file', str(source), '--output-dir', str(destination), *options],
        capture_output=True, text=True, timeout=70)
    return result, json.loads(result.stdout)


def test_agent_cli_compresses_copy_with_source_provenance(tmp_path):
    source = tmp_path / 'source.jpg'
    with Image.new('RGB', (400, 200), 'red') as image:
        image.save(source, 'JPEG')
    original = source.read_bytes()
    result, report = invoke(source, tmp_path / 'compressed', '--max-dimension', '100',
        '--max-pixels', '5000', '--max-bytes', '20000')
    assert result.returncode == 0
    assert report['status'] == 'complete'
    assert report['source']['sha256'] == hashlib.sha256(original).hexdigest()
    assert report['source']['width'] == 400
    assert report['output']['width'] == 100
    assert report['output']['height'] == 50
    assert report['output']['bytes'] <= 20000
    assert report['resized'] is True
    assert report['readability'] == 'not_verified'
    with Image.open(report['output']['path']) as image:
        assert image.size == (100, 50)
        assert image.getpixel((50, 25))[0] > 240
    assert source.read_bytes() == original
    assert json.loads(Path(report['report_file']).read_text()) == report


def test_transparent_png_keeps_alpha_and_byte_limit(tmp_path):
    source = tmp_path / 'alpha.png'
    with Image.new('RGBA', (200, 100), (255, 0, 0, 51)) as image:
        image.save(source)
    result, report = invoke(source, tmp_path / 'output', '--max-dimension', '100', '--max-bytes', '1024')
    assert result.returncode == 0
    assert report['output']['format'] == 'PNG'
    assert report['output']['bytes'] <= 1024
    with Image.open(report['output']['path']) as image:
        assert image.size == (100, 50)
        assert image.getpixel((50, 25))[3] == 51


def test_exif_rotation_is_applied_to_output_pixels(tmp_path):
    source = tmp_path / 'rotated.jpg'
    with Image.new('RGB', (80, 40), 'blue') as image:
        exif = Image.Exif()
        exif[274] = 6
        image.save(source, exif=exif)
    result, report = invoke(source, tmp_path / 'output')
    assert result.returncode == 0
    assert report['orientation_applied'] is True
    with Image.open(report['output']['path']) as image:
        assert image.size == (40, 80)
        assert image.getexif().get(274, 1) == 1


def test_file_size_target_shrinks_noisy_image(tmp_path):
    source = tmp_path / 'noise.png'
    with Image.effect_noise((128, 128), 100) as image:
        image.save(source)
    result, report = invoke(source, tmp_path / 'output', '--max-bytes', '1024')
    assert result.returncode == 0
    assert report['output']['bytes'] <= 1024 < source.stat().st_size
    assert report['output']['width'] < 128


def test_compression_capacity_failure_keeps_disposable_source_while_reading(tmp_path, monkeypatch):
    from apps.oa_approval import image_compression, material_store

    source = tmp_path / 'source.png'
    with Image.effect_noise((128, 128), 100) as image:
        image.convert('RGB').save(source)
    original = source.read_bytes()
    monkeypatch.setattr(material_store, 'DEFAULT_LIMIT_BYTES', len(original) + 1024)
    store = material_store.MaterialStore()
    store.adopt(source)
    store.mark_disposable(source, closed=True, write_state='not_applicable')
    report = image_compression.compress_image(source, output_dir=tmp_path / 'output', max_bytes=1024)
    assert report['status'] == 'failed'
    assert report['error_code'] == 'material_budget_exceeded'
    assert source.read_bytes() == original
    assert store.report()['retired_count'] == 0


@pytest.mark.parametrize('failure', ['animation', 'invalid', 'source_pixels', 'decoded_pixels', 'timeout'])
def test_failures_leave_explicit_report_without_output(tmp_path, failure):
    source = tmp_path / 'source.png'
    options = []
    with Image.new('RGB', (16, 16), 'red') as image:
        image.save(source)
        if failure == 'animation':
            with Image.new('RGB', (16, 16), 'blue') as second:
                image.save(source, format='GIF', save_all=True, append_images=[second])
    if failure in {'source_pixels', 'decoded_pixels'}:
        raw = bytearray(source.read_bytes())
        raw[16:24] = struct.pack('>II', *( (20000, 20000) if failure == 'source_pixels' else (9000, 8000)))
        raw[29:33] = struct.pack('>I', zlib.crc32(raw[12:29]))
        source.write_bytes(raw)
    if failure == 'invalid': source.write_bytes(b'not an image')
    if failure == 'timeout': options = ['--timeout-seconds', '0.001']
    original = source.read_bytes()
    result, report = invoke(source, tmp_path / 'output', *options)
    assert result.returncode == 1
    assert report['status'] == 'failed'
    expected = {'animation': 'image_animation_unsupported', 'invalid': 'image_invalid_or_unsupported',
        'source_pixels': 'image_source_pixels_exceeded', 'decoded_pixels': 'image_decode_pixels_exceeded',
        'timeout': 'image_compression_timeout'}
    assert report['error_code'] == expected[failure]
    assert 'output' not in report
    assert source.read_bytes() == original
    assert [p.name for p in (tmp_path / 'output').iterdir()] == ['compression.json']


def test_existing_output_and_frozen_attempt_are_preserved(tmp_path):
    source = tmp_path / 'source.png'
    with Image.new('RGB', (10, 10)) as image:
        image.save(source)
    existing = tmp_path / 'existing'
    existing.mkdir()
    marker = existing / 'compression.json'
    marker.write_text('preserve')
    result, report = invoke(source, existing)
    assert result.returncode == 1
    assert report['error_code'] == 'image_output_exists'
    assert marker.read_text() == 'preserve'
    frozen = tmp_path / 'attempt-2'
    frozen.mkdir()
    (frozen / 'action.json').write_text('{}')
    result, report = invoke(source, frozen / 'output')
    assert result.returncode == 1
    assert report['error_code'] == 'image_output_inside_frozen_attempt'
    assert not (frozen / 'output').exists()


def test_library_call_keeps_parent_image_protection(tmp_path):
    source = tmp_path / 'source.jpg'
    with Image.new('RGB', (400, 200), 'red') as image:
        image.save(source)
    before = Image.MAX_IMAGE_PIXELS
    report = compress_image(source, output_dir=tmp_path / 'out', max_dimension=100)
    assert report['status'] == 'complete'
    assert report['decoded_size'] == [100, 50]
    assert Image.MAX_IMAGE_PIXELS == before


@pytest.mark.parametrize('option,value', [('--max-dimension', '0'), ('--max-pixels', '16000001'),
    ('--max-bytes', '512'), ('--quality', '99')])
def test_invalid_limits_do_not_create_output(tmp_path, option, value):
    result, report = invoke(tmp_path / 'absent.jpg', tmp_path / 'output', option, value)
    assert result.returncode == 1
    assert report['error_code'] == 'image_compression_limits_invalid'
    assert not (tmp_path / 'output').exists()


def test_source_byte_budget_is_checked_before_decoding(tmp_path):
    source = tmp_path / 'oversized.jpg'
    with source.open('wb') as stream:
        stream.truncate(32 * 1024 * 1024 + 1)
    result, report = invoke(source, tmp_path / 'output')
    assert result.returncode == 1
    assert report['error_code'] == 'image_source_bytes_exceeded'


def test_progressive_header_over_decode_budget_is_rejected_before_loading(tmp_path):
    source = tmp_path / 'progressive.jpg'
    with Image.new('RGB', (16, 16), 'red') as image:
        image.save(source, progressive=True)
    raw = bytearray(source.read_bytes())
    marker = raw.index(b'\xff\xc2')
    raw[marker + 5:marker + 9] = struct.pack('>HH', 8000, 9000)
    source.write_bytes(raw)
    result, report = invoke(source, tmp_path / 'output')
    assert result.returncode == 1
    assert report['error_code'] == 'image_progressive_decode_budget_exceeded'


@pytest.mark.parametrize('size', [(400, 1), (1, 400)])
def test_thin_images_obey_total_pixel_limit_after_rounding(tmp_path, size):
    source = tmp_path / 'thin.png'
    with Image.new('RGB', size, 'red') as image:
        image.save(source)
    result, report = invoke(source, tmp_path / 'out', '--max-pixels', '1')
    assert result.returncode == 0
    with Image.open(report['output']['path']) as image:
        assert image.size == (1, 1)
