from __future__ import annotations

from collections import Counter
from typing import Callable

from apps.oa_approval import api, auth, flow

from . import production_pending


def status(
    *,
    configuration_status: Callable[[], dict] | None = None,
    configs_loader: Callable[[], list[dict]] | None = None,
    api_factory: Callable[[dict], object] | None = None,
    identity_provider: Callable[[], str] | None = None,
) -> dict:
    credential_check = (configuration_status or auth.configuration_status)()
    if credential_check.get("status") != "ok":
        raise RuntimeError("runtime_live_configuration_missing")

    configs = (configs_loader or flow.load_all_configs)()
    client = (api_factory or api.build_api)(configs[0])
    rows = client.pending_rows(page_size=200, max_pages=100)
    identities = production_pending.identify_pending_rows(rows, configs)
    expected_identity = (identity_provider or (lambda: auth.env("OA_UID")))()
    operator_ids = [
        str(row.get("operator_id") or "").strip() for row in rows
    ]
    if rows and all(
        operator_id == expected_identity for operator_id in operator_ids
    ):
        identity_status = "verified"
    elif rows:
        identity_status = "mismatch"
    else:
        identity_status = "unknown"
    supported = [
        item for item in identities if item["support_status"] == "supported"
    ]
    by_workflow = Counter(str(item["flow_key"]) for item in supported)
    unsupported = len(identities) - len(supported)

    return {
        "schema_version": 1,
        "kind": "preaudit_live_status",
        "status": (
            "ok"
            if unsupported == 0 and identity_status == "verified"
            else "degraded"
        ),
        "credentials": credential_check,
        "oa": {
            "authentication": "ok",
            "execution_identity": identity_status,
            "pending_total": len(identities),
            "supported": len(supported),
            "unsupported": unsupported,
            "by_workflow": dict(sorted(by_workflow.items())),
        },
        "write_state": "not_requested",
    }
