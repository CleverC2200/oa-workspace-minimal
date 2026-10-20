import hashlib
from pathlib import Path

from PIL import Image
import pytest

from apps import json_artifact
from apps.oa_approval.attachment_extractors import prepare_materials
from apps.oa_approval import attachment_recognition
from apps.oa_finance_preaudit import case_bundle, flow_engine
from tests.finance_preaudit.test_attachment_recognition import fake_api
from tests.finance_preaudit.test_collect_case_contract import FLOW_CONFIGS, RecordedOaApi


@pytest.mark.parametrize('image_format,suffix', [('PNG', '.png'), ('JPEG', '.jpg')])
def test_public_preparation_preserves_suffixless_image(tmp_path, image_format, suffix):
    source = tmp_path / 'payment-evidence'
    Image.new('RGB', (8, 8), 'white').save(source, format=image_format)
    original = source.read_bytes()
    images, manifest, contents = prepare_materials(
        [{'path': str(source), 'resource_id': 'photo', 'name': source.name}],
        out_dir=tmp_path / 'prepared',
    )
    assert len(images) == 1
    image = images[0]
    prepared = Path(image['path'])
    assert prepared.parent == tmp_path / 'prepared'
    assert prepared.suffix == suffix
    assert prepared.read_bytes() == source.read_bytes() == original
    assert image['resource_id'] == 'photo'
    assert image['name'] == source.name
    assert image['source_content_sha256'] == image['content_sha256'] == hashlib.sha256(original).hexdigest()
    assert manifest[0]['status'] == 'image'
    assert manifest[0]['image_count'] == 1
    assert contents == []


@pytest.mark.parametrize('payload', [b'unsupported', b'\x89PNG\r\n\x1a\ntruncated'])
def test_public_preparation_rejects_unreadable_suffixless_content(tmp_path, payload):
    source = tmp_path / 'attachment'
    source.write_bytes(payload)
    with pytest.raises(RuntimeError, match='^attachment_content_incomplete$'):
        prepare_materials([{'path': str(source), 'resource_id': 'photo'}], out_dir=tmp_path / 'prepared')
    assert source.read_bytes() == payload


def test_public_preparation_keeps_named_image_path(tmp_path):
    source = tmp_path / 'photo.png'
    Image.new('RGB', (8, 8), 'white').save(source)
    images, _, _ = prepare_materials([{'path': str(source), 'resource_id': 'photo'}], out_dir=tmp_path / 'prepared')
    assert Path(images[0]['path']) == source


def test_collection_freeze_and_reader_cover_suffixless_image(tmp_path, monkeypatch):
    calls = fake_api(monkeypatch)
    source = tmp_path / 'attachments' / 'payment-evidence'
    source.parent.mkdir()
    Image.new('RGB', (8, 8), 'white').save(source, format='PNG')
    monkeypatch.setattr(case_bundle, '_travel_resources', lambda *_a, **_k: ([], []))
    monkeypatch.setattr(case_bundle.evidence_packet, 'logical_attachment_resources',
                        lambda *_: [{'id': 'photo', 'name': source.name}])
    monkeypatch.setattr(case_bundle.attachments, 'download_resources', lambda *_a, **_k: (
        [{'path': str(source), 'resource_id': 'photo', 'name': source.name}], []))
    monkeypatch.setattr(case_bundle.invoice_buyers, 'read_invoice_buyers', lambda *_a, **_k: {
        'read_status': 'not_applicable', 'invoices': []})
    material = case_bundle.collect_case('recorded-request',
        oa_api=RecordedOaApi('195021', '通用费用报销申请流程'), configs=FLOW_CONFIGS,
        allow_browser_download=False, attachment_root=source.parent, prepared_root=tmp_path / 'prepared')
    material_file = tmp_path / 'material.json'
    json_artifact.write_json_atomic(material_file, material)
    verified = flow_engine._validate_material_bundle(material_file, expected_request_id='recorded-request',
        attachment_root=source.parent, prepared_root=tmp_path / 'prepared')
    assert verified['image_count'] == 1
    prepared = material['prepared']['images'][0]
    assert prepared['resource_id'] == 'photo'
    assert prepared['name'] == source.name
    report = attachment_recognition.recognize(files=[Path(prepared['path'])],
        output_dir=tmp_path / 'recognition')
    assert report['status'] == 'complete'
    assert len(report['parts']) == len(calls) == 1
