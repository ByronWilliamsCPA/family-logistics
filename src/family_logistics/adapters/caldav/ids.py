"""Opaque calendar identifiers (single source of truth).

A calendar is stored and referred to by ``calendar_id``, never by its URL: the
URL embeds the iCloud account number. The identifier is derived from the
normalized absolute collection URL:

- scheme and host lowercased, default port dropped;
- path kept as is, with exactly one trailing slash;
- query and fragment removed.

``calendar_id = "cal-" + sha256(normalized_url).hexdigest()[:16]`` (20 chars),
well inside the 100-character limit of registry audit references.

#ASSUME: External resources: iCloud keeps an account on the same partition
host; a host move changes every calendar_id and the seed must be re-mapped
with ``family-logistics collect discover``. #VERIFY in M0 check 1 and watch
for a sudden all-calendars-not-visible run (DATA-011 on every source).
"""

from __future__ import annotations

import hashlib
from urllib.parse import urlsplit, urlunsplit

CALENDAR_ID_PREFIX = "cal-"
CALENDAR_ID_HEX = 16
DEFAULT_PORTS = {"https": 443, "http": 80}


def normalize_calendar_href(href: str) -> str:
    """Normalize an absolute calendar collection URL.

    Args:
        href: Absolute calendar URL as returned by discovery.

    Returns:
        ``scheme://host[:port]/path/`` with lowercased scheme and host, one
        trailing slash, and no query or fragment.

    Raises:
        ValueError: When ``href`` is not an absolute URL.
    """
    parts = urlsplit(href.strip())
    if not parts.scheme or not parts.hostname:
        msg = "calendar href must be an absolute URL"
        raise ValueError(msg)
    scheme = parts.scheme.lower()
    host = parts.hostname.lower()
    if parts.port is not None and parts.port != DEFAULT_PORTS.get(scheme):
        host = f"{host}:{parts.port}"
    path = (parts.path or "/").rstrip("/") + "/"
    return urlunsplit((scheme, host, path, "", ""))


def calendar_id_for_href(href: str) -> str:
    """Derive the opaque calendar identifier from a calendar URL.

    Args:
        href: Absolute calendar collection URL.

    Returns:
        ``cal-`` followed by 16 hex characters of the normalized URL's SHA-256.
    """
    digest = hashlib.sha256(normalize_calendar_href(href).encode()).hexdigest()
    return CALENDAR_ID_PREFIX + digest[:CALENDAR_ID_HEX]
