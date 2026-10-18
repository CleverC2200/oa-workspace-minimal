from __future__ import annotations

import hashlib
import json

import pytest

from apps import json_artifact
from apps.oa_approval import attachment_extractors as extractor
from apps.oa_approval import attachment_recognition as recognition
from apps.oa_finance_preaudit import case_bundle, flow_engine
from tests.finance_preaudit.test_attachment_recognition import fake_api, image
from tests.finance_preaudit.test_collect_case_contract import FLOW_CONFIGS, RecordedOaApi


@pytest.mark.parametrize('suffix', ['.mp4', '.MOV', '.m4v', '.avi', '.mkv', '.webm'])
def test_public_preparation_skips_video_without_decoding(tmp_path, monkeypatch, suffix):
    video = tmp_path / ('evidence' + suffix)
    video.write_bytes(b'opaque-video-content')
    monkeypatch.setattr(extractor.subprocess, 'run', lambda *_a, **_k: pytest.fail('video decoder invoked'))
    images, manifest, contents = extractor.prepare_materials(
        [{'path': video, 'resource_id': 'video-1'}], out_dir=tmp_path / 'prepared')
    assert images == contents == []
    assert manifest[0]['status'] == 'video_skipped'
    assert manifest[0]['body_read_status'] == 'not_read'
    assert manifest[0]['source_content_sha256'] == hashlib.sha256(video.read_bytes()).hexdigest()
    assert list((tmp_path / 'prepared').iterdir()) == []


@pytest.mark.parametrize('archived', [False, True])
def test_public_reader_reports_skipped_video_and_reads_other_files(tmp_path, monkeypatch, archived):
    calls = fake_api(monkeypatch)
    video = tmp_path / 'evidence.mp4'
    video.write_bytes(b'opaque-video-content')
    photo = image(tmp_path)
    files = [video, photo]
    if archived:
        archive = tmp_path / 'evidence.zip'
        archive.write_bytes(b'recorded-archive')
        original = extractor.prepare_materials
        def prepare(items, **kwargs):
            return original([{**items[0], 'extracted_files': [
                {'path': video, 'resource_id': 'video-1', 'parent_resource_id': 'file-1'},
                {'path': photo, 'resource_id': 'photo-1', 'parent_resource_id': 'file-1'},
            ]}], **kwargs)
        monkeypatch.setattr(extractor, 'prepare_materials', prepare)
        files = [archive]
    report = recognition.recognize(files=files, output_dir=tmp_path / 'recognition')
    assert report['status'] == 'complete'
    assert len(calls) == len(report['parts']) == 1
    assert report['skipped_sources'][0]['body_read_status'] == 'not_read'
    assert report['coverage'] == 'non_video_only'
    assert 'opaque-video-content' not in json.dumps(calls)


def test_video_only_reader_returns_skipped_without_requests(tmp_path, monkeypatch):
    video = tmp_path / 'evidence.mp4'
    video.write_bytes(b'opaque-video-content')
    fake_api(monkeypatch)
    monkeypatch.setattr(recognition, '_post', lambda *_a, **_k: pytest.fail('video sent to model'))
    report = recognition.recognize(files=[video], output_dir=tmp_path / 'recognition')
    assert report['status'] == 'skipped'
    assert report['parts'] == []
    assert report['skipped_sources'][0]['resource_id'] == 'file-1'


def test_public_collection_freeze_and_reader_keep_video_gap(tmp_path, monkeypatch):
    calls = fake_api(monkeypatch)
    source = tmp_path / 'attachments' / 'evidence.mp4'
    source.parent.mkdir()
    source.write_bytes(b'opaque-video-content')
    monkeypatch.setattr(case_bundle, '_travel_resources', lambda *_a, **_k: ([], []))
    monkeypatch.setattr(case_bundle.evidence_packet, 'logical_attachment_resources',
                        lambda *_: [{'id': 'video-1', 'name': source.name}])
    monkeypatch.setattr(case_bundle.attachments, 'download_resources', lambda *_a, **_k: (
        [{'path': str(source), 'resource_id': 'video-1', 'name': source.name}], []))
    monkeypatch.setattr(case_bundle.invoice_buyers, 'read_invoice_buyers', lambda *_a, **_k: {
        'read_status': 'not_applicable', 'invoices': []})
    material = case_bundle.collect_case('recorded-request',
        oa_api=RecordedOaApi('195021', '通用费用报销申请流程'), configs=FLOW_CONFIGS,
        allow_browser_download=False, attachment_root=source.parent, prepared_root=tmp_path / 'prepared')
    assert material['prepared']['manifest'][0]['status'] == 'video_skipped'
    assert material['warnings'][-1]['code'] == 'video_attachment_skipped'
    material_file = tmp_path / 'material.json'
    json_artifact.write_json_atomic(material_file, material)
    verified = flow_engine._validate_material_bundle(material_file, expected_request_id='recorded-request',
        attachment_root=source.parent, prepared_root=tmp_path / 'prepared')
    assert verified['image_count'] == 0
    packet = tmp_path / 'review-packet.json'
    packet.write_text(json.dumps({'workflow_key': 'general_expense', 'material_file': str(material_file),
        'material_fingerprint': 'sha256:' + hashlib.sha256(material_file.read_bytes()).hexdigest()}))
    action = tmp_path / 'action.json'
    action.write_text(json.dumps({'workflow_key': 'general_expense', 'review_packet_file': str(packet)}))
    report = recognition.recognize(action_file=action,
        output_dir=tmp_path.parent / (tmp_path.name + '-recognition'))
    assert report['status'] == 'skipped'
    assert report['skipped_sources'][0]['resource_id'] == 'video-1'
    assert calls == []


@pytest.mark.parametrize('missing', [False, True])
def test_video_skip_preserves_other_preparation_failures(tmp_path, missing):
    source = tmp_path / ('missing.mp4' if missing else 'unsupported.bin')
    if not missing:
        source.write_bytes(b'unsupported')
    with pytest.raises(RuntimeError, match='^attachment_content_incomplete$'):
        extractor.prepare_materials([{'path': source, 'resource_id': 'file-1'}], out_dir=tmp_path / 'prepared')
