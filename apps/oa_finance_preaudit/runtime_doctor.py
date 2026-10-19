from __future__ import annotations

from collections import Counter
from typing import Callable

from apps.oa_approval import api, attachments, auth, flow

from . import production_pending


def status(
    *,
    configuration_status: Callable[[], dict] | None = None,
    configs_loader: Callable[[], list[dict]] | None = None,
    api_factory: Callable[[dict], object] | None = None,
    identity_provider: Callable[[], str] | None = None,
    web_session_provider: Callable[[], dict] | None = None,
) -> dict:
    credential_check = (configuration_status or auth.configuration_status)()
    if credential_check.get("status") != "ok":
        raise RuntimeError("runtime_live_configuration_missing")

    web_session = (
        web_session_provider or attachments.rpa_web_session_status
    )()
    if (
        not isinstance(web_session, dict)
        or web_session.get("status") != "ok"
        or web_session.get("authentication") != "ok"
        or web_session.get("source") != "rpa_page"
    ):
        raise RuntimeError("rpa_session_invalid")

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
    workflow_counts = Counter((item['workflow_id'], item['workflow_name'], item['flow_key'],
                               item['support_status']) for item in identities)

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
            "workflow_counts": [dict(zip(('workflow_id', 'workflow_name', 'flow_key', 'support_status'), key),
                                     count=count) for key, count in sorted(workflow_counts.items())],
        },
        "web_session": web_session,
        "write_state": "not_requested",
    }
