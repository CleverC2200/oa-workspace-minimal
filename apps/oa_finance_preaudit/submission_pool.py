from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import re
from typing import Callable, Iterable


_REQUEST_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_UNKNOWN_CODES = {
    "writer_result_unknown",
    "writer_receipt_unreadable",
    "writer_receipt_mismatch",
    "write_state_disputed",
}
_FAILURE_CODE = re.compile(r"^[a-z][a-z0-9_]{2,80}$")


def _request_id(job: object) -> str:
    if not isinstance(job, dict):
        raise ValueError("submission_job_invalid")
    request_id = str(job.get("request_id") or "")
    if not _REQUEST_ID.fullmatch(request_id):
        raise ValueError("submission_job_invalid")
    return request_id


def run_bounded(
    jobs: Iterable[dict],
    submit_one: Callable[[dict], dict],
    *,
    max_workers: int,
) -> list[dict]:
    if isinstance(max_workers, bool) or not isinstance(max_workers, int) or not 1 <= max_workers <= 8:
        raise ValueError("submission_pool_limit_invalid")
    queued = [dict(job) for job in jobs]
    request_ids = [_request_id(job) for job in queued]
    if len(set(request_ids)) != len(request_ids):
        raise ValueError("submission_request_duplicate")

    def run(job: dict) -> dict:
        request_id = _request_id(job)
        try:
            response = submit_one(job)
        except Exception as error:
            raw_code = str(error)
            code = raw_code if _FAILURE_CODE.fullmatch(raw_code) else "submission_failed"
            return {
                "request_id": request_id,
                "status": "unknown" if code in _UNKNOWN_CODES else "failed",
                "failure_code": code,
            }
        return {
            "request_id": request_id,
            "status": "submitted",
            "result": response,
        }

    with ThreadPoolExecutor(max_workers=min(max_workers, len(queued) or 1)) as pool:
        return list(pool.map(run, queued))
