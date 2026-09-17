import pytest
from apps.oa_approval import attachments
from apps.oa_approval import evidence_packet


def workflow(html):
    return {"workflowDetailTableInfos": [{"workflowRequestTableRecords": [{"workflowRequestTableFields": [{"fieldShowName": "附件", "filedHtmlShow": html}]}]}]}


def test_html_only_attachments_are_collected_with_source_ownership():
    data = workflow('''<span onclick="toURL('/news/download.do?fileid=101&filename=现场.jpeg',false);">照片</span>''')
    data["workflowRequestLogs"] = [{"annexDocHtmls": '''<span onclick="toURL('/news/download.do?fileid=102&filename=清单.xlsx',false);">清单</span>'''}]
    rows = evidence_packet.logical_attachment_resources(data, [])
    assert [r["id"] for r in rows] == ["101", "102"]
    assert rows[0]["downloadUrl"] == "/weaver/weaver.file.FileDownload?fileid=101"
    assert rows[0]["_trusted_owner_detail_id"] == "detail[0].row[0]"
    assert not rows[1].get("_trusted_owner_detail_id")
    assert rows[1]["_trusted_source_field_path"] == "workflowRequestLogs[0].annexDocHtmls"


def test_html_discovery_deduplicates_ids_without_guessing_row_ownership():
    data = workflow("<a href='/news/download.do?fileid=101&amp;filename=a.jpeg'>a</a>")
    data["workflowDetailTableInfos"][0]["workflowRequestTableRecords"] *= 2
    rows = evidence_packet.logical_attachment_resources(data, [])
    assert len(rows) == 1
    assert not rows[0].get("_trusted_owner_detail_id")
    listed = [{"id": "101", "name": "a.jpeg", "downloadUrl": "/download/101"}]
    assert len(evidence_packet.logical_attachment_resources(data, listed)) == 1


def test_html_discovery_ignores_external_and_non_download_links():
    data = workflow("""<a href='https://external.invalid/news/download.do?fileid=101&filename=a.jpeg'>bad</a>
    <a href='/news/download.do?fileid=102&fileid=103&filename=a.jpeg'>ambiguous</a>
    <span title='/news/download.do?fileid=104&filename=a.jpeg'>plain metadata</span>
    <a href='javascript:alert(1)'>bad</a>""")
    assert evidence_packet.logical_attachment_resources(data, []) == []


def test_rejected_field_link_cannot_assign_log_attachment_to_detail():
    data = workflow("<a href='https://external.invalid/news/download.do?fileid=101&filename=a.xlsx'>bad</a>")
    data['workflowRequestLogs'] = [{'annexDocHtmls': "<a href='/news/download.do?fileid=101&filename=a.xlsx'>good</a>"}]
    rows = evidence_packet.logical_attachment_resources(data, [])
    assert len(rows) == 1
    assert not rows[0].get('_trusted_owner_detail_id')
    assert rows[0]['_trusted_source_field_path'] == 'workflowRequestLogs[0].annexDocHtmls'


@pytest.mark.parametrize("url", [
    "ftp://external.invalid/news/download.do?fileid=101&filename=a.xlsx",
    "HTTPS://external.invalid/news/download.do?fileid=101&filename=a.xlsx",
    "javascript:/news/download.do?fileid=101&filename=a.xlsx",
    "https://oa.example.com:444/news/download.do?fileid=101&filename=a.xlsx",
])
def test_raw_origin_is_validated_before_native_url_conversion(monkeypatch, url):
    monkeypatch.setattr(attachments, 'OA_HOST', 'https://oa.example.com')
    data = workflow(f"<a href='{url}'>bad</a>")
    assert evidence_packet.logical_attachment_resources(data, []) == []


def test_same_origin_absolute_link_and_fileid_only_are_kept(monkeypatch):
    monkeypatch.setattr(attachments, 'OA_HOST', 'https://oa.example.com')
    data = workflow("<a href='HTTPS://oa.example.com/news/download.do?fileid=101'>attachment</a>")
    rows = evidence_packet.logical_attachment_resources(data, [])
    assert rows[0]['downloadUrl'] == '/weaver/weaver.file.FileDownload?fileid=101'
    assert rows[0]['name'] == 'attachment-101.bin'
