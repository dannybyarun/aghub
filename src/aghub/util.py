import re
from datetime import datetime, timedelta, timezone

ISO = "%Y-%m-%dT%H:%M:%SZ"


def now():
    return datetime.now(timezone.utc)


def now_iso():
    return now().strftime(ISO)


def ago_iso(seconds):
    return (now() - timedelta(seconds=int(seconds))).strftime(ISO)


def in_iso(seconds):
    return (now() + timedelta(seconds=int(seconds))).strftime(ISO)


def parse_iso(value):
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        return datetime.strptime(str(value), ISO).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def age_seconds(value):
    dt = parse_iso(value)
    if dt is None:
        return None
    return max(0, int((now() - dt).total_seconds()))


def human_age(seconds):
    if seconds is None:
        return "never"
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"


def slug(value):
    return re.sub(r"[^a-z0-9]+", "-", str(value).lower()).strip("-") or "agent"
