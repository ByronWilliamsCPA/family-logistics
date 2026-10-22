"""SSRF blocklist and rate-limit behaviour of the (not yet wired) middleware."""

from __future__ import annotations

import ipaddress

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.requests import Request
from starlette.responses import PlainTextResponse

from family_logistics.middleware.security import (
    ALL_INTERFACES,
    RateLimitMiddleware,
    SSRFPreventionMiddleware,
)


def ssrf() -> SSRFPreventionMiddleware:
    return SSRFPreventionMiddleware(app=FastAPI())


@pytest.mark.unit
class TestSsrfBlocklist:
    def test_all_interfaces_constant_is_the_unspecified_address(self):
        assert ipaddress.ip_address(ALL_INTERFACES).is_unspecified
        assert ALL_INTERFACES in SSRFPreventionMiddleware.BLOCKED_HOSTS

    @pytest.mark.parametrize(
        "url",
        [
            "http://localhost/x",
            "http://LOCALHOST/x",
            "http://127.0.0.1/x",
            "http://0.0.0.0/x",
            "http://0/x",
            "http://[::1]/x",
            "http://[::ffff:127.0.0.1]/x",
            "http://169.254.169.254/latest/meta-data",
            "http://10.0.0.1/x",
            "http://192.168.1.10/x",
            "http://2130706433/x",
            "http://metadata.google.internal/x",
            "http://user@127.0.0.1/x",
            "file:///etc/passwd",
            "gopher://example.invalid/",
        ],
    )
    def test_internal_destinations_are_blocked(self, url):
        assert ssrf()._is_blocked_url(url) is True

    @pytest.mark.parametrize(
        "url",
        ["https://example.com/x", "https://caldav.icloud.com/", "http://8.8.8.8/"],
    )
    def test_public_destinations_are_allowed(self, url):
        assert ssrf()._is_blocked_url(url) is False

    def test_middleware_rejects_a_blocked_url_in_a_query_parameter(self):
        app = FastAPI()
        app.add_middleware(SSRFPreventionMiddleware)

        @app.get("/")
        def root() -> dict[str, str]:
            return {}

        client = TestClient(app)
        blocked = client.get("/", params={"u": "http://0.0.0.0/x"})
        allowed = client.get("/", params={"u": "https://example.com/x"})
        assert blocked.status_code == 400
        assert allowed.status_code == 200


def request_without_client() -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/",
            "headers": [],
            "query_string": b"",
            "client": None,
        }
    )


@pytest.mark.unit
class TestRateLimit:
    @pytest.mark.asyncio
    async def test_missing_client_is_limited_under_the_unknown_bucket(self, caplog):
        middleware = RateLimitMiddleware(app=FastAPI(), requests_per_minute=1)

        async def call_next(_request: Request) -> PlainTextResponse:
            return PlainTextResponse("ok")

        with caplog.at_level("WARNING"):
            first = await middleware.dispatch(request_without_client(), call_next)
            second = await middleware.dispatch(request_without_client(), call_next)
        assert (first.status_code, second.status_code) == (200, 429)
        assert "request.client is None" in caplog.text
        assert list(middleware.requests) == ["unknown"]
