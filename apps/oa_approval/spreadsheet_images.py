"""Read referenced Excel pictures and their sheet anchors; never infer business ownership."""
from __future__ import annotations

import posixpath
import re
from pathlib import Path
import xml.etree.ElementTree as ET
import zipfile

import fitz

_NS = {
    "s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    "xdr": "http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing",
}


def extract(path: Path, out_dir: Path, prefix: str, *, compact: bool = False) -> list[tuple[Path, str]]:
    from openpyxl.utils import get_column_letter

    output = []
    total_bytes = 0
    with zipfile.ZipFile(path) as archive:
        infos = archive.infolist()
        if (len(infos) > 10000 or sum(info.file_size for info in infos) > 300_000_000
                or any(info.file_size > (20_000_000 if info.filename.endswith('.xml') else 50_000_000) for info in infos)):
            raise ValueError("spreadsheet_image_limit")
        names = set(archive.namelist())
        if len(names) != len(infos):
            raise ValueError("spreadsheet_duplicate_members")

        def read(member: str, limit: int = 20_000_000) -> bytes:
            nonlocal total_bytes
            info = archive.getinfo(member)
            if info.file_size > limit or total_bytes + info.file_size > 300_000_000:
                raise ValueError("spreadsheet_image_limit")
            total_bytes += info.file_size
            return archive.read(member)

        def xml(member: str):
            return ET.fromstring(read(member))

        def relations(member: str) -> dict[str, str]:
            base, name = posixpath.split(member)
            relfile = f"{base}/_rels/{name}.rels"
            if relfile not in names:
                return {}
            result = {}
            for rel in xml(relfile):
                if rel.get("TargetMode") == "External":
                    continue
                target = rel.get("Target", "")
                target = posixpath.normpath(target.lstrip('/') if target.startswith('/') else posixpath.join(base, target))
                if not target.startswith('xl/') or '\\' in target:
                    raise ValueError("spreadsheet_relationship_untrusted")
                key = rel.get("Id", "")
                if key in result:
                    raise ValueError("spreadsheet_relationship_ambiguous")
                result[key] = target
            return result

        def emit(member: str, cell: str) -> None:
            if len(output) >= 1000:
                raise ValueError("spreadsheet_image_limit")
            suffix = Path(member).suffix.lower()
            if suffix not in {'.png', '.jpg', '.jpeg', '.bmp', '.gif', '.tif', '.tiff', '.webp'}:
                raise ValueError("spreadsheet_image_unsupported")
            content = read(member, 50_000_000)
            with fitz.open(stream=content) as image:
                if image.is_pdf or image.page_count != 1 or image[0].rect.get_area() > 100_000_000:
                    raise ValueError("spreadsheet_image_unreadable")
                image[0].get_pixmap()
            if compact and suffix == '.png' and len(content) > 256 * 1024:
                pixels = fitz.Pixmap(content)
                if not pixels.alpha and pixels.colorspace.n == 3:
                    jpeg = pixels.tobytes('jpeg', jpg_quality=95)
                    if len(jpeg) < len(content):
                        content, suffix = jpeg, '.jpg'
            destination = out_dir / f'{prefix}_excel_{len(output)+1}{suffix}'
            destination.write_bytes(content)
            output.append((destination, cell))

        cell_images = {}
        if 'xl/cellimages.xml' in names:
            image_rels = relations('xl/cellimages.xml')
            for pic in xml('xl/cellimages.xml').findall('.//xdr:pic', _NS):
                prop = pic.find('xdr:nvPicPr/xdr:cNvPr', _NS)
                blip = pic.find('.//a:blip', _NS)
                if prop is None or blip is None:
                    raise ValueError("spreadsheet_cell_image_incomplete")
                key = prop.get('name')
                if key in cell_images:
                    raise ValueError("spreadsheet_cell_image_ambiguous")
                cell_images[key] = image_rels[blip.get(f'{{{_NS["r"]}}}embed')]
        workbook_rels = relations('xl/workbook.xml')
        for sheet in xml('xl/workbook.xml').findall('s:sheets/s:sheet', _NS):
            sheet_path = workbook_rels[sheet.get(f'{{{_NS["r"]}}}id')]
            document = xml(sheet_path)
            shared = {
                f.get('si'): f.text for f in document.findall('.//s:f', _NS)
                if f.get('t') == 'shared' and f.text
            }
            for cell in document.findall('.//s:sheetData/s:row/s:c', _NS):
                formula = cell.find('s:f', _NS)
                if formula is None:
                    continue
                text = formula.text or ''
                if not text and formula.get('t') == 'shared':
                    text = shared[formula.get('si')]
                if 'DISPIMG' not in text:
                    continue
                match = re.search(r'DISPIMG\(\s*"([^"]+)"', text)
                if match is None or match[1] not in cell_images:
                    raise ValueError("spreadsheet_cell_image_unresolved")
                emit(cell_images[match[1]], f'{sheet.get("name")}!{cell.get("r")}')
            sheet_rels = relations(sheet_path)
            for drawing in document.findall('s:drawing', _NS):
                drawing_path = sheet_rels[drawing.get(f'{{{_NS["r"]}}}id')]
                drawing_rels = relations(drawing_path)
                for anchor in xml(drawing_path):
                    for blip in anchor.findall('.//a:blip', _NS):
                        member = drawing_rels[blip.get(f'{{{_NS["r"]}}}embed')]
                        origin = anchor.find('xdr:from', _NS)
                        location = 'absolute-anchor'
                        if origin is not None:
                            col = int(origin.findtext('xdr:col', namespaces=_NS))
                            row = int(origin.findtext('xdr:row', namespaces=_NS))
                            location = f'{get_column_letter(col+1)}{row+1}'
                        emit(member, f'{sheet.get("name")}!{location}')
    return output
