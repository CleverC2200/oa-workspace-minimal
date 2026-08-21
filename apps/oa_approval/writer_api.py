from __future__ import annotations

import html
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from apps.oa_approval import auth
from apps.oa_approval.api import OaApi, TIMEOUT, _decode_json


SUBMIT_ENDPOINT = "/api/workflow/paService/submitRequest"


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
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
                return _decode_json(response.read().decode("utf-8", errors="ignore"))
        except urllib.error.HTTPError as error:
            return {
                "http_status": error.code,
                "error": str(error),
            }

    def submit_and_verify(self, request_id: str, remark: str) -> dict[str, bool]:
        response = self._post_submit(request_id, remark)
        submit_ok = str(response.get("code") or "").strip().upper() == "SUCCESS"
        if not submit_ok:
            return {
                "submit_ok": False,
                "done_hit": False,
                "postcheck_done_hit": False,
            }
        try:
            target = str(request_id)
            done_hit = any(
                row.get("request_id") == target for row in self.handled_rows()
            )
            pending_hit = any(
                row.get("request_id") == target for row in self.pending_rows()
            )
        except Exception:
            done_hit = False
            pending_hit = True
        return {
            "submit_ok": True,
            "done_hit": done_hit,
            "postcheck_done_hit": done_hit and not pending_hit,
        }


def build_writer_api() -> FrozenWriterApi:
    token = auth.get_token()
    oa_uid = auth.env("OA_UID")
    return FrozenWriterApi(token, auth.encrypt_user_id(oa_uid))
