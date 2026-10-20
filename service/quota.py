"""Detection of Gemini's per-account quota limit and its reset time.

When an account exhausts a model's image quota, Gemini does not raise an error:
it answers with a plain-text refusal and no media, e.g.

    "I can create more images as soon as your limit resets."
    "You've reached your limit for creating images. Your limit resets Oct 21 at 2:15 PM."

The service treats such a reply as a quota hit: the account is locked for that
model until the stated reset time (or a configurable fallback when the message
gives none), so no further requests are sent to Gemini on its behalf.
"""

import re
from datetime import datetime, timedelta, timezone, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from gemini_webapi.types import ModelOutput

_LIMIT_PATTERNS = [
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\blimit\s+resets?\b",
        r"\breached\s+(?:your|the|my)\s+(?:\w+\s+){0,3}limit\b",
        r"\b(?:can't|can’t|cannot|can not|unable to)\s+(?:create|generate|make)\s+"
        r"(?:any\s+)?more\s+(?:images|pictures|videos)\b",
        r"\b(?:image|video)\s+(?:creation|generation)\s+limit\b",
    )
]

_TIME = r"(?P<hour>\d{1,2})(?::(?P<minute>\d{2}))?\s*(?P<ampm>[ap]\.?\s?m\.?)?"
_MONTHS = (
    "jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"
)
_ABSOLUTE = re.compile(
    r"resets?\s+(?:on\s+)?(?:\w+day,?\s+)?(?P<month>[A-Za-z]{3,9})\.?\s+(?P<day>\d{1,2})"
    r"(?:,?\s+(?P<year>\d{4}))?,?\s+(?:at\s+)?" + _TIME,
    re.IGNORECASE,
)
_TOMORROW = re.compile(r"resets?\s+tomorrow,?\s+(?:at\s+)?" + _TIME, re.IGNORECASE)
_TODAY = re.compile(r"resets?\s+(?:today,?\s+)?at\s+" + _TIME, re.IGNORECASE)
_RELATIVE = re.compile(
    r"resets?\s+in\s+(?:about\s+)?(?P<n>\d+)\s*(?P<unit>minute|min|hour|hr|day)s?",
    re.IGNORECASE,
)
_UNIT_SECONDS = {"minute": 60, "min": 60, "hour": 3600, "hr": 3600, "day": 86400}


def is_quota_limited(output: ModelOutput) -> bool:
    """Whether a generation reply is Gemini's quota-limit refusal (text, no media)."""

    if output.images or output.videos or output.media:
        return False
    text = output.text or ""
    return any(p.search(text) for p in _LIMIT_PATTERNS)


def resolve_tz(name: str) -> tzinfo:
    """Timezone Gemini renders reset times in; falls back to UTC if unknown."""

    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return timezone.utc


def _clock(match: re.Match) -> tuple[int, int] | None:
    hour, minute = int(match["hour"]), int(match["minute"] or 0)
    ampm = (match["ampm"] or "").replace(".", "").replace(" ", "").lower()
    if ampm == "pm" and hour < 12:
        hour += 12
    elif ampm == "am" and hour == 12:
        hour = 0
    if hour > 23 or minute > 59:
        return None
    return hour, minute


def _absolute(match: re.Match, local_now: datetime) -> datetime | None:
    month_key = match["month"][:3].lower()
    if month_key not in _MONTHS:
        return None
    clock = _clock(match)
    if clock is None:
        return None
    year = int(match["year"]) if match["year"] else local_now.year
    try:
        when = local_now.replace(
            year=year, month=_MONTHS.index(month_key) + 1, day=int(match["day"]),
            hour=clock[0], minute=clock[1], second=0, microsecond=0,
        )
    except ValueError:
        return None
    # A year-less date that already passed (e.g. "Jan 1" seen on Dec 31) is next year.
    if not match["year"] and when < local_now - timedelta(days=1):
        when = when.replace(year=when.year + 1)
    return when


def _at_clock(match: re.Match, local_now: datetime, days: int) -> datetime | None:
    clock = _clock(match)
    if clock is None:
        return None
    base = local_now + timedelta(days=days)
    when = base.replace(hour=clock[0], minute=clock[1], second=0, microsecond=0)
    if days == 0 and when <= local_now:  # "at 1 AM" said after 1 AM means tomorrow
        when += timedelta(days=1)
    return when


def parse_reset_at(text: str, now: datetime, tz: tzinfo) -> datetime | None:
    """Reset time stated in a quota-limit message, as an aware UTC datetime.

    ``now`` must be timezone-aware. Wall-clock times in the message are read in
    ``tz``. Returns None when the message carries no recognisable reset time.
    """

    local_now = now.astimezone(tz)
    when: datetime | None = None
    if m := _RELATIVE.search(text):
        when = local_now + timedelta(seconds=int(m["n"]) * _UNIT_SECONDS[m["unit"].lower()])
    elif m := _TOMORROW.search(text):
        when = _at_clock(m, local_now, days=1)
    elif m := _ABSOLUTE.search(text):
        when = _absolute(m, local_now)
    elif m := _TODAY.search(text):
        when = _at_clock(m, local_now, days=0)
    return when.astimezone(timezone.utc) if when else None


def lock_until(text: str, now: datetime, tz: tzinfo, fallback_seconds: float) -> datetime:
    """When a quota lock should lift: the stated reset time, else ``now + fallback``."""

    stated = parse_reset_at(text, now, tz)
    if stated and stated > now:
        return stated
    return now + timedelta(seconds=fallback_seconds)
