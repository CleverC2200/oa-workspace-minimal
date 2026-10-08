"""Shared packaging version identifiers for generated and consumed decisions."""
import re


# Native schema supports plain anchors; local fullmatch rejects trailing newlines.
PACKAGE_VERSION_ID_PATTERN = r"^[0-9A-Za-z._-]{1,80}$"
PACKAGE_VERSION_ID_SCHEMA = {
    "type": ["string", "null"],
    "pattern": PACKAGE_VERSION_ID_PATTERN,
}
PACKAGE_VERSION_ID = re.compile(PACKAGE_VERSION_ID_PATTERN)
