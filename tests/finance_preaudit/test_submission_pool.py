from __future__ import annotations

import time
from threading import Barrier, Lock

import pytest

from apps.oa_finance_preaudit import submission_pool


class TestSubmissionPool:
    def test_different_requests_run_concurrently_with_bound(self) -> None:
        barrier = Barrier(2)
        guard = Lock()
        active = 0
        peak = 0

        def submit(job: dict) -> dict:
            nonlocal active, peak
            with guard:
                active += 1
                peak = max(peak, active)
            barrier.wait(timeout=1)
            time.sleep(0.02)
            with guard:
                active -= 1
            return {"request_id": job["request_id"], "state": "submitted"}

        result = submission_pool.run_bounded(
            [{"request_id": "r1"}, {"request_id": "r2"}],
            submit,
            max_workers=2,
        )

        assert peak == 2
        assert [item["request_id"] for item in result] == ["r1", "r2"]
        assert all(item["status"] == "submitted" for item in result)

    def test_duplicate_request_is_rejected_before_pool_start(self) -> None:
        with pytest.raises(ValueError, match="submission_request_duplicate"):
            submission_pool.run_bounded(
                [{"request_id": "r1"}, {"request_id": "r1"}],
                lambda job: job,
                max_workers=2,
            )

    def test_failures_are_request_scoped(self) -> None:
        def submit(job: dict) -> dict:
            if job["request_id"] == "r1":
                raise RuntimeError("writer_result_unknown")
            return {"state": "submitted"}

        result = submission_pool.run_bounded(
            [{"request_id": "r1"}, {"request_id": "r2"}],
            submit,
            max_workers=2,
        )

        assert result[0]["status"] == "unknown"
        assert result[0]["failure_code"] == "writer_result_unknown"
        assert result[1]["status"] == "submitted"

    def test_stress_results_never_cross_request_identity(self) -> None:
        jobs = [{"request_id": f"r{index}"} for index in range(30)]

        result = submission_pool.run_bounded(
            jobs,
            lambda job: {"request_id": job["request_id"], "receipt": job["request_id"]},
            max_workers=3,
        )

        for expected, actual in zip(jobs, result):
            assert actual["request_id"] == expected["request_id"]
            assert actual["result"]["receipt"] == expected["request_id"]
