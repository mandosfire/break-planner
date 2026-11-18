"""Permanent break-planning exclusion list. Does not modify roster history."""
from __future__ import annotations
from roster import normalize_name

EXCLUDED_EMAILS = frozenset({"hakan.kacmaz@concentrix.com"})
EXCLUDED_NAMES = frozenset({"hakan kacmaz"})


def is_excluded_moderator(record: dict) -> bool:
    """Recognize a moderator even if their email or display name changes."""
    email = str(record.get("Email") or record.get("email") or "").strip().casefold()
    name = str(record.get("Name") or record.get("name") or "")
    return email in EXCLUDED_EMAILS or normalize_name(name) in EXCLUDED_NAMES
