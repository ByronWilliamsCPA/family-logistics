"""Inbound correlation and trace IDs are accepted only as short plain tokens."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from family_logistics.middleware.correlation import (
    CorrelationMiddleware,
    correlation_context_processor,
    sanitize_id,
)

UUID_LENGTH = 36


@pytest.fixture
def seen():
    """Log context captured inside the request, as the log processor sees it."""
    return []


@pytest.fixture
def client(seen):
    app = FastAPI()
    app.add_middleware(CorrelationMiddleware)

    @app.get("/")
    def root() -> dict[str, str]:
        seen.append(correlation_context_processor(None, "info", {}))
        return {}

    return TestClient(app)


@pytest.mark.unit
class TestSanitize:
    @pytest.mark.parametrize("value", ["abc", "run-1.2_3", "a" * 64])
    def test_accepts_plain_tokens(self, value):
        assert sanitize_id(value) == value

    @pytest.mark.parametrize(
        "value",
        [
            None,
            "",
            "a" * 65,
            "has space",
            "line\nbreak",
            "tab\there",
            "x;drop",
            "abc\n",
            "\u0661\u0662\u0663",
            "caf\u00e9",
        ],
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

    def test_unsafe_request_id_is_replaced(self, client):
        response = client.get("/", headers={"X-Request-ID": "bad value\x01"})
        assert response.headers["X-Request-ID"] != "bad value\x01"
        assert len(response.headers["X-Request-ID"]) == UUID_LENGTH

    def test_unsafe_correlation_id_falls_back_to_a_safe_request_id(self, client):
        response = client.get(
            "/", headers={"X-Correlation-ID": "bad value", "X-Request-ID": "req-9"}
        )
        assert response.headers["X-Correlation-ID"] == "req-9"
        assert response.headers["X-Request-ID"] == "req-9"

    def test_trace_id_is_never_a_correlation_fallback(self, client):
        response = client.get("/", headers={"X-Trace-ID": "trace-1"})
        assert response.headers["X-Correlation-ID"] != "trace-1"
        assert len(response.headers["X-Correlation-ID"]) == UUID_LENGTH
        assert response.headers["X-Trace-ID"] == "trace-1"

    def test_safe_span_id_is_echoed(self, client):
        response = client.get("/", headers={"X-Span-ID": "span-1"})
        assert response.headers["X-Span-ID"] == "span-1"

    def test_unsafe_ids_never_reach_the_log_context(self, client, seen):
        client.get(
            "/",
            headers={
                "X-Correlation-ID": "evil value",
                "X-Request-ID": "evil\x01",
                "X-Trace-ID": "evil trace",
                "X-Span-ID": "evil span",
            },
        )
        context = seen[0]
        assert set(context) == {"correlation_id", "request_id"}
        assert len(context["correlation_id"]) == UUID_LENGTH
        assert len(context["request_id"]) == UUID_LENGTH

    def test_safe_ids_reach_the_log_context(self, client, seen):
        client.get(
            "/",
            headers={
                "X-Correlation-ID": "c-1",
                "X-Request-ID": "r-1",
                "X-Trace-ID": "t-1",
                "X-Span-ID": "s-1",
            },
        )
        assert seen[0] == {
            "correlation_id": "c-1",
            "request_id": "r-1",
            "trace_id": "t-1",
            "span_id": "s-1",
        }
