from __future__ import annotations

from array import array
import hashlib
import json
import math
from pathlib import Path
import shutil
import subprocess
from tempfile import TemporaryDirectory
from typing import Any

import fitz


def _sha256(path: Path) -> str:
    with path.open('rb') as source:
        return hashlib.file_digest(source, 'sha256').hexdigest()


def _run(command: list[str]) -> bytes:
    result = subprocess.run(command, capture_output=True, check=True, timeout=60)
    return result.stdout


def render(
    path: Path, *, out_dir: Path, prefix: str, source_sha256: str,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    try:
        return _render(path.resolve(), out_dir=out_dir, prefix=prefix,
                       source_sha256=source_sha256)
    except (OSError, ValueError, KeyError, TypeError, OverflowError,
            subprocess.SubprocessError, RuntimeError):
        raise RuntimeError('attachment_content_incomplete') from None


def _render(
    path: Path, *, out_dir: Path, prefix: str, source_sha256: str,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    probe, decoder = shutil.which('ffprobe'), shutil.which('ffmpeg')
    if not probe or not decoder:
        raise ValueError('decoder_missing')
    probe_input = ['-v', 'error', '-protocol_whitelist', 'file,pipe', '-f', 'mov']
    info = json.loads(_run([
        probe, *probe_input, '-show_entries',
        'format=duration:stream=codec_type,width,height,sample_rate,channels,nb_frames:stream_side_data=rotation',
        '-of', 'json', str(path),
    ]))
    duration = float(info['format']['duration'])
    streams = info['streams']
    video = [s for s in streams if s['codec_type'] == 'video']
    audio = [s for s in streams if s['codec_type'] == 'audio']
    if (not math.isfinite(duration) or not 0 < duration <= 30
            or len(video) != 1 or len(audio) > 1
            or len(streams) != len(video) + len(audio)):
        raise ValueError('unsupported_streams')
    width, height = int(video[0]['width']), int(video[0]['height'])
    if not 0 < width <= 2160 or not 0 < height <= 2160:
        raise ValueError('dimensions_exceeded')
    if any(float(s.get('rotation', 0)) != 0 for s in video[0].get('side_data_list', [])):
        raise ValueError('rotation_unsupported')
    timeline = json.loads(_run([
        probe, *probe_input, '-select_streams', 'v:0', '-read_intervals', '%+#901',
        '-show_frames', '-show_entries', 'frame=best_effort_timestamp_time',
        '-of', 'json', str(path),
    ]))
    timestamps = [float(frame['best_effort_timestamp_time']) for frame in timeline['frames']]
    count = len(timestamps)
    if (not 0 < count <= 900 or width * height * count > 350_000_000
            or any(not math.isfinite(t) or not 0 <= t <= duration + .1 for t in timestamps)
            or any(a >= b for a, b in zip(timestamps, timestamps[1:]))):
        raise ValueError('frame_budget_or_timeline_invalid')
    declared_count = video[0].get('nb_frames')
    if declared_count not in (None, 'N/A') and int(declared_count) != count:
        raise ValueError('frame_count_mismatch')
    decode_input = [decoder, '-nostdin', '-v', 'error', '-xerror',
                    '-protocol_whitelist', 'file,pipe', '-f', 'mov', '-noautorotate', '-i', str(path)]
    frame_size = width * height * 3
    pages = []
    audio_state = 'absent'
    sample_count = 0
    with TemporaryDirectory(prefix='video-', dir=out_dir) as temporary:
        raw = Path(temporary) / 'frames.rgb'
        _run([*decode_input, '-map', '0:v:0', '-an', '-pix_fmt', 'rgb24',
              '-fps_mode', 'passthrough', '-frames:v', '901', '-fs',
              str(frame_size * count + 1), '-f', 'rawvideo', str(raw)])
        if raw.stat().st_size != frame_size * count:
            raise ValueError('decoded_frame_count_mismatch')
        if audio:
            rate, channels = int(audio[0]['sample_rate']), int(audio[0]['channels'])
            if not 0 < rate <= 48000 or not 0 < channels <= 2:
                raise ValueError('audio_budget_exceeded')
            sound = Path(temporary) / 'audio.f32'
            _run([*decode_input, '-map', '0:a:0', '-vn', '-c:a', 'pcm_f32le',
                  '-fs', '16000004', '-f', 'f32le', str(sound)])
            size = sound.stat().st_size
            if not 0 < size <= 16_000_000 or size % (4 * channels):
                raise ValueError('audio_incomplete')
            samples = array('f')
            with sound.open('rb') as source:
                samples.fromfile(source, size // 4)
            if any(sample != 0 for sample in samples):
                raise ValueError('audio_not_silent')
            sample_count = len(samples) // channels
            audio_state = 'digital_silence'
        output_bytes = 0
        with raw.open('rb') as source:
            for index in range(0, count, 2):
                tiles = min(2, count - index)
                sheet = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, width * tiles, height), False)
                frames = []
                for tile in range(tiles):
                    pixels = source.read(frame_size)
                    pixmap = fitz.Pixmap(fitz.csRGB, width, height, pixels, False)
                    pixmap.set_origin(tile * width, 0)
                    sheet.copy(pixmap, pixmap.irect)
                    frames.append({
                        'frame_index': index + tile, 'timestamp_seconds': timestamps[index + tile],
                        'x': tile * width, 'y': 0, 'width': width, 'height': height,
                        'decoded_rgb_sha256': hashlib.sha256(pixels).hexdigest(),
                    })
                png = sheet.tobytes('png')
                output_bytes += len(png)
                if output_bytes > 150_000_000:
                    raise ValueError('output_budget_exceeded')
                target = out_dir / f'{prefix}_video_{len(pages) + 1}.png'
                target.write_bytes(png)
                pages.append({'path': target, 'kind': f'video_frames_{index + 1}_{index + tiles}',
                              'content_sha256': hashlib.sha256(png).hexdigest(),
                              'video_frames': frames})
    if _sha256(path) != source_sha256:
        raise ValueError('source_changed')
    return pages, {'frame_count': count, 'duration_seconds': duration,
                   'width': width, 'height': height, 'audio_state': audio_state,
                   'audio_sample_count': sample_count}
