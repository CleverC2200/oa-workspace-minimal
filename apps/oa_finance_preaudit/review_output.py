"""Pure final-output parsing shared by production consumption and offline replay."""
from __future__ import annotations

from dataclasses import dataclass
import json
import math
import re
from typing import Callable


def parse_json(raw: str) -> object:
    """Unwrap one complete JSON fence without selecting or repairing a decision."""
    block = re.fullmatch(r"\s*```json[ \t]*\r?\n(.*?)\r?\n```\s*", raw, re.DOTALL)
    if block:
        raw = block.group(1)

    def unique_fields(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("decision_json_duplicate_field")
            result[key] = value
        return result

    def reject_constant(_value):
        raise ValueError("decision_json_nonfinite")

    def finite_float(value):
        number = float(value)
        if not math.isfinite(number):
            raise ValueError("decision_json_nonfinite")
        return number

    return json.loads(raw, object_pairs_hook=unique_fields,
                      parse_constant=reject_constant, parse_float=finite_float)


@dataclass(frozen=True)
class ParsedOutput:
    value: object
    state: str
    code: str | None = None
    line: int | None = None
    column: int | None = None


def consume(raw: bytes, *, parser: Callable[[str], object] = parse_json) -> ParsedOutput:
    try:
        text = raw.decode("utf-8")
        value = parser(text)
    except UnicodeDecodeError:
        return ParsedOutput(None, "unreadable", "invalid_utf8")
    except json.JSONDecodeError as error:
        return ParsedOutput(None, "unreadable", "invalid_json", error.lineno, error.colno)
    except ValueError as error:
        safe = str(error) if str(error) in {"decision_json_duplicate_field", "decision_json_nonfinite"} else "invalid_json"
        return ParsedOutput(None, "unreadable", safe)
    return ParsedOutput(value, "valid_object" if isinstance(value, dict) else "not_object",
                        None if isinstance(value, dict) else "decision_object")
