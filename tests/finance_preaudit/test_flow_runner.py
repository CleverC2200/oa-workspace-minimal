from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from apps.oa_finance_preaudit import flow_runner


class TestFlowRunner:
    def test_five_runners_have_isolated_config_context_state_and_metrics(self) -> None:
        configs = flow_runner.load_configs()

        assert set(configs) == set(flow_runner.WORKFLOW_KEYS)
        assert len({item.runner_id for item in configs.values()}) == 5
        assert len({item.state_root for item in configs.values()}) == 5
        assert len({item.metrics_root for item in configs.values()}) == 5
        for workflow_key, config in configs.items():
            assert config.workflow_key == workflow_key
            flow_contexts = [path for path in config.context_files if "/flows/" in path]
            assert flow_contexts == [config.rule_pack]
            assert workflow_key.replace("_", "-") + ".md" in config.rule_pack

    def test_config_rejects_duplicate_runner_identity(self) -> None:
        payload = json.loads(flow_runner.CONFIG_FILE.read_text(encoding="utf-8"))
        payload["runners"][1]["runner_id"] = payload["runners"][0]["runner_id"]
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "flow-runners.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            with pytest.raises(ValueError, match="flow_runner_config_invalid"):
                flow_runner.load_configs(path)

    def test_zero_one_and_backlog_close_without_idle_controller(self) -> None:
        config = flow_runner.load_configs()["travel_expense"]

        empty = flow_runner.plan_run(
            config,
            [],
            available_reviewer_slots=4,
            available_submit_slots=4,
        )
        single = flow_runner.plan_run(
            config,
            [{"request_id": "r1", "workflow_key": "travel_expense", "state": "queued"}],
            available_reviewer_slots=4,
            available_submit_slots=4,
        )
        backlog = flow_runner.plan_run(
            config,
            [
                {
                    "request_id": f"r{index}",
                    "workflow_key": "travel_expense",
                    "state": "queued",
                }
                for index in range(8)
            ],
            available_reviewer_slots=2,
            available_submit_slots=4,
        )

        assert empty["closure"] == "closed"
        assert empty["reviewers"] == 0
        assert single["reviewers"] == 1
        assert backlog["reviewers"] == 2
        assert not backlog["persistent_controller"]

    def test_prepared_submissions_use_independent_bounded_capacity(self) -> None:
        config = flow_runner.load_configs()["business_entertainment"]
        candidates = [
            {
                "request_id": f"r{index}",
                "workflow_key": config.workflow_key,
                "state": "prepared",
            }
            for index in range(10)
        ]

        plan = flow_runner.plan_run(
            config,
            candidates,
            available_reviewer_slots=4,
            available_submit_slots=2,
        )

        assert plan["reviewers"] == 0
        assert plan["submitters"] == 2
        assert plan["closure"] == "run_to_completion"

    def test_cross_flow_input_fails_only_that_runner(self) -> None:
        configs = flow_runner.load_configs()
        with pytest.raises(ValueError, match="flow_candidate_mismatch"):
            flow_runner.plan_run(
                configs["travel_expense"],
                [
                    {
                        "request_id": "bad",
                        "workflow_key": "business_support_fee",
                        "state": "queued",
                    }
                ],
                available_reviewer_slots=1,
                available_submit_slots=1,
            )

        healthy = flow_runner.plan_run(
            configs["business_support_fee"],
            [
                {
                    "request_id": "ok",
                    "workflow_key": "business_support_fee",
                    "state": "queued",
                }
            ],
            available_reviewer_slots=1,
            available_submit_slots=1,
        )
        assert healthy["reviewers"] == 1
