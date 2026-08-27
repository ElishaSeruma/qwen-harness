"""
tools/datetime_tool.py — the current date and time, for a region.

Models have no clock. Asked "what day is it?" they either refuse or, worse,
answer from their training cutoff and sound confident about a date that is
months stale. This gives them the real answer.

Everything here is stdlib and offline: `zoneinfo` carries the full IANA
database including DST rules, so no API key, no network, and correct answers
during a DST transition. That matters because the sandbox denies network — a
tool that needed one would fail exactly where agents run.
"""

from __future__ import annotations

import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError, available_timezones

# Common ways a person names a place, mapped to IANA zones. Deliberately small:
# any IANA name works directly, and an unknown place is reported as unknown
# rather than guessed at — a wrong timezone is worse than "I don't know".
CITY_ALIASES = {
    "utc": "UTC", "gmt": "UTC", "zulu": "UTC",
    "london": "Europe/London", "uk": "Europe/London", "england": "Europe/London",
    "dublin": "Europe/Dublin", "ireland": "Europe/Dublin",
    "paris": "Europe/Paris", "france": "Europe/Paris",
    "berlin": "Europe/Berlin", "germany": "Europe/Berlin",
    "madrid": "Europe/Madrid", "spain": "Europe/Madrid",
    "rome": "Europe/Rome", "italy": "Europe/Rome",
    "amsterdam": "Europe/Amsterdam", "netherlands": "Europe/Amsterdam",
    "lisbon": "Europe/Lisbon", "portugal": "Europe/Lisbon",
    "moscow": "Europe/Moscow", "russia": "Europe/Moscow",
    "new york": "America/New_York", "nyc": "America/New_York", "eastern": "America/New_York",
    "chicago": "America/Chicago", "central": "America/Chicago",
    "denver": "America/Denver", "mountain": "America/Denver",
    "los angeles": "America/Los_Angeles", "la": "America/Los_Angeles",
    "san francisco": "America/Los_Angeles", "pacific": "America/Los_Angeles",
    "seattle": "America/Los_Angeles", "toronto": "America/Toronto",
    "vancouver": "America/Vancouver", "mexico city": "America/Mexico_City",
    "sao paulo": "America/Sao_Paulo", "brazil": "America/Sao_Paulo",
    "buenos aires": "America/Argentina/Buenos_Aires",
    "lagos": "Africa/Lagos", "nigeria": "Africa/Lagos",
    "nairobi": "Africa/Nairobi", "kenya": "Africa/Nairobi",
    "kampala": "Africa/Kampala", "uganda": "Africa/Kampala",
    "cairo": "Africa/Cairo", "egypt": "Africa/Cairo",
    "johannesburg": "Africa/Johannesburg", "south africa": "Africa/Johannesburg",
    "dubai": "Asia/Dubai", "uae": "Asia/Dubai",
    "mumbai": "Asia/Kolkata", "delhi": "Asia/Kolkata", "india": "Asia/Kolkata",
    "singapore": "Asia/Singapore", "hong kong": "Asia/Hong_Kong",
    "beijing": "Asia/Shanghai", "shanghai": "Asia/Shanghai", "china": "Asia/Shanghai",
    "tokyo": "Asia/Tokyo", "japan": "Asia/Tokyo",
    "seoul": "Asia/Seoul", "korea": "Asia/Seoul",
    "sydney": "Australia/Sydney", "melbourne": "Australia/Melbourne",
    "australia": "Australia/Sydney", "auckland": "Pacific/Auckland",
    "new zealand": "Pacific/Auckland",
}


def _local_zone_name() -> str:
    """The machine's IANA zone name — the user's actual region."""
    # /etc/localtime is a symlink into the zoneinfo database on macOS and most
    # Linuxes; its tail is the IANA name. Far more useful than an abbreviation
    # like "IST", which is ambiguous (Irish vs India vs Israel).
    try:
        target = Path("/etc/localtime").resolve()
        parts = target.parts
        if "zoneinfo" in parts:
            name = "/".join(parts[parts.index("zoneinfo") + 1:])
            if name in available_timezones():
                return name
    except OSError:
        pass
    return time.tzname[0] if time.tzname else "UTC"


def resolve_timezone(location: str) -> tuple[str | None, str | None]:
    """(iana_name, error). Never guesses — an unknown place returns an error."""
    raw = (location or "").strip()
    if not raw:
        return _local_zone_name(), None

    if raw in available_timezones():
        return raw, None

    key = re.sub(r"[_\s]+", " ", raw.lower()).strip()
    if key in CITY_ALIASES:
        return CITY_ALIASES[key], None

    # "America/new_york" typed with odd casing.
    for zone in available_timezones():
        if zone.lower() == raw.lower().replace(" ", "_"):
            return zone, None

    # Last resort: a unique substring match on the city part of a zone name.
    needle = key.replace(" ", "_")
    matches = [z for z in available_timezones() if z.rsplit("/", 1)[-1].lower() == needle]
    if len(matches) == 1:
        return matches[0], None
    if len(matches) > 1:
        return None, f"'{location}' is ambiguous — could be {', '.join(sorted(matches)[:5])}."

    return None, (
        f"Unknown location '{location}'. Pass an IANA timezone such as "
        "'Europe/London' or 'America/New_York', or omit it for local time. "
        "Do not guess the time for a place you could not resolve."
    )


def get_datetime(location: str = "", format: str = "") -> dict[str, Any]:
    """Current date and time. Defaults to this machine's region.

    Args:
        location: city, country, or IANA timezone. Empty = local region.
        format:   optional strftime pattern for an extra `formatted` field.
    """
    zone_name, error = resolve_timezone(location)
    if error:
        return {"error": error, "requested": location}

    try:
        now = datetime.now(ZoneInfo(zone_name))
    except (ZoneInfoNotFoundError, ValueError) as exc:
        return {"error": f"could not load timezone '{zone_name}': {exc}"}

    utc_now = datetime.now(timezone.utc)
    offset = now.utcoffset()
    offset_minutes = int(offset.total_seconds() // 60) if offset else 0
    sign = "+" if offset_minutes >= 0 else "-"

    result = {
        "status": "ok",
        "timezone": zone_name,
        "is_local_region": not location,
        "date": now.strftime("%Y-%m-%d"),
        "time": now.strftime("%H:%M:%S"),
        "day_of_week": now.strftime("%A"),
        "spoken": now.strftime("%A, %-d %B %Y at %-I:%M %p"),
        "iso": now.isoformat(),
        "utc_iso": utc_now.isoformat(),
        "utc_offset": f"{sign}{abs(offset_minutes) // 60:02d}:{abs(offset_minutes) % 60:02d}",
        "abbreviation": now.tzname(),
        "is_dst": bool(now.dst()),
        "unix": int(now.timestamp()),
    }
    if format:
        try:
            result["formatted"] = now.strftime(format)
        except (ValueError, TypeError) as exc:
            result["format_error"] = str(exc)
    return result
