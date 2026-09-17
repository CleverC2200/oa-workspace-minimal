import io
import zipfile
from pathlib import Path
import openpyxl
import fitz
import pytest
from apps.oa_approval import attachment_extractors


def book(tmp_path: Path, *, broken=False):
    path = tmp_path / 'photos.xlsx'
    image = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 8, 8), False)
    image.clear_with(255)
    png = io.BytesIO(image.tobytes('png'))
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = '现场'
    ws['A1'] = '项目'
    ws['B2'] = '=DISPIMG("ID_TEST",1)'
    wb.save(path)
    with zipfile.ZipFile(path) as z:
        parts = {name: z.read(name) for name in z.namelist()}
    parts['xl/worksheets/sheet1.xml'] = parts['xl/worksheets/sheet1.xml'].replace(b'</worksheet>', b'<drawing xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" r:id="rId1"/></worksheet>')
    with zipfile.ZipFile(path, 'w') as z:
        for name, content in parts.items():
            z.writestr(name, content)
        z.writestr('xl/worksheets/_rels/sheet1.xml.rels', '<Relationships><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/drawing" Target="../drawings/drawing1.xml"/></Relationships>')
        z.writestr('xl/drawings/drawing1.xml', '<xdr:wsDr xmlns:xdr="http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing" xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><xdr:oneCellAnchor><xdr:from><xdr:col>2</xdr:col><xdr:row>2</xdr:row></xdr:from><xdr:pic><a:blip r:embed="rId1"/></xdr:pic></xdr:oneCellAnchor></xdr:wsDr>')
        z.writestr('xl/drawings/_rels/drawing1.xml.rels', '<Relationships><Relationship Id="rId1" Target="../media/floating.png"/></Relationships>')
        z.writestr('xl/media/floating.png', png.getvalue())
        z.writestr('xl/cellimages.xml', '<root xmlns:xdr="http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing" xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><cellImage><xdr:pic><xdr:nvPicPr><xdr:cNvPr name="ID_TEST"/></xdr:nvPicPr><xdr:blipFill><a:blip r:embed="rId1"/></xdr:blipFill></xdr:pic></cellImage></root>')
        z.writestr('xl/_rels/cellimages.xml.rels', '<Relationships><Relationship Id="rId1" Target="media/cell.png"/></Relationships>')
        if not broken:
            z.writestr('xl/media/cell.png', png.getvalue())
    return path


def test_preparation_keeps_wps_cell_and_floating_images(tmp_path):
    path = book(tmp_path)
    images, manifest, facts = attachment_extractors.prepare_materials([{'resource_id':'book', 'path':str(path)}], out_dir=tmp_path/'prepared')
    assert len(images) == 2
    assert {image['spreadsheet_cell'] for image in images} == {'现场!B2', '现场!C3'}
    assert all(image['resource_id']=='book' and Path(image['path']).is_file() for image in images)
    assert manifest[0]['image_count'] == 2
    assert any('B2' in text for text in facts[0]['key_text'])


def test_broken_image_relation_is_not_reported_as_complete(tmp_path):
    path = book(tmp_path, broken=True)
    with pytest.raises(RuntimeError, match='attachment_content_incomplete'):
        attachment_extractors.prepare_materials([{'resource_id':'book', 'path':str(path)}], out_dir=tmp_path/'prepared')


def rewrite_part(path, member, transform):
    with zipfile.ZipFile(path) as z:
        parts = {name: z.read(name) for name in z.namelist()}
    parts[member] = transform(parts[member])
    with zipfile.ZipFile(path, 'w') as z:
        for name, content in parts.items():
            z.writestr(name, content)


def test_shared_formula_preserves_each_cell_anchor(tmp_path):
    path = book(tmp_path)
    rewrite_part(path, 'xl/worksheets/sheet1.xml', lambda b: b.replace(b'<f>DISPIMG', b'<f t="shared" si="0" ref="B2:C2">DISPIMG').replace(b'</row></sheetData>', b'<c r="C2"><f t="shared" si="0"/></c></row></sheetData>'))
    images, _, _ = attachment_extractors.prepare_materials([{'resource_id':'book', 'path':str(path)}], out_dir=tmp_path/'prepared')
    assert {image['spreadsheet_cell'] for image in images} == {'现场!B2', '现场!C2', '现场!C3'}


def test_corrupt_image_is_not_successfully_prepared(tmp_path):
    path = book(tmp_path)
    rewrite_part(path, 'xl/media/cell.png', lambda _: b'not a PNG')
    with pytest.raises(RuntimeError, match='attachment_content_incomplete'):
        attachment_extractors.prepare_materials([{'resource_id':'book', 'path':str(path)}], out_dir=tmp_path/'prepared')


def test_image_relationship_cannot_escape_workbook(tmp_path):
    path = book(tmp_path)
    rewrite_part(path, 'xl/_rels/cellimages.xml.rels', lambda b: b.replace(b'media/cell.png', b'../../outside.png'))
    with pytest.raises(RuntimeError, match='attachment_content_incomplete'):
        attachment_extractors.prepare_materials([{'resource_id':'book', 'path':str(path)}], out_dir=tmp_path/'prepared')
