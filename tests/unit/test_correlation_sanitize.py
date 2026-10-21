"""Inbound correlation and trace IDs are accepted only as short plain tokens."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from family_logistics.middleware.correlation import CorrelationMiddleware, sanitize_id


@pytest.fixture
def client():
    app = FastAPI()
    app.add_middleware(CorrelationMiddleware)

    @app.get("/")
    def root() -> dict[str, str]:
        return {}

    return TestClient(app)


@pytest.mark.unit
class TestSanitize:
    @pytest.mark.parametrize("value", ["abc", "run-1.2_3", "a" * 64])
    def test_accepts_plain_tokens(self, value):
        assert sanitize_id(value) == value

    @pytest.mark.parametrize(
        "value", [None, "", "a" * 65, "has space", "line\nbreak", "tab\there", "x;drop"]
    )
    def test_rejects_unsafe(self, value):
        assert sanitize_id(value) is None

    def test_oversized_correlation_id_replaced(self, client):
        response = client.get("/", headers={"X-Correlation-ID": "a" * 500})
        assert response.headers["X-Correlation-ID"] != "a" * 500
        assert len(response.headers["X-Correlation-ID"]) == 36

    def test_unsafe_trace_headers_dropped(self, client):
        response = client.get(
            "/", headers={"X-Trace-ID": "bad value", "X-Span-ID": "s" * 100}
        )
        assert "X-Trace-ID" not in response.headers
        assert "X-Span-ID" not in response.headers

    def test_safe_ids_echoed(self, client):
        response = client.get(
            "/", headers={"X-Request-ID": "req-1", "X-Trace-ID": "trace-1"}
        )
        assert response.headers["X-Correlation-ID"] == "req-1"
        assert response.headers["X-Request-ID"] == "req-1"
        assert response.headers["X-Trace-ID"] == "trace-1"
