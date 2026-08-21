from __future__ import annotations

from threading import Barrier, Lock
import time
import unittest

from apps.oa_finance_preaudit import submission_pool


class SubmissionPoolTests(unittest.TestCase):
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

        self.assertEqual(peak, 2)
        self.assertEqual([item["request_id"] for item in result], ["r1", "r2"])
        self.assertTrue(all(item["status"] == "submitted" for item in result))

    def test_duplicate_request_is_rejected_before_pool_start(self) -> None:
        with self.assertRaisesRegex(ValueError, "submission_request_duplicate"):
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

        self.assertEqual(result[0]["status"], "unknown")
        self.assertEqual(result[0]["failure_code"], "writer_result_unknown")
        self.assertEqual(result[1]["status"], "submitted")

    def test_stress_results_never_cross_request_identity(self) -> None:
        jobs = [{"request_id": f"r{index}"} for index in range(30)]

        result = submission_pool.run_bounded(
            jobs,
            lambda job: {"request_id": job["request_id"], "receipt": job["request_id"]},
            max_workers=3,
        )

        for expected, actual in zip(jobs, result):
            self.assertEqual(actual["request_id"], expected["request_id"])
            self.assertEqual(actual["result"]["receipt"], expected["request_id"])


if __name__ == "__main__":
    unittest.main()
