#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "httpx>=0.27",
#     "defusedxml>=0.7",
#     "icalendar>=6.0",
# ]
# ///
"""M0 check 2: export sanitized VEVENT fixtures from iCloud (or a local .ics).

Pulls selected events (by UID or summary text) over read-only CalDAV, or reads
``.ics`` files exported from Apple Calendar, and writes sanitized fixtures that
keep the structure Family Logistics must parse:

* kept: hashed UID, DTSTART/DTEND (with TZID), RRULE, RDATE, EXDATE,
  RECURRENCE-ID, SEQUENCE, STATUS, SUMMARY and DESCRIPTION (sanitized),
  VTIMEZONE components;
* removed: LOCATION, ATTENDEE, ORGANIZER, URL, GEO, CONTACT, ATTACH, all X-
  properties (including Apple structured locations), VALARM components;
* replaced: external names via a mapping file kept OUTSIDE the repository,
  addresses (``@ venue/address`` values, street-address patterns), e-mail
  addresses and phone numbers;
* optional: ``--shift-days`` moves every date by N days (use a multiple of 7 to
  keep weekdays) so real transportation history never lands in Git.

A residual scan refuses to write any fixture that still contains an e-mail,
phone number, street address or a mapped real name. Output into a git-tracked
directory is refused unless ``--allow-repo-path`` is given; even then, review
every file before committing.

Exit codes:
    0  fixtures written
    1  residual personal data detected or no matching events (nothing written)
    2  usage, authentication or connectivity error

Run with: ``uv run --script tools/m0/check2_export_sanitized_fixtures.py --help``
"""

from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import os
import re
import shutil
import subprocess  # nosec B404 - fixed git invocation, no shell
import sys
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Final
from urllib.parse import urljoin

import httpx
from defusedxml.ElementTree import fromstring as safe_fromstring
from icalendar import Calendar, Event, Timezone

if TYPE_CHECKING:
    import xml.etree.ElementTree as ET  # nosec B405 - type annotations only

ALLOWED_METHODS: Final = frozenset({"PROPFIND", "REPORT", "GET", "OPTIONS"})
DEFAULT_URL: Final = "https://caldav.icloud.com/"
NS: Final = {"d": "DAV:", "c": "urn:ietf:params:xml:ns:caldav"}
KEEP_PROPS: Final = frozenset(
    {
        "UID",
        "DTSTART",
        "DTEND",
        "DURATION",
        "RRULE",
        "RDATE",
        "EXDATE",
        "RECURRENCE-ID",
        "SEQUENCE",
        "STATUS",
        "TRANSP",
        "DTSTAMP",
        "CREATED",
        "LAST-MODIFIED",
        "SUMMARY",
        "DESCRIPTION",
    }
)
DATE_PROPS: Final = (
    "DTSTART",
    "DTEND",
    "RECURRENCE-ID",
    "DTSTAMP",
    "CREATED",
    "LAST-MODIFIED",
)
EMAIL_RE: Final = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
PHONE_RE: Final = re.compile(
    r"(?<!\d)(?:\+?1[\s.-]?)?\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}(?!\d)"
)
STREET_RE: Final = re.compile(
    r"\b\d{1,6}\s+(?:[NSEW]\.?\s+)?[A-Za-z0-9.'-]+(?:\s+[A-Za-z0-9.'-]+){0,4}\s+"
    r"(?:St|Street|Ave|Avenue|Rd|Road|Blvd|Boulevard|Dr|Drive|Ln|Lane|Way|Ct|Court|"
    r"Pl|Place|Pkwy|Parkway|Hwy|Highway|Cir|Circle|Ter|Terrace|Loop|Trl|Trail)\b\.?",
    re.IGNORECASE,
)
VENUE_AT_RE: Final = re.compile(r"(?m)^(\s*[^:\n]+:[^@\n]*?)@\s*.+$")

EXIT_OK: Final = 0
EXIT_REFUSED: Final = 1
EXIT_ERROR: Final = 2


class WriteAttemptBlockedError(RuntimeError):
    """Raised when code tries to send a method outside the read-only allow-list."""


class InsecureRequestError(RuntimeError):
    """Raised when a request would leave HTTPS or go outside the CalDAV site."""


def _site(host: str) -> str:
    """Return the last two DNS labels of ``host`` (``icloud.com`` for a partition host)."""
    return ".".join(host.lower().split(".")[-2:])


def check_request(request: httpx.Request, home_host: str) -> None:
    """Refuse a request that is not HTTPS or that leaves the CalDAV site.

    httpx calls this for every request, including each redirect hop, so a
    redirect (or a server-supplied href) cannot move the Basic credentials to
    plain HTTP or to an unrelated host. iCloud answers from per-account
    partition hosts under the same registrable domain, so those stay allowed.

    Args:
        request: The outgoing request or redirect hop.
        home_host: Host of the ``--url`` the run started from.

    Raises:
        InsecureRequestError: If the scheme is not https, the URL embeds
            credentials, or the host is outside the home host's site.
    """
    url = request.url
    if url.scheme != "https" or not url.host:
        msg = "refusing a non-HTTPS CalDAV request"
        raise InsecureRequestError(msg)
    if url.userinfo:
        msg = "refusing a CalDAV URL with embedded credentials"
        raise InsecureRequestError(msg)
    if _site(url.host) != _site(home_host):
        msg = f"refusing a CalDAV request to a different site ({url.host})"
        raise InsecureRequestError(msg)


def build_client(
    url: str,
    username: str,
    password: str,
    transport: httpx.BaseTransport | None = None,
) -> httpx.Client:
    """Build the CalDAV client: HTTPS only and no redirect off the CalDAV site.

    Args:
        url: CalDAV root URL; must be https with a host.
        username: Apple ID for Basic authentication.
        password: App-specific password; held only in memory.
        transport: Optional transport, for tests.

    Returns:
        A client that checks every request and redirect hop.

    Raises:
        InsecureRequestError: If ``url`` is not an https URL with a host.
    """
    home = httpx.URL(url)
    if home.scheme != "https" or not home.host:
        msg = "the CalDAV --url must be an https URL with a host"
        raise InsecureRequestError(msg)

    def guard(request: httpx.Request) -> None:
        check_request(request, home.host)

    return httpx.Client(
        auth=(username, password),
        timeout=30.0,
        follow_redirects=True,
        transport=transport,
        event_hooks={"request": [guard]},
    )


@dataclass
class SanitizeStats:
    """Counts of changes made to one event, for the human review summary."""

    removed_props: set[str] = field(default_factory=set[str])
    names: int = 0
    addresses: int = 0
    emails: int = 0
    phones: int = 0
    alarms: int = 0


class Sanitizer:
    """Applies pseudonym, address and contact redaction to calendar text."""

    def __init__(self, mapping: dict[str, str]) -> None:
        """Build regexes for the name mapping.

        Args:
            mapping: Real name to pseudonym, e.g. ``{"Jane": "External Passenger J"}``.
        """
        self._mapping = mapping
        self._patterns = [
            (re.compile(rf"\b{re.escape(real)}\b", re.IGNORECASE), pseudo)
            for real, pseudo in sorted(mapping.items(), key=lambda kv: -len(kv[0]))
        ]

    def text(self, value: str, stats: SanitizeStats) -> str:
        """Sanitize one text value.

        Args:
            value: Original text.
            stats: Accumulator for the review summary.

        Returns:
            Sanitized text.
        """
        out = value
        for pattern, pseudo in self._patterns:
            out, count = pattern.subn(pseudo, out)
            stats.names += count
        out, count = VENUE_AT_RE.subn(r"\1@ [ADDRESS]", out)
        stats.addresses += count
        out, count = STREET_RE.subn("[ADDRESS]", out)
        stats.addresses += count
        out, count = EMAIL_RE.subn("[EMAIL]", out)
        stats.emails += count
        out, count = PHONE_RE.subn("[PHONE]", out)
        stats.phones += count
        return out

    def residuals(self, value: str) -> list[str]:
        """List personal-data patterns still present in ``value``.

        Args:
            value: Text to scan.

        Returns:
            Human-readable residual descriptions (no values echoed).
        """
        found: list[str] = []
        if EMAIL_RE.search(value):
            found.append("e-mail address")
        if PHONE_RE.search(value):
            found.append("phone number")
        if STREET_RE.search(value):
            found.append("street address")
        found.extend(
            "mapped real name"
            for real in self._mapping
            if re.search(rf"\b{re.escape(real)}\b", value, re.IGNORECASE)
        )
        return found


def _shift(value: object, days: int) -> object:
    if isinstance(value, datetime | date):
        return value + timedelta(days=days)
    return value


def sanitize_event(
    event: Event, sanitizer: Sanitizer, shift_days: int, salt: str
) -> tuple[Event, SanitizeStats]:
    """Return a sanitized copy of one VEVENT.

    Args:
        event: Source event.
        sanitizer: Text sanitizer.
        shift_days: Days to add to every date.
        salt: Salt for UID hashing so fixtures cannot be matched to live UIDs.

    Returns:
        The sanitized event and change statistics.
    """
    stats = SanitizeStats()
    clean = Event()
    for name, value in event.property_items(recursive=False):
        key = name.upper()
        if key in {"BEGIN", "END"}:
            continue
        if key not in KEEP_PROPS:
            stats.removed_props.add(key)
            continue
        if key == "UID":
            digest = hashlib.sha256(f"{salt}:{value}".encode()).hexdigest()[:16]
            clean.add("UID", f"fixture-{digest}.family-logistics.invalid")
        elif key in {"SUMMARY", "DESCRIPTION"}:
            clean.add(key, sanitizer.text(str(value), stats))
        elif key in DATE_PROPS and shift_days:
            original = event.decoded(key)
            params = dict(getattr(value, "params", {}))
            clean.add(key, _shift(original, shift_days), parameters=params)
        elif key in {"EXDATE", "RDATE"} and shift_days:
            dates = [_shift(d.dt, shift_days) for d in getattr(value, "dts", [])]
            clean.add(key, dates, parameters=dict(getattr(value, "params", {})))
        elif key == "RRULE" and shift_days and "UNTIL" in value:
            rule = dict(value)
            rule["UNTIL"] = [_shift(u, shift_days) for u in rule["UNTIL"]]
            clean.add(key, rule)
        else:
            clean.add(key, value)
    stats.alarms = len(event.subcomponents)
    return clean, stats


def _in_git_worktree(path: Path) -> bool:
    probe = path if path.exists() else path.parent
    while not probe.exists():
        probe = probe.parent
    git = shutil.which("git")
    if git is None:
        return False
    try:
        result = subprocess.run(  # noqa: S603  # nosec B603 - fixed argv, no shell
            [git, "-C", str(probe), "rev-parse", "--is-inside-work-tree"],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.stdout.strip() == "true"


def _fetch_caldav(args: argparse.Namespace) -> list[Calendar]:
    password = os.environ.get("FL_CALDAV_APP_PASSWORD") or getpass.getpass(
        "App-specific password (input hidden): "
    )
    if not args.username or not password:
        msg = "CalDAV mode needs --username (or FL_CALDAV_USERNAME) and a password"
        raise ValueError(msg)
    client = build_client(args.url, args.username, password)

    def request(method: str, url: str, body: str, depth: str) -> ET.Element:
        if method not in ALLOWED_METHODS:
            raise WriteAttemptBlockedError(method)
        response = client.request(
            method,
            url,
            content=body,
            headers={"Depth": depth, "Content-Type": "application/xml; charset=utf-8"},
        )
        if response.status_code != httpx.codes.MULTI_STATUS:
            msg = f"{method} returned HTTP {response.status_code}"
            raise ConnectionError(msg)
        return safe_fromstring(response.content)

    def prop(url: str, inner: str, depth: str) -> ET.Element:
        return request(
            "PROPFIND",
            url,
            '<?xml version="1.0"?><d:propfind xmlns:d="DAV:" '
            f'xmlns:c="urn:ietf:params:xml:ns:caldav"><d:prop>{inner}</d:prop></d:propfind>',
            depth,
        )

    try:
        root = prop(args.url, "<d:current-user-principal/>", "0")
        principal = urljoin(
            args.url, root.findtext(".//d:current-user-principal/d:href", "", NS)
        )
        root = prop(principal, "<c:calendar-home-set/>", "0")
        home = urljoin(
            principal, root.findtext(".//c:calendar-home-set/d:href", "", NS)
        )
        root = prop(home, "<d:displayname/><d:resourcetype/>", "1")
        hrefs: list[str] = []
        for response in root.findall("d:response", NS):
            if response.find(".//d:resourcetype/c:calendar", NS) is None:
                continue
            name = response.findtext(".//d:displayname", "", NS)
            if args.calendar and args.calendar.lower() not in name.lower():
                continue
            hrefs.append(urljoin(home, response.findtext("d:href", "", NS)))
        now = datetime.now(UTC)
        fmt = "%Y%m%dT%H%M%SZ"
        start = (now - timedelta(days=args.days_back)).strftime(fmt)
        end = (now + timedelta(days=args.days_ahead)).strftime(fmt)
        body = (
            '<?xml version="1.0"?><c:calendar-query xmlns:d="DAV:" '
            'xmlns:c="urn:ietf:params:xml:ns:caldav"><d:prop><c:calendar-data/></d:prop>'
            '<c:filter><c:comp-filter name="VCALENDAR"><c:comp-filter name="VEVENT">'
            f'<c:time-range start="{start}" end="{end}"/></c:comp-filter></c:comp-filter>'
            "</c:filter></c:calendar-query>"
        )
        calendars: list[Calendar] = []
        for href in hrefs:
            root = request("REPORT", href, body, "1")
            calendars.extend(
                Calendar.from_ical(node.text)
                for node in root.findall(".//c:calendar-data", NS)
                if node.text
            )
        return calendars
    finally:
        client.close()


def _matches(event: Event, uids: list[str], summaries: list[str]) -> bool:
    if not uids and not summaries:
        return False
    uid = str(event.get("UID", ""))
    summary = str(event.get("SUMMARY", "")).lower()
    return uid in uids or any(s.lower() in summary for s in summaries)


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Export sanitized VEVENT fixtures (M0 check 2). Read-only."
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--from-ics", type=Path, nargs="+", help="local .ics export(s)")
    source.add_argument(
        "--caldav", action="store_true", help="fetch over read-only CalDAV"
    )
    parser.add_argument("--url", default=DEFAULT_URL, help="CalDAV root URL")
    parser.add_argument("--username", default=os.environ.get("FL_CALDAV_USERNAME"))
    parser.add_argument("--calendar", help="calendar display-name filter (CalDAV mode)")
    parser.add_argument("--days-back", type=int, default=2)
    parser.add_argument("--days-ahead", type=int, default=14)
    parser.add_argument(
        "--uid", action="append", default=[], help="event UID (repeatable)"
    )
    parser.add_argument(
        "--summary", action="append", default=[], help="summary substring (repeatable)"
    )
    parser.add_argument(
        "--mapping",
        type=Path,
        required=True,
        help='JSON {"Real name": "Pseudonym"} kept OUTSIDE the repository',
    )
    parser.add_argument("--out", type=Path, required=True, help="output directory")
    parser.add_argument(
        "--shift-days", type=int, default=0, help="shift all dates by N days"
    )
    parser.add_argument(
        "--allow-repo-path",
        action="store_true",
        help="permit writing inside a git work tree (review before committing)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Run the export.

    Args:
        argv: Optional argument list.

    Returns:
        Process exit code.
    """
    args = _parse_args(argv)
    if _in_git_worktree(args.mapping.resolve()):
        print(
            "error: the mapping file is inside a git work tree; keep it outside",
            file=sys.stderr,
        )
        return EXIT_ERROR
    in_repo = _in_git_worktree(args.out.resolve())
    if in_repo and not args.allow_repo_path:
        print(
            "error: --out is inside a git work tree; use --allow-repo-path only after "
            "deciding to commit, and review every file first",
            file=sys.stderr,
        )
        return EXIT_ERROR
    try:
        mapping = {
            str(k): str(v) for k, v in json.loads(args.mapping.read_text()).items()
        }
        calendars = (
            _fetch_caldav(args)
            if args.caldav
            else [Calendar.from_ical(p.read_bytes()) for p in args.from_ics]
        )
    except (
        OSError,
        ValueError,
        ConnectionError,
        httpx.HTTPError,
        InsecureRequestError,
    ) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR

    sanitizer = Sanitizer(mapping)
    salt = os.urandom(8).hex()
    outputs: list[tuple[str, bytes]] = []
    problems: list[str] = []
    for index, calendar in enumerate(calendars):
        selected = [
            e for e in calendar.walk("VEVENT") if _matches(e, args.uid, args.summary)
        ]
        if not selected:
            continue
        clean_cal = Calendar()
        clean_cal.add("PRODID", "-//family-logistics//m0 sanitized fixture//EN")
        clean_cal.add("VERSION", "2.0")
        for tz in calendar.walk("VTIMEZONE"):
            if isinstance(tz, Timezone):
                clean_cal.add_component(tz)
        for event in selected:
            clean, stats = sanitize_event(event, sanitizer, args.shift_days, salt)
            clean_cal.add_component(clean)
            label = str(clean.get("UID"))[:24]
            print(
                f"[{label}] removed={sorted(stats.removed_props) or '-'} "
                f"names={stats.names} addresses={stats.addresses} emails={stats.emails} "
                f"phones={stats.phones} alarms_dropped={stats.alarms} "
                f"recurrence_id={'RECURRENCE-ID' in clean} rrule={'RRULE' in clean}"
            )
        payload = clean_cal.to_ical()
        residual = sanitizer.residuals(payload.decode())
        if residual:
            problems.append(
                f"calendar #{index}: residual {', '.join(sorted(set(residual)))}"
            )
        outputs.append((f"fixture-{index:02d}.ics", payload))

    if not outputs:
        print("RESULT: no matching events (nothing written)")
        return EXIT_REFUSED
    if problems:
        print("RESULT: REFUSED, residual personal data:")
        for problem in problems:
            print(f" - {problem}")
        return EXIT_REFUSED
    args.out.mkdir(parents=True, exist_ok=True)
    for filename, payload in outputs:
        (args.out / filename).write_bytes(payload)
    print(f"RESULT: wrote {len(outputs)} fixture file(s) to {args.out}")
    if in_repo:
        print(
            "WARNING: written inside a git work tree. Review every file before committing."
        )
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
