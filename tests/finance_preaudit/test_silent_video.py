from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import fitz
import pytest

from apps.oa_approval import attachment_extractors as extractor
from apps.oa_approval import attachment_recognition as recognition


@pytest.fixture
def recorded_video(tmp_path, monkeypatch):
    source = tmp_path / 'evidence.mp4'
    source.write_bytes(b'recorded-video-source')
    frames = [bytes([value, 0, 0]) * 6 for value in (20, 40, 60)]
    settings = {'audio': True, 'nonzero_audio': False, 'missing_frame': False,
                'duration': '1.0', 'source_changed': False}
    monkeypatch.setattr(extractor.shutil, 'which', lambda name: '/recorded/' + name)
    def run(command, **kwargs):
        if command[0].endswith('ffprobe'):
            if '-show_frames' in command:
                result = {'frames': [{'best_effort_timestamp_time': str(i / 3)} for i in range(3)]}
            else:
                result = {'format': {'duration': settings['duration']}, 'streams': [
                    {'codec_type': 'video', 'width': 3, 'height': 2},
                    *([{'codec_type': 'audio', 'sample_rate': '8000', 'channels': 1}] if settings['audio'] else [])]}
            return subprocess.CompletedProcess(command, 0, json.dumps(result).encode(), b'')
        output = Path(command[-1])
        if 'rawvideo' in command:
            output.write_bytes(b''.join(frames[:-1] if settings['missing_frame'] else frames))
            if settings['source_changed']:
                source.write_bytes(b'changed-source')
        else:
            output.write_bytes((b'\x01' if settings['nonzero_audio'] else b'\0') * 32)
        return subprocess.CompletedProcess(command, 0, b'', b'')
    monkeypatch.setattr(extractor.subprocess, 'run', run)
    return source, frames, settings


@pytest.mark.parametrize('audio', [True, False])
def test_public_preparation_keeps_every_silent_video_frame_and_original_pixels(recorded_video, tmp_path, audio):
    source, frames, settings = recorded_video
    settings['audio'] = audio
    images, manifest, contents = extractor.prepare_materials(
        [{'path': source, 'resource_id': 'video-1', 'name': source.name}], out_dir=tmp_path / 'prepared')
    assert len(images) == 2 and manifest[0]['status'] == 'video_rendered'
    assert manifest[0]['frame_count'] == 3 and manifest[0]['image_count'] == 2
    assert manifest[0]['audio_state'] == ('digital_silence' if audio else 'absent')
    observed = []
    for page in images:
        pixmap = fitz.Pixmap(str(page['path']))
        assert page['resource_id'] == 'video-1'
        assert page['source_content_sha256'] == hashlib.sha256(source.read_bytes()).hexdigest()
        assert page['content_sha256'] == hashlib.sha256(Path(page['path']).read_bytes()).hexdigest()
        for frame in page['video_frames']:
            index = frame['frame_index']
            x = frame['x']
            actual = b''.join(pixmap.samples[(y * pixmap.width + x) * 3:(y * pixmap.width + x + 3) * 3] for y in range(2))
            assert actual == frames[index]
            assert frame['decoded_rgb_sha256'] == hashlib.sha256(actual).hexdigest()
            assert frame['timestamp_seconds'] == pytest.approx(index / 3)
            observed.append(index)
    assert observed == [0, 1, 2]
    assert contents[0]['document_type'] == 'video'
    assert contents[0]['frame_count'] == 3


@pytest.mark.parametrize('damage', ['nonzero_audio', 'missing_frame', 'duration', 'source_changed'])
def test_public_video_preparation_rejects_unread_audio_or_incomplete_source(recorded_video, tmp_path, damage):
    source, _, settings = recorded_video
    settings[damage] = '31.0' if damage == 'duration' else True
    with pytest.raises(RuntimeError, match='^attachment_content_incomplete$'):
        extractor.prepare_materials([{'path': source, 'resource_id': 'video-1'}], out_dir=tmp_path / 'prepared')


def test_public_video_preparation_rejects_missing_decoder(recorded_video, tmp_path, monkeypatch):
    source, _, _ = recorded_video
    monkeypatch.setattr(extractor.shutil, 'which', lambda _name: None)
    with pytest.raises(RuntimeError, match='^attachment_content_incomplete$'):
        extractor.prepare_materials([{'path': source, 'resource_id': 'video-1'}], out_dir=tmp_path / 'prepared')


def test_public_reader_preserves_video_frame_coverage(recorded_video, tmp_path, monkeypatch):
    source, _, _ = recorded_video
    monkeypatch.setenv('OA_REVIEW_API_KEY', 'offline-secret')
    calls = []
    def post(body, **kwargs):
        calls.append(body)
        return {'choices': [{'finish_reason': 'stop', 'message': {'content': 'Frame transcription'}}]}
    monkeypatch.setattr(recognition, '_post', post)
    result = recognition.recognize(files=[source], prompt='Read every frame in temporal order',
                                   output_dir=tmp_path / 'recognition')
    assert result['status'] == 'complete'
    assert len(calls) == len(result['parts']) == 3
    assert [f['frame_index'] for p in result['parts'] if p['kind'] == 'image'
            for f in p['video_frames']] == [0, 1, 2]
    assert all(p['resource_id'] == 'file-1' for p in result['parts'])


@pytest.mark.parametrize('failure', ['probe', 'decode', 'timeout'])
def test_public_video_preparation_rejects_decoder_failure(recorded_video, tmp_path, monkeypatch, failure):
    source, _, _ = recorded_video
    recorded_run = extractor.subprocess.run
    def run(command, **kwargs):
        if (failure == 'probe' and command[0].endswith('ffprobe')
                or failure != 'probe' and command[0].endswith('ffmpeg')):
            if failure == 'timeout':
                raise subprocess.TimeoutExpired(command, 60)
            raise subprocess.CalledProcessError(1, command)
        return recorded_run(command, **kwargs)
    monkeypatch.setattr(extractor.subprocess, 'run', run)
    with pytest.raises(RuntimeError, match='^attachment_content_incomplete$'):
        extractor.prepare_materials([{'path': source, 'resource_id': 'video-1'}], out_dir=tmp_path / 'prepared')
