"""URL checks shared by configuration and the outbound HTTP adapters.

One parser (``httpx.URL``, the same one the clients use) and one set of rules,
so a configuration check and a transport check cannot drift apart. Messages are
fixed strings: they never echo the rejected URL, which may carry a credential.
"""

from __future__ import annotations

from typing import Final

import httpx

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
