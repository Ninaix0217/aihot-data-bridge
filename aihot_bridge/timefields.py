from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


def parse_timestamp(value: Any) -> datetime | None:
    """Parse a timezone-aware ISO 8601 timestamp without inventing one."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def published_at_in_window(
    item: dict[str, Any], window_start: datetime, window_end: datetime
) -> bool:
    """Return whether trusted published_at is in [window_start, window_end)."""
    published = parse_timestamp(item.get("published_at"))
    if published is None:
        return False
    return _utc(window_start) <= published < _utc(window_end)


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
