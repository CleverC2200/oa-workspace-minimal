"""Validate model-owned trip observations; never read or pair odometer images."""
from __future__ import annotations

from decimal import Decimal

from .decision_validation import InvalidDecision


MARKER = "fuel_trip_contract"
FIELD = "fuel_trip_observation"


def freeze_contract(workflow_key: str, material: dict) -> dict:
    fields = material.get("declared_fields")
    if workflow_key != "travel_expense" or not isinstance(fields, list):
        return {}
    refs = [row["path"] for row in fields if isinstance(row, dict)
            and row.get("code") == "sfdc" and row.get("read_status") == "present"
            and str(row.get("value") or "").strip() == "按次"
            and isinstance(row.get("path"), str) and row["path"].startswith("main[")]
    return {MARKER: {"version": 1, "mode_field_refs": refs}} if refs else {}


def binding(source: dict) -> dict:
    return {MARKER: source[MARKER]} if MARKER in source else {}


def accepted_packets(expected: dict, state: dict, *, material: dict, action: dict | None = None) -> list[dict]:
    """Old frozen actions keep their old contract; new ones cannot omit the marker."""
    marker = state.get(MARKER)
    if marker != material.get(MARKER):
        raise ValueError("frozen_packet_changed")
    if action is not None and action.get(MARKER) != marker:
        raise ValueError("frozen_packet_changed")
    if marker is not None:
        if marker != expected.get(MARKER):
            raise ValueError("frozen_packet_changed")
        return [expected]
    legacy = dict(expected)
    legacy.pop(MARKER, None)
    without_routing = dict(legacy)
    without_routing.pop("page_routing", None)
    return [legacy, without_routing]


def schema() -> dict:
    endpoint = {
        "type": "object", "additionalProperties": False,
        "required": ["reading_km", "locator", "image_ref"],
        "properties": {
            "reading_km": {"type": "number", "minimum": 0},
            "locator": {"type": "string", "minLength": 1},
            "image_ref": {"type": "string", "minLength": 1},
        },
    }
    group = {
        "type": "object", "additionalProperties": False,
        "required": ["trip_basis", "basis_refs", "start", "end", "distance_km"],
        "properties": {
            "trip_basis": {"type": "string", "minLength": 1},
            "basis_refs": {"type": "array", "minItems": 1,
                           "items": {"type": "string", "minLength": 1}},
            "start": endpoint, "end": endpoint,
            "distance_km": {"type": "number", "minimum": 0},
        },
    }
    return {
        "type": "object", "additionalProperties": False,
        "required": ["outcome", "groups", "total_km"],
        "properties": {
            "outcome": {"type": "string", "enum": ["grouped", "uncertain", "not_applicable"]},
            "groups": {"type": "array", "items": group, "maxItems": 100},
            "total_km": {"type": ["number", "null"], "minimum": 0},
        },
    }


def _number(value: object) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InvalidDecision("fuel_trip_observation")
    number = Decimal(str(value))
    if not number.is_finite() or number < 0:
        raise InvalidDecision("fuel_trip_observation")
    return number


def validate(value: object, *, contract: dict, item: dict, pages: tuple,
             declared_fields: object) -> None:
    error = "fuel_trip_observation"
    if not isinstance(value, dict) or set(value) != {"outcome", "groups", "total_km"}:
        raise InvalidDecision(error)
    outcome, groups = value["outcome"], value["groups"]
    if outcome not in {"grouped", "uncertain", "not_applicable"} or not isinstance(groups, list) or len(groups) > 100:
        raise InvalidDecision(error)
    refs = set(item["evidence_refs"])
    if not set(contract["mode_field_refs"]).issubset(refs):
        raise InvalidDecision(error)
    if outcome == "grouped":
        if not groups:
            raise InvalidDecision(error)
        total = _number(value["total_km"])
    else:
        if value["total_km"] is not None or (outcome == "not_applicable" and groups):
            raise InvalidDecision(error)
        if outcome == "uncertain" and item["status"] != "uncertain":
            raise InvalidDecision(error)
        total = None
    page_map = dict(pages)
    field_refs = {row["path"] for row in declared_fields if isinstance(row, dict)
                  and isinstance(row.get("path"), str)} if isinstance(declared_fields, list) else set()
    allowed_basis = field_refs | set(page_map)
    seen_pairs = set()
    aggregate = Decimal(0)
    for group in groups:
        if not isinstance(group, dict) or set(group) != {"trip_basis", "basis_refs", "start", "end", "distance_km"}:
            raise InvalidDecision(error)
        basis = group["basis_refs"]
        if (not isinstance(group["trip_basis"], str) or not group["trip_basis"].strip()
                or not isinstance(basis, list) or not basis
                or any(not isinstance(ref, str) or ref not in allowed_basis or ref not in refs for ref in basis)):
            raise InvalidDecision(error)
        endpoints = []
        readings = []
        for name in ("start", "end"):
            endpoint = group[name]
            if (not isinstance(endpoint, dict) or set(endpoint) != {"reading_km", "locator", "image_ref"}
                    or not isinstance(endpoint["locator"], str) or not endpoint["locator"].strip()
                    or not isinstance(endpoint["image_ref"], str)
                    or endpoint["image_ref"] not in page_map or endpoint["image_ref"] not in refs):
                raise InvalidDecision(error)
            readings.append(_number(endpoint["reading_km"]))
            image = page_map[endpoint["image_ref"]]
            endpoints.append(image.get("content_sha256") or endpoint["image_ref"])
        pair = tuple(endpoints)
        if pair[0] == pair[1] or pair in seen_pairs:
            raise InvalidDecision(error)
        seen_pairs.add(pair)
        distance = _number(group["distance_km"])
        if readings[1] - readings[0] != distance:
            raise InvalidDecision(error)
        aggregate += distance
    if total is not None and total != aggregate:
        raise InvalidDecision(error)
