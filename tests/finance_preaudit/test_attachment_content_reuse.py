from __future__ import annotations

from collections import Counter
import io
from pathlib import Path
from threading import Lock
import zipfile

import openpyxl
from openpyxl.drawing.image import Image as WorkbookImage
from PIL import Image
import pytest

from apps.oa_approval import attachment_cache, attachment_extractors, attachments, spreadsheet_images
from apps.oa_finance_preaudit import case_bundle


@pytest.fixture
def png_bytes():
    picture = io.BytesIO()
    Image.new('RGB', (2, 2), 'red').save(picture, format='PNG')
    return picture.getvalue()


@pytest.fixture
def workbook_bytes(png_bytes):
    workbook = openpyxl.Workbook()
    workbook.active['A1'] = 'recorded amount'
    workbook.active.add_image(WorkbookImage(io.BytesIO(png_bytes)), 'B2')
    output = io.BytesIO()
    workbook.save(output)
    workbook.close()
    return output.getvalue()


@pytest.fixture
def recorded_download(tmp_path, monkeypatch, workbook_bytes):
    calls = []
    lock = Lock()
    monkeypatch.setenv('OA_FINANCE_PREAUDIT_CACHE_DB', str(tmp_path/'cache.sqlite3'))
    monkeypatch.setattr(attachments, 'OA_HOST', 'https://oa.example.test')

    def download(resource, *, dest_dir, **_kwargs):
        path = dest_dir / attachments.deterministic_filename(resource)
        path.write_bytes(workbook_bytes + (b'changed' if resource['id'] == 'different' else b''))
        with lock:
            calls.append(resource['id'])
        return path, ''

    monkeypatch.setattr(attachments, '_download_resource_direct', download)
    return calls


def resources(ids):
    return [{'id':rid, 'name':'recorded.xlsx', 'downloadUrl':f'/download?fileid={rid}'} for rid in ids]


@pytest.mark.parametrize('browser_fallback', [False, True])
def test_download_shares_identical_content_after_reading_every_resource(
    tmp_path, monkeypatch, recorded_download, browser_fallback,
):
    if browser_fallback:
        download = attachments._download_resource_direct
        monkeypatch.setattr(attachments, '_download_resource_direct', lambda *_a, **_k:(None, ''))
        monkeypatch.setattr(attachments, '_download_resource_with_browser', download)
    files, errors = attachments.download_resources(
        resources(['first', 'second', 'different']), dest_dir=tmp_path/'downloads',
        timeout_sec=1, allow_browser_download=True, request_id='recorded', flow_key='tpm_small_marketing_fee',
    )
    assert errors == []
    assert Counter(recorded_download) == Counter(['first', 'second', 'different'])
    assert [item['resource_id'] for item in files] == ['first', 'second', 'different']
    assert files[0]['path'] == files[1]['path']
    assert files[2]['path'] != files[0]['path']
    assert len(list((tmp_path/'downloads').glob('*.xlsx'))) == 2
    assert all(Path(item['path']).is_file() for item in files)


def test_cached_content_reuse_preserves_files_from_prior_attempts(tmp_path, recorded_download, workbook_bytes):
    previous = tmp_path/'previous'
    previous.mkdir()
    originals = [previous/f'{rid}.xlsx' for rid in ('first', 'second')]
    for resource, path in zip(resources(['first', 'second']), originals):
        path.write_bytes(workbook_bytes)
        attachment_cache.record_attachment_file(
            request_id='recorded', flow_key='tpm_small_marketing_fee', resource_key=resource['id'],
            entry_json={'path':str(path), 'resource_id':resource['id'], 'name':resource['name']},
        )
    files, errors = attachments.download_resources(
        resources(['first', 'second']), dest_dir=tmp_path/'next', timeout_sec=1,
        allow_browser_download=True, request_id='recorded', flow_key='tpm_small_marketing_fee',
    )
    assert errors == []
    assert recorded_download == []
    assert all(path.read_bytes() == workbook_bytes for path in originals)
    assert [item['resource_id'] for item in files] == ['first', 'second']
    assert all(item['reused'] is True for item in files)


def test_preparation_reuses_content_and_rebinds_each_resource(tmp_path, monkeypatch, workbook_bytes):
    items = []
    for rid, parent in [('first', 'archive-a'), ('second', 'archive-b')]:
        path = tmp_path/f'{rid}.xlsx'
        path.write_bytes(workbook_bytes)
        items.append({'resource_id':rid, 'parent_resource_id':parent, 'name':f'{rid}.xlsx', 'path':str(path)})
    calls = []
    extract = spreadsheet_images.extract

    def recorded_extract(*args, **kwargs):
        calls.append(args[0])
        return extract(*args, **kwargs)

    monkeypatch.setattr(spreadsheet_images, 'extract', recorded_extract)
    images, manifest, facts = attachment_extractors.prepare_materials(items, out_dir=tmp_path/'prepared')
    assert len(calls) == 1
    assert [(i['resource_id'], i['parent_resource_id'], i['name']) for i in images] == [
        ('first', 'archive-a', 'first.xlsx'), ('second', 'archive-b', 'second.xlsx'),
    ]
    assert len({str(i['path']) for i in images}) == 1
    assert len(list((tmp_path/'prepared').glob('*.png'))) == 1
    assert [i['resource_id'] for i in manifest] == ['first', 'second']
    assert [i['resource_id'] for i in facts] == ['first', 'second']
    assert all(i['spreadsheet_cell'] == 'Sheet!B2' for i in images)


def test_collection_preserves_67_resource_bindings_with_one_workbook_and_image(
    tmp_path, monkeypatch, recorded_download,
):
    ids = [str(1000 + index) for index in range(67)]

    class RecordedOaApi:
        def request_context(self, _request_id):
            rows = []
            position = 0
            for index in range(30):
                bound = ids[position:position + (3 if index < 7 else 2)]
                position += len(bound)
                rows.append({'workflowRequestTableFields':[{
                    'fieldName':'fj', 'fieldShowName':'附件', 'fieldValue':','.join(bound),
                    'filedHtmlShow':''.join(
                        f'<a href="/news/download.do?fileid={rid}&filename=recorded.xlsx">recorded.xlsx</a>'
                        for rid in bound
                    ),
                }]})
            return {
                'workflowBaseInfo':{'workflowId':'195039', 'workflowName':'TPM小额市场费用报销申请流程'},
                'workflowDetailTableInfos':[{'workflowRequestTableRecords':rows}],
            }, resources(ids)

    monkeypatch.setattr(case_bundle.invoice_buyers, 'read_invoice_buyers', lambda *_a, **_k:{
        'read_status':'not_applicable', 'invoices':[],
    })
    material = case_bundle.collect_case(
        'recorded', oa_api=RecordedOaApi(), configs=[{
            'flow_key':'tpm_small_marketing_fee', 'workflow_id':'195039',
            'workflow_name':'TPM小额市场费用报销申请流程',
        }], allow_browser_download=True, attachment_root=tmp_path/'downloads', prepared_root=tmp_path/'prepared',
    )
    assert Counter(recorded_download) == Counter(ids)
    assert [r['resource_id'] for r in material['resources']] == ids
    assert Counter(r['owner_detail_id'] for r in material['resources']) == {
        f'detail[0].row[{i}]':3 if i < 7 else 2 for i in range(30)
    }
    assert len({r['local_readonly_path'] for r in material['resources']}) == 1
    assert len(list((tmp_path/'downloads').rglob('*.xlsx'))) == 1
    prepared = material['prepared']
    assert [i['resource_id'] for i in prepared['images']] == ids
    assert len({str(i['path']) for i in prepared['images']}) == 1
    assert len(list((tmp_path/'prepared').rglob('*.png'))) == 1
    assert [i['resource_id'] for i in prepared['manifest']] == ids
    assert [i['resource_id'] for i in prepared['structured_contents']] == ids


def test_identical_docx_files_keep_their_own_media_identities(tmp_path, png_bytes):
    content = io.BytesIO()
    with zipfile.ZipFile(content, 'w') as archive:
        archive.writestr('word/document.xml', '<document><body/></document>')
        archive.writestr('word/media/image1.png', png_bytes)
        archive.writestr('word/media/image2.png', png_bytes)
    items = []
    for rid in ('first', 'second'):
        path = tmp_path/f'{rid}.docx'
        path.write_bytes(content.getvalue())
        items.append({'resource_id':rid, 'name':path.name, 'path':str(path)})
    images, _, _ = attachment_extractors.prepare_materials(items, out_dir=tmp_path/'prepared')
    assert [(i['resource_id'], i['parent_resource_id']) for i in images] == [
        (f'{rid}::docx_media::{index}', rid) for rid in ('first', 'second') for index in (1, 2)
    ]
    assert all(i['name'].startswith(f"{i['parent_resource_id']}.docx::word/media/") for i in images)


def test_identical_archive_videos_keep_parent_in_skip_records(tmp_path):
    children = []
    for index in (1, 2):
        path = tmp_path/f'{index}.mp4'
        path.write_bytes(b'recorded unread video')
        children.append({'resource_id':f'archive::{index}', 'parent_resource_id':'archive', 'path':str(path)})
    images, manifest, _ = attachment_extractors.prepare_materials(
        [{'resource_id':'archive', 'extracted_files':children}], out_dir=tmp_path/'prepared',
    )
    assert images == []
    assert [(i['resource_id'], i['parent_resource_id']) for i in manifest[0]['skipped_videos']] == [
        ('archive::1', 'archive'), ('archive::2', 'archive'),
    ]


def test_identical_archive_image_stays_inside_prepared_directory(tmp_path, png_bytes):
    standalone = tmp_path/'standalone.png'
    child = tmp_path/'child.png'
    standalone.write_bytes(png_bytes)
    child.write_bytes(png_bytes)
    prepared = tmp_path/'prepared'
    images, _, _ = attachment_extractors.prepare_materials([
        {'resource_id':'standalone', 'path':str(standalone)},
        {'resource_id':'archive', 'extracted_files':[
            {'resource_id':'archive::1', 'parent_resource_id':'archive', 'path':str(child)},
        ]},
    ], out_dir=prepared)
    assert images[0]['path'] == standalone
    assert Path(images[1]['path']).is_relative_to(prepared)
    assert images[1]['parent_resource_id'] == 'archive'
    assert Path(images[1]['path']).read_bytes() == png_bytes
