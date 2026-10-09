"""Exec image receipts cross the public archive-to-report seam with real PNG bytes."""

import base64
import json
from pathlib import Path

import pytest

from apps.oa_finance_preaudit import review_batch_report
from tests.finance_preaudit.test_review_batch_report import archive, digest, save


def image_part(path):
    return {
        "type": "input_image",
        "image_url": "data:image/png;base64," + base64.b64encode(path.read_bytes()).decode(),
    }


def exec_archive(tmp_path, *, change="valid", initial=False, direct=False):
    colors = (0, 0, 160, 255) if change == "ambiguous_pages" else (0, 80, 160, 255)
    root = archive(tmp_path, closed=False, image_colors=colors)
    manifest = json.loads((root / "manifest.json").read_text())
    material = json.loads(Path(manifest["action_file"]).with_name("material.json").read_text())
    pages = [Path(page["path"]) for page in material["prepared"]["images"]]
    parts = [image_part(page) for page in pages]
    call = {"type": "custom_tool_call", "name": "exec", "call_id": "image-call",
            "input": "const base='bound prepared directory'; const r=await Promise.allSettled([0,1,2,3].map(i=>tools.view_image({path:base+'/page-'+i+'.png'}))); for(const v of r){if(v.status==='fulfilled')image(v.value.image_url);}"}
    output = {"type": "custom_tool_call_output", "call_id": "image-call", "output": parts}
    payloads = [call, output]
    if change == "reverse_order":
        output["output"] = list(reversed(parts))
    elif change == "missing_output":
        payloads = [call]
    elif change == "missing_call":
        payloads = [output]
    elif change == "unrelated_call":
        call["name"] = "other-tool"
    elif change == "unrelated_image":
        import fitz
        unrelated = tmp_path / "unrelated.png"
        image = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 16, 16), False)
        image.clear_with(19)
        unrelated.write_bytes(image.tobytes("png"))
        parts[0] = image_part(unrelated)
    elif change == "duplicate_call":
        payloads = [call, call, output]
    elif change == "duplicate_output":
        payloads = [call, output, output]
    elif change == "duplicate_image":
        output["output"] = [parts[0], parts[0], *parts[1:]]
    elif change == "missing_image":
        output["output"] = parts[:-1]
    elif change == "wrong_call_id":
        output["call_id"] = "unrelated-call"
    elif change == "output_before_call":
        payloads = [output, call]
    elif change == "invalid_base64":
        parts[0] = {"type": "input_image", "image_url": "data:image/png;base64,!!!"}
    elif change == "invalid_mime":
        parts[0]["image_url"] = parts[0]["image_url"].replace("image/png", "text/plain")
    elif change == "invalid_output":
        output["output"] = "image bytes were not returned"
    if direct:
        payloads = []
        for index, page in enumerate(pages):
            payloads += [{"type": "function_call", "name": "view_image", "call_id": str(index),
                          "arguments": json.dumps({"path": str(page)})},
                         {"type": "function_call_output", "call_id": str(index), "output": [parts[index]]}]
    if initial:
        initial_payloads = [image_part(page) for page in pages]
        payloads.insert(0, {"type": "message", "role": "user", "content": initial_payloads})
        delivery = root / "input-delivery.json"
        save(delivery, {"delivery": {"initial_image_sha256": [digest(page) for page in pages]}})
        manifest["inputs"][str(delivery)] = digest(delivery)
        save(root / "manifest.json", manifest)
        save(root / "started.json", {"manifest_sha256": digest(root / "manifest.json")})
    trace = root / "reviewer/tool-receipts/session.jsonl"
    trace.parent.mkdir(mode=0o700)
    trace.write_text("\n".join(json.dumps({"type": "response_item", "payload": p}) for p in payloads) + "\n")
    trace.chmod(0o600)
    result = json.loads((root / "result.json").read_text())
    result["output_files"] = {str(trace): digest(trace)}
    save(root / "result.json", result)
    return root


@pytest.mark.parametrize("change", ["valid", "reverse_order"])
def test_exec_returned_images_are_bound_to_each_original_by_content(tmp_path, change):
    root = exec_archive(tmp_path, change=change)
    originals = {p: p.read_bytes() for p in root.rglob("*") if p.is_file()}
    report = review_batch_report.build_report(root)
    images = report["acceptance"]["original_images"]
    assert images["status"] == "verified"
    assert images["expected"] == images["covered"] == 4
    assert images["view_returned"] == 4
    assert images["observation"] == "instrumented_view_receipts"
    assert report["interactions"]["tool_events"] == {"status": "observed", "count": 0}
    assert all(p.read_bytes() == raw for p, raw in originals.items())


@pytest.mark.parametrize("change", [
    "missing_output", "missing_call", "unrelated_call", "unrelated_image",
    "duplicate_call", "duplicate_output", "duplicate_image", "missing_image",
    "wrong_call_id", "output_before_call", "invalid_base64", "invalid_mime",
    "invalid_output", "ambiguous_pages",
])
def test_exec_receipts_do_not_infer_missing_or_ambiguous_image_coverage(tmp_path, change):
    images = review_batch_report.build_report(exec_archive(tmp_path, change=change))["acceptance"]["original_images"]
    assert images["status"] == "incomplete"
    assert images["covered"] < images["expected"] == 4


def test_complete_initial_delivery_preserves_existing_projection_with_exec_review(tmp_path):
    images = review_batch_report.build_report(exec_archive(tmp_path, initial=True))["acceptance"]["original_images"]
    assert images == {
        "status": "verified", "expected": 4, "covered": 4, "initial_delivered": 4,
        "initial_payloads_observed": 4, "view_returned": 0, "unique_images": 4,
        "observation": "not_established_initial_delivery_only",
    }


def test_direct_view_image_receipts_keep_the_existing_projection(tmp_path):
    images = review_batch_report.build_report(exec_archive(tmp_path, direct=True))["acceptance"]["original_images"]
    assert images == {
        "status": "verified", "expected": 4, "covered": 4, "initial_delivered": 0,
        "initial_payloads_observed": 0, "view_returned": 4, "unique_images": 4,
        "observation": "instrumented_view_receipts",
    }
