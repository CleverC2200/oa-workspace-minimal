"""Compatibility imports for shared retention primitives."""

from apps.oa_approval.retention import (
    MARKER_NAME,
    MARKER_VALUE,
    RETENTION_DAYS,
    RetentionAction,
    RetentionRecord,
    apply_file_actions,
    apply_file_actions_locked,
    apply_task_actions,
    initialize_root,
    maintenance_lock,
    plan_retention,
    validate_file_actions,
)

__all__ = [
    "MARKER_NAME",
    "MARKER_VALUE",
    "RETENTION_DAYS",
    "RetentionAction",
    "RetentionRecord",
    "apply_file_actions",
    "apply_file_actions_locked",
    "apply_task_actions",
    "initialize_root",
    "maintenance_lock",
    "plan_retention",
    "validate_file_actions",
]
