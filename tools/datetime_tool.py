"""
tools/datetime_tool.py — the current date and time, for a region.

Models have no clock. Asked "what day is it?" they either refuse or, worse,
answer from their training cutoff and sound confident about a date that is
months stale. This gives them the real answer.

Everything here is stdlib and offline: `zoneinfo` carries the full IANA
database including DST rules, so no API key, no network, and correct answers
during a DST transition. That matters because the sandbox denies network — a
tool that needed one would fail exactly where agents run.

One Windows caveat worth knowing: Windows ships no IANA database at all, so
`zoneinfo` there reads it from the `tzdata` PyPI package instead. Without it
`ZoneInfo("Europe/London")` raises and *every* timezone lookup fails — which
is why `tzdata` is a hard requirement on Windows in requirements.txt, not an
optional extra. Windows also names its own zone in its own vocabulary
("GMT Standard Time"), so the local zone is read from the registry and mapped
through the CLDR windowsZones table below.
"""

from __future__ import annotations

import re
import sys
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


# Windows names its time zones in its own vocabulary ("GMT Standard Time",
# "Pacific Standard Time"), which is not an IANA name and cannot be handed to
# ZoneInfo. This is the CLDR windowsZones mapping, trimmed to the zones a
# Windows machine is actually likely to be set to. An unmapped zone falls
# through to the offset search below rather than being guessed at.
WINDOWS_ZONE_TO_IANA = {
    "UTC": "UTC",
    "GMT Standard Time": "Europe/London",
    "Greenwich Standard Time": "Atlantic/Reykjavik",
    "W. Europe Standard Time": "Europe/Berlin",
    "Central Europe Standard Time": "Europe/Budapest",
    "Central European Standard Time": "Europe/Warsaw",
    "Romance Standard Time": "Europe/Paris",
    "E. Europe Standard Time": "Europe/Chisinau",
    "FLE Standard Time": "Europe/Kiev",
    "GTB Standard Time": "Europe/Bucharest",
    "Turkey Standard Time": "Europe/Istanbul",
    "Israel Standard Time": "Asia/Jerusalem",
    "Egypt Standard Time": "Africa/Cairo",
    "South Africa Standard Time": "Africa/Johannesburg",
    "W. Central Africa Standard Time": "Africa/Lagos",
    "E. Africa Standard Time": "Africa/Nairobi",
    "Russian Standard Time": "Europe/Moscow",
    "Arabian Standard Time": "Asia/Dubai",
    "Arab Standard Time": "Asia/Riyadh",
    "Iran Standard Time": "Asia/Tehran",
    "Pakistan Standard Time": "Asia/Karachi",
    "India Standard Time": "Asia/Kolkata",
    "Bangladesh Standard Time": "Asia/Dhaka",
    "SE Asia Standard Time": "Asia/Bangkok",
    "China Standard Time": "Asia/Shanghai",
    "Singapore Standard Time": "Asia/Singapore",
    "W. Australia Standard Time": "Australia/Perth",
    "Tokyo Standard Time": "Asia/Tokyo",
    "Korea Standard Time": "Asia/Seoul",
    "Cen. Australia Standard Time": "Australia/Adelaide",
    "AUS Eastern Standard Time": "Australia/Sydney",
    "E. Australia Standard Time": "Australia/Brisbane",
    "Tasmania Standard Time": "Australia/Hobart",
    "New Zealand Standard Time": "Pacific/Auckland",
    "Hawaiian Standard Time": "Pacific/Honolulu",
    "Alaskan Standard Time": "America/Anchorage",
    "Pacific Standard Time": "America/Los_Angeles",
    "Pacific Standard Time (Mexico)": "America/Tijuana",
    "US Mountain Standard Time": "America/Phoenix",
    "Mountain Standard Time": "America/Denver",
    "Mountain Standard Time (Mexico)": "America/Chihuahua",
    "Central Standard Time": "America/Chicago",
    "Central Standard Time (Mexico)": "America/Mexico_City",
    "Canada Central Standard Time": "America/Regina",
    "Eastern Standard Time": "America/New_York",
    "US Eastern Standard Time": "America/Indianapolis",
    "SA Pacific Standard Time": "America/Bogota",
    "Atlantic Standard Time": "America/Halifax",
    "SA Western Standard Time": "America/La_Paz",
    "Central Brazilian Standard Time": "America/Cuiaba",
    "E. South America Standard Time": "America/Sao_Paulo",
    "Argentina Standard Time": "America/Buenos_Aires",
    "Newfoundland Standard Time": "America/St_Johns",
}

_WINDOWS_TZ_KEY = "SYSTEM" + chr(92) + "CurrentControlSet" + chr(92) + "Control" + chr(92) + "TimeZoneInformation"


def _windows_zone_name():
    """The machine's IANA zone, read from the Windows registry."""
    try:
        import winreg
    except ImportError:
        return None
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, _WINDOWS_TZ_KEY) as key:
            windows_name, _ = winreg.QueryValueEx(key, "TimeZoneKeyName")
    except OSError:
        return None
    windows_name = (windows_name or "").strip().rstrip(chr(0))
    mapped = WINDOWS_ZONE_TO_IANA.get(windows_name)
    if mapped and mapped in available_timezones():
        return mapped
    return None


def _zone_matching_local_offset():
    """Any IANA zone whose current offset matches this machine's.

    Deliberately the last resort, and deliberately not a claim about where you
    are: it returns a zone that is correct about the *time*, which is all this
    tool can promise once it cannot name the region.
    """
    try:
        local_offset = datetime.now().astimezone().utcoffset()
    except (OSError, ValueError):
        return None
    for zone in ("UTC", "Europe/London", "Europe/Berlin", "America/New_York",
                 "America/Chicago", "America/Denver", "America/Los_Angeles",
                 "Asia/Kolkata", "Asia/Shanghai", "Asia/Tokyo", "Australia/Sydney"):
        try:
            if datetime.now(ZoneInfo(zone)).utcoffset() == local_offset:
                return zone
        except (ZoneInfoNotFoundError, KeyError, ValueError):
            continue
    return None


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
    # Windows has no /etc/localtime: the zone lives in the registry under a
    # Windows-specific name that has to be mapped back to IANA.
    if sys.platform == "win32":
        for candidate in (_windows_zone_name(), _zone_matching_local_offset()):
            if candidate:
                return candidate
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


def _spoken(moment) -> str:
    """A human-readable sentence form: "Wednesday, 27 August 2026 at 8:31 PM".

    Built by hand rather than with strftime because `%-d` and `%-I` (the
    no-leading-zero forms) are glibc/BSD extensions. Windows uses MSVCRT's
    strftime, where they are not "ignored" — the whole call raises
    ValueError("Invalid format string"). That took out get_datetime()
    completely on Windows, and date_time is in EVERY tool profile including
    `minimal`, so the one tool that exists to stop models inventing a
    timestamp was itself unavailable.

    The Windows spelling (`%#d`, `%#I`) is not used either: it would be one
    more platform branch to keep in step, and integer formatting is exact
    everywhere.
    """
    hour_24 = moment.hour
    hour_12 = hour_24 % 12 or 12
    meridiem = "AM" if hour_24 < 12 else "PM"
    return (
        f"{moment.strftime('%A')}, {moment.day} {moment.strftime('%B')} "
        f"{moment.year} at {hour_12}:{moment.minute:02d} {meridiem}"
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
        "spoken": _spoken(now),
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
