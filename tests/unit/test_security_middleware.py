"""Behaviour of the security headers middleware and its wiring helper."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from family_logistics.middleware import security
from family_logistics.middleware.security import (
    SecurityHeadersMiddleware,
    add_security_middleware,
)


def build_app() -> FastAPI:
    app = FastAPI()

    @app.get("/")
    def root() -> dict[str, str]:
        return {}

    return app


@pytest.mark.unit
class TestSecurityHeaders:
    def test_headers_are_added_to_every_response(self):
        app = build_app()
        app.add_middleware(SecurityHeadersMiddleware)
        response = TestClient(app).get("/")
        assert response.headers["X-Content-Type-Options"] == "nosniff"
        assert response.headers["X-Frame-Options"] == "DENY"
        assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]
        assert "Strict-Transport-Security" not in response.headers

    def test_hsts_is_sent_only_over_https(self):
        app = build_app()
        app.add_middleware(SecurityHeadersMiddleware)
        response = TestClient(app, base_url="https://testserver").get("/")
        assert "max-age=31536000" in response.headers["Strict-Transport-Security"]


@pytest.mark.unit
class TestAddSecurityMiddleware:
    def test_default_wiring_adds_headers_without_redirect(self):
        app = build_app()
        add_security_middleware(app)
        response = TestClient(app, follow_redirects=False).get("/")
        assert response.status_code == 200
        assert response.headers["X-Frame-Options"] == "DENY"

    def test_https_redirect_is_opt_in(self):
        app = build_app()
        add_security_middleware(app, enable_https_redirect=True)
        response = TestClient(app, follow_redirects=False).get("/")
        assert response.status_code == 307
        assert response.headers["location"].startswith("https://")

    def test_trusted_hosts_reject_other_hosts(self):
        app = build_app()
        add_security_middleware(app, allowed_hosts=["allowed.test"])
        allowed = TestClient(app, base_url="http://allowed.test").get("/")
        rejected = TestClient(app, base_url="http://other.test").get("/")
        assert allowed.status_code == 200
        assert rejected.status_code == 400

    def test_cors_allows_only_configured_origins(self):
        app = build_app()
        add_security_middleware(app, allowed_origins=["https://ok.test"])
        client = TestClient(app)
        ok = client.get("/", headers={"Origin": "https://ok.test"})
        other = client.get("/", headers={"Origin": "https://evil.test"})
        assert ok.headers["access-control-allow-origin"] == "https://ok.test"
        assert "access-control-allow-origin" not in other.headers

    def test_removed_middleware_is_not_exported(self):
        assert not hasattr(security, "RateLimitMiddleware")
        assert not hasattr(security, "SSRFPreventionMiddleware")
