import json
from datetime import datetime, timezone


def parse_iso_datetime(value):
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is not None:
            return parsed.astimezone(timezone.utc).replace(tzinfo=None)
        return parsed
    except ValueError:
        return None


def safe_json_loads(value, default=None):
    if value in (None, ""):
        return {} if default is None else default
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return {} if default is None else default


def safe_json_dumps(value):
    return json.dumps(value or {}, separators=(",", ":"), sort_keys=True)


def normalize_subscriber_record(email, last_open=None, engagement_score=None, source_ref=None, source_data=None):
    return {
        "email": (email or "").strip().lower(),
        "last_open": last_open,
        "engagement_score": engagement_score,
        "source_ref": source_ref,
        "source_data": source_data or {},
    }
