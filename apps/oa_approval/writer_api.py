from __future__ import annotations

import html
import time
import urllib.parse
import urllib.request
from typing import Any

from apps.oa_approval import auth
from apps.oa_approval.api import OaApi, TIMEOUT, _decode_json


SUBMIT_ENDPOINT = "/api/workflow/paService/submitRequest"
POSTCHECK_ATTEMPTS = 3
POSTCHECK_INTERVAL_SECONDS = 0.25


def _format_remark(remark: str) -> str:
    normalized = str(remark or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    return "<br/>".join(
        html.escape(line.strip(), quote=False) if line.strip() else "&nbsp;"
        for line in normalized.split("\n")
    )


class FrozenWriterApi(OaApi):
    def _post_submit(self, request_id: str, remark: str) -> dict[str, Any]:
        request = urllib.request.Request(
            f"{self.base_url}{SUBMIT_ENDPOINT}",
            data=urllib.parse.urlencode(
                {
                    "requestId": int(str(request_id)),
                    "remark": _format_remark(remark),
                }
            ).encode("utf-8"),
            headers={
                **self._headers(),
                "content-type": "application/x-www-form-urlencoded",
            },
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            return _decode_json(response.read().decode("utf-8", errors="ignore"))

    def submit_and_verify(self, request_id: str, remark: str) -> dict[str, Any]:
        try:
            response = self._post_submit(request_id, remark)
        except Exception:
            return {
                "submit_ok": False,
                "done_hit": False,
                "postcheck_done_hit": False,
                "failure_code": "writer_transport_unknown",
            }
        submit_ok = str(response.get("code") or "").strip().upper() == "SUCCESS"
        if not submit_ok:
            return {
                "submit_ok": False,
                "done_hit": False,
                "postcheck_done_hit": False,
                "failure_code": "writer_submit_rejected",
            }
        target = str(request_id)
        for attempt in range(POSTCHECK_ATTEMPTS):
            try:
                done_hit = any(
                    row.get("request_id") == target for row in self.handled_rows()
                )
                pending_hit = any(
                    row.get("request_id") == target for row in self.pending_rows()
                )
            except Exception:
                break
            if done_hit and pending_hit:
                return {
                    "submit_ok": True,
                    "done_hit": True,
                    "postcheck_done_hit": False,
                    "failure_code": "writer_pending_conflict",
                }
            if done_hit:
                return {
                    "submit_ok": True,
                    "done_hit": True,
                    "postcheck_done_hit": True,
                    "failure_code": None,
                }
            if attempt + 1 < POSTCHECK_ATTEMPTS:
                time.sleep(POSTCHECK_INTERVAL_SECONDS)
        return {
            "submit_ok": True,
            "done_hit": False,
            "postcheck_done_hit": False,
            "failure_code": "writer_postcheck_timeout",
        }


def build_writer_api() -> FrozenWriterApi:
    token = auth.get_token()
    oa_uid = auth.env("OA_UID")
    return FrozenWriterApi(token, auth.encrypt_user_id(oa_uid))
