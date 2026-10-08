"""Shared packaging version identifiers for generated and consumed decisions."""
import re


# JSON Schema patterns search rather than fullmatch; reject trailing newlines too.
PACKAGE_VERSION_ID_PATTERN = r"^[0-9A-Za-z._-]{1,80}(?![\s\S])"
PACKAGE_VERSION_ID_SCHEMA = {
    "type": ["string", "null"],
    "pattern": PACKAGE_VERSION_ID_PATTERN,
}
PACKAGE_VERSION_ID = re.compile(PACKAGE_VERSION_ID_PATTERN)
