from __future__ import annotations


SUBMIT_REJECTED = "writer_submit_rejected"
TRANSPORT_UNKNOWN = "writer_transport_unknown"
POSTCHECK_TIMEOUT = "writer_postcheck_timeout"
PENDING_CONFLICT = "writer_pending_conflict"
PREFLIGHT_FAILED = "writer_preflight_failed"
TARGET_CHANGED = "submission_target_changed"

CONFIRMED_NO_WRITE_CODES = frozenset(
    {
        SUBMIT_REJECTED,
        PREFLIGHT_FAILED,
        TARGET_CHANGED,
    }
)

FAILURE_CODES = frozenset(
    {
        *CONFIRMED_NO_WRITE_CODES,
        TRANSPORT_UNKNOWN,
        POSTCHECK_TIMEOUT,
        PENDING_CONFLICT,
    }
)

UNKNOWN_WRITE_CODES = frozenset({
    TRANSPORT_UNKNOWN, POSTCHECK_TIMEOUT, PENDING_CONFLICT, "writer_result_unknown",
})
