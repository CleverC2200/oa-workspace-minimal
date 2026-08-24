from __future__ import annotations


SUBMIT_REJECTED = "writer_submit_rejected"
TRANSPORT_UNKNOWN = "writer_transport_unknown"
POSTCHECK_TIMEOUT = "writer_postcheck_timeout"
PENDING_CONFLICT = "writer_pending_conflict"

FAILURE_CODES = frozenset(
    {
        SUBMIT_REJECTED,
        TRANSPORT_UNKNOWN,
        POSTCHECK_TIMEOUT,
        PENDING_CONFLICT,
    }
)
