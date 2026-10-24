"""URL checks shared by configuration and the outbound HTTP adapters.

One parser (``httpx.URL``, the same one the clients use) and one set of rules,
so a configuration check and a transport check cannot drift apart. Messages are
fixed strings: they never echo the rejected URL, which may carry a credential.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Final

import httpx

if TYPE_CHECKING:
    from collections.abc import Sequence

HTTPS_ONLY: Final = frozenset({"https"})
HTTP_OR_HTTPS: Final = frozenset({"http", "https"})


def url_problem(value: str, schemes: frozenset[str]) -> str | None:
    """Describe why ``value`` is not an acceptable service URL.

    A URL is acceptable when it is absolute, uses one of ``schemes``, names a
    host and embeds no credentials. Userinfo is refused because ``https://a@b``
    makes ``b`` the real host and httpx turns the userinfo into a Basic
    ``Authorization`` header.

    Args:
        value: The URL to check.
        schemes: Allowed lower-case schemes.

    Returns:
        A fixed message naming the problem, or None when the URL is acceptable.
    """
    try:
        url = httpx.URL(value)
    except httpx.InvalidURL:
        return "must be a valid absolute URL"
    if url.scheme not in schemes:
        return "must use " + " or ".join(f"{scheme}://" for scheme in sorted(schemes))
    if not url.host:
        return "must include a host"
    if url.userinfo:
        return "must not embed credentials"
    return None


_DOMAIN_LABEL = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
_DOMAIN = re.compile(rf"{_DOMAIN_LABEL}(?:\.{_DOMAIN_LABEL})+")
_NUMERIC_TLD = re.compile(r"[0-9]+")


def normalize_host(host: str) -> str:
    """Return a host name in comparison form: lower case, no trailing dot.

    Args:
        host: A host name or allowlist entry.

    Returns:
        ``host`` lower-cased with a single trailing root dot removed.
    """
    host = host.lower()
    return host.removesuffix(".")


def host_allowlist_problem(entries: Sequence[str]) -> str | None:
    """Describe why ``entries`` is not a usable host allowlist.

    Each entry must be a registrable domain such as ``icloud.com``: ASCII
    letters, digits and hyphens in dot-separated labels, at least two labels,
    and a non-numeric last label (so an IP address is never an entry). A
    scheme, path, port, wildcard, credentials or blanks make an entry invalid.

    Args:
        entries: Candidate allowlist entries.

    Returns:
        A fixed message that never echoes an entry, or None when usable.
    """
    if not entries:
        return "must list at least one domain"
    for entry in entries:
        domain = normalize_host(entry)
        if (
            _DOMAIN.fullmatch(domain) is None
            or _NUMERIC_TLD.fullmatch(domain.rsplit(".", 1)[-1]) is not None
        ):
            return (
                "entries must be bare domains such as icloud.com "
                "(no scheme, path, port or wildcard)"
            )
    return None


def host_allowed(host: str, allowed: Sequence[str]) -> bool:
    """Return whether ``host`` is an allowed domain or a subdomain of one.

    A host matches an entry when it equals the entry or ends with ``.`` plus
    the entry, compared case-insensitively after removing a trailing dot, so
    ``p42-caldav.icloud.com`` matches ``icloud.com`` while ``evil-icloud.com``
    and ``icloud.com.evil.net`` do not. An empty host or an IP address never
    matches a domain entry.

    Args:
        host: Host name taken from a parsed URL.
        allowed: Normalized allowlist entries (see ``host_allowlist_problem``).

    Returns:
        True when the host is covered by an entry.
    """
    name = normalize_host(host)
    if not name:
        return False
    return any(
        name == entry or name.endswith("." + entry)
        for entry in map(normalize_host, allowed)
    )
