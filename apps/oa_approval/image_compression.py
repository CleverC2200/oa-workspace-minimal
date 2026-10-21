from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
from io import BytesIO
import json
import math
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
from uuid import uuid4

from . import material_store, runtime_control


MAX_SOURCE_BYTES = 32 * 1024 * 1024
MAX_SOURCE_PIXELS = 300_000_000
MAX_DECODED_PIXELS = 64_000_000
WORKER_DATA_BYTES = 1024 * 1024 * 1024


def _read_source(path: Path) -> bytes:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, 'rb') as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError('image_source_not_regular')
        raw = stream.read(MAX_SOURCE_BYTES + 1)
    if len(raw) > MAX_SOURCE_BYTES:
        raise ValueError('image_source_bytes_exceeded')
    return raw


def _target_size(width, height, dimension, pixels):
    scale = min(1, dimension / width, dimension / height, math.sqrt(pixels / (width * height)))
    width, height = max(1, int(width * scale)), max(1, int(height * scale))
    if width * height > pixels:
        if width >= height:
            width = max(1, pixels // height)
        else:
            height = max(1, pixels // width)
    return width, height


def _worker(args) -> dict:
    import resource
    import warnings
    memory_limit_enforced = sys.platform == 'linux'
    if memory_limit_enforced:
        resource.setrlimit(resource.RLIMIT_DATA, (WORKER_DATA_BYTES, WORKER_DATA_BYTES))
    resource.setrlimit(resource.RLIMIT_CPU, (30, 31))
    from PIL import Image, ImageOps
    Image.MAX_IMAGE_PIXELS = MAX_SOURCE_PIXELS // 2
    warnings.simplefilter('ignore', Image.DecompressionBombWarning)
    try:
        with Image.open(args.file, formats=['JPEG', 'PNG', 'WEBP', 'GIF']) as image:
            width, height = image.size
            source_format, source_mode = image.format, image.mode
            if width * height > MAX_SOURCE_PIXELS:
                raise ValueError('image_source_pixels_exceeded')
            if getattr(image, 'n_frames', 1) != 1:
                raise ValueError('image_animation_unsupported')
            if source_format == 'JPEG' and image.info.get('progressive') and width * height > MAX_DECODED_PIXELS:
                raise ValueError('image_progressive_decode_budget_exceeded')
            target = _target_size(width, height, args.max_dimension, args.max_pixels)
            if source_format == 'JPEG':
                image.draft('RGB', target)
            decoded_size = image.size
            if image.width * image.height > MAX_DECODED_PIXELS:
                raise ValueError('image_decode_pixels_exceeded')
            image.load()
            orientation = image.getexif().get(274, 1)
            ImageOps.exif_transpose(image, in_place=True)
            image.thumbnail(_target_size(image.width, image.height, args.max_dimension, args.max_pixels),
                            Image.Resampling.LANCZOS)
            transparent = 'A' in image.getbands() or 'transparency' in image.info
            output_format = 'PNG' if transparent or source_format in {'PNG', 'GIF'} else 'JPEG'
            mode = 'RGBA' if transparent else 'RGB'
            with image.convert(mode) as rendered:
                for _ in range(24):
                    encoded = BytesIO()
                    if output_format == 'PNG':
                        rendered.save(encoded, format='PNG', compress_level=6)
                    else:
                        rendered.save(encoded, format='JPEG', quality=args.quality, subsampling=0)
                    raw = encoded.getvalue()
                    if (len(raw) <= args.max_bytes and max(rendered.size) <= args.max_dimension
                            and rendered.width * rendered.height <= args.max_pixels):
                        name = 'image.png' if output_format == 'PNG' else 'image.jpg'
                        target_file = getattr(args, 'reserved_output', None) or args.output_dir / name
                        descriptor = os.open(target_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                        with os.fdopen(descriptor, 'wb') as stream:
                            stream.write(raw)
                        return {'status': 'complete', 'worker_data_limit_enforced': memory_limit_enforced,
                            'source': {'width': width, 'height': height, 'format': source_format, 'mode': source_mode},
                            'decoded_size': list(decoded_size), 'orientation_applied': orientation in range(2, 9),
                            'output': {'name': name, 'width': rendered.width, 'height': rendered.height,
                                       'format': output_format, 'mode': mode},
                            'resized': rendered.size != ((height, width) if orientation in (5, 6, 7, 8) else (width, height)),
                            'encoding_lossy': output_format == 'JPEG', 'metadata_preserved': False}
                    if rendered.size == (1, 1):
                        break
                    rendered.thumbnail((max(1, int(rendered.width * 0.8)), max(1, int(rendered.height * 0.8))),
                                       Image.Resampling.LANCZOS)
        raise ValueError('image_target_bytes_unreachable')
    except Image.DecompressionBombError:
        raise ValueError('image_source_pixels_exceeded') from None
    except MemoryError:
        raise ValueError('image_worker_memory_exceeded') from None
    except (OSError, SyntaxError):
        raise ValueError('image_invalid_or_unsupported') from None


def compress_image(source: Path, *, output_dir: Path | None = None, max_dimension: int = 8192,
                   max_pixels: int = 16_000_000, max_bytes: int = 8 * 1024 * 1024,
                   quality: int = 90, timeout_seconds: float = 60) -> dict:
    source = Path(source).expanduser().absolute()
    destination = Path(output_dir or runtime_control.PROJECT_ROOT / 'runtime' / 'image-compressions' / uuid4().hex).expanduser().absolute()
    report = {'schema_version': 1, 'kind': 'agent_image_compression', 'status': 'failed',
              'created_at': datetime.now(timezone.utc).isoformat(), 'report_file': None,
              'readability': 'not_verified', 'evidence_scope': 'derived_reading_copy'}
    try:
        if (not 1 <= max_dimension <= 8192 or not 1 <= max_pixels <= 16_000_000
                or not 1024 <= max_bytes <= MAX_SOURCE_BYTES or not 40 <= quality <= 95
                or not 0 < timeout_seconds <= 60):
            raise ValueError('image_compression_limits_invalid')
        resolved = destination.resolve()
        if any(re.fullmatch(r'attempt-[1-9][0-9]*', p.name) and (p / 'action.json').exists()
               for p in (resolved, *resolved.parents)):
            raise ValueError('image_output_inside_frozen_attempt')
        destination = resolved
        destination.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        destination.mkdir(mode=0o700)
        report['report_file'] = str(destination / 'compression.json')
        report['limits'] = {'max_dimension': max_dimension, 'max_pixels': max_pixels, 'max_bytes': max_bytes,
            'quality': quality, 'source_max_bytes': MAX_SOURCE_BYTES, 'source_max_pixels': MAX_SOURCE_PIXELS,
            'decoded_max_pixels': MAX_DECODED_PIXELS, 'worker_data_bytes': WORKER_DATA_BYTES,
            'worker_cpu_seconds': 30, 'timeout_seconds': timeout_seconds}
        material_store.MaterialStore().adopt_directory(destination, source={'kind': 'image_compression'})
        raw = _read_source(source)
        source_digest = hashlib.sha256(raw).hexdigest()
        report['source'] = {'path': str(source), 'sha256': source_digest, 'bytes': len(raw)}
        with tempfile.TemporaryDirectory(prefix='.working-', dir=destination) as scratch:
            scratch = Path(scratch)
            input_file = scratch / 'source'
            material_store.write_bytes(input_file, raw, source={'kind': 'image_compression_source'})
            input_file.chmod(0o600)
            reserved_output = scratch / 'encoded'
            command = [sys.executable, '-B', '-m', __spec__.name, '--worker', '--file', str(input_file),
                '--output-dir', str(scratch), '--max-dimension', str(max_dimension), '--max-pixels', str(max_pixels),
                '--max-bytes', str(max_bytes), '--quality', str(quality), '--reserved-output', str(reserved_output)]
            with material_store.MaterialStore().reserved_file(reserved_output, max_bytes=max_bytes, external=True,
                                                               source={'kind': 'compressed_image'}):
                result = subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True,
                    text=True, timeout=timeout_seconds)
            if result.returncode != 0:
                raise ValueError('image_compression_worker_failed')
            detail = json.loads(result.stdout)
            if detail['status'] != 'complete':
                raise ValueError(detail['error_code'])
            output = reserved_output
            content = _read_source(output)
            if hashlib.sha256(_read_source(source)).hexdigest() != source_digest:
                raise ValueError('image_source_changed')
            final = destination / detail['output']['name']
            output.rename(final)
            report.update({key: value for key, value in detail.items() if key not in {'source', 'output'}})
            report['source'].update(detail['source'])
            report['output'] = {**detail['output'], 'path': str(final), 'bytes': len(content),
                                'sha256': hashlib.sha256(content).hexdigest()}
    except subprocess.TimeoutExpired:
        report['error_code'] = 'image_compression_timeout'
    except FileExistsError:
        report['error_code'] = 'image_output_exists'
    except (OSError, ValueError, KeyError, TypeError) as error:
        code = str(error)
        report['error_code'] = code if re.fullmatch(r'(?:image|material)_[a-z_]+', code) else 'image_compression_failed'
    report['completed_at'] = datetime.now(timezone.utc).isoformat()
    if report['report_file']:
        try:
            runtime_control.write_control_bytes_atomic(Path(report['report_file']),
                (json.dumps(report, ensure_ascii=False) + '\n').encode())
        except (OSError, RuntimeError):
            report.update(status='failed', error_code='image_compression_report_failed')
    return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description='Create a bounded image reading copy while preserving the source.',
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument('--file', type=Path, required=True, help='Local static source image; source remains unchanged.')
    parser.add_argument('--output-dir', type=Path, help='New private directory outside frozen attempts.')
    parser.add_argument('--max-dimension', type=int, default=8192, help='Longest output edge, 1..8192 pixels.')
    parser.add_argument('--max-pixels', type=int, default=16_000_000, help='Output pixel count, 1..16000000.')
    parser.add_argument('--max-bytes', type=int, default=8 * 1024 * 1024, help='Output bytes, 1024..33554432.')
    parser.add_argument('--quality', type=int, default=90, help='JPEG encoding quality, 40..95.')
    parser.add_argument('--timeout-seconds', type=float, default=60, help='Worker wait limit, greater than 0 and at most 60.')
    parser.add_argument('--reserved-output', type=Path, help=argparse.SUPPRESS)
    parser.add_argument('--worker', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.worker:
        try:
            result = _worker(args)
        except (ValueError, OSError) as error:
            code = str(error)
            result = {'status': 'failed', 'error_code': code if re.fullmatch(r'image_[a-z_]+', code)
                      else 'image_compression_worker_failed'}
        print(json.dumps(result))
        return 0
    result = compress_image(args.file, output_dir=args.output_dir, max_dimension=args.max_dimension,
        max_pixels=args.max_pixels, max_bytes=args.max_bytes, quality=args.quality, timeout_seconds=args.timeout_seconds)
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result['status'] == 'complete' else 1


if __name__ == '__main__':
    raise SystemExit(main())
