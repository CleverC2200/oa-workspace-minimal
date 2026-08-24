from __future__ import annotations

import html
import time
import urllib.parse
import urllib.request
from typing import Any

from apps.oa_approval import auth
from apps.oa_approval.api import OaApi, TIMEOUT, _decode_json
from apps.oa_approval import writer_outcomes


SUBMIT_ENDPOINT = "/api/workflow/paService/submitRequest"
POSTCHECK_ATTEMPTS = 3
POSTCHECK_INTERVAL_SECONDS = 0.25
SUBMIT_REJECTION_CODES = frozenset({"ERROR"})


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
                "failure_code": writer_outcomes.TRANSPORT_UNKNOWN,
            }
        response_code = str(response.get("code") or "").strip().upper()
        submit_ok = response_code == "SUCCESS"
        if response_code in SUBMIT_REJECTION_CODES:
            return {
                "submit_ok": False,
                "done_hit": False,
                "postcheck_done_hit": False,
                "failure_code": writer_outcomes.SUBMIT_REJECTED,
            }
        if not submit_ok:
            return {
                "submit_ok": False,
                "done_hit": False,
                "postcheck_done_hit": False,
                "failure_code": writer_outcomes.TRANSPORT_UNKNOWN,
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
                    "failure_code": writer_outcomes.PENDING_CONFLICT,
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
            "failure_code": writer_outcomes.POSTCHECK_TIMEOUT,
        }


def build_writer_api() -> FrozenWriterApi:
    token = auth.get_token()
    oa_uid = auth.env("OA_UID")
    return FrozenWriterApi(token, auth.encrypt_user_id(oa_uid))
