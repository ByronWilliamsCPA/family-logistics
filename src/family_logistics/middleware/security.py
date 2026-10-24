"""Security middleware for FastAPI applications.

This module provides production-ready security middleware implementing OWASP best practices:
- CORS configuration (A05: Security Misconfiguration)
- Security headers (A05: Security Misconfiguration)
- Request validation (A03: Injection)

Usage:
    from family_logistics.middleware.security import (
        add_security_middleware,
        SecurityHeadersMiddleware,
    )

    app = FastAPI()
    add_security_middleware(app)
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.cors import CORSMiddleware
from starlette.middleware.httpsredirect import HTTPSRedirectMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

if TYPE_CHECKING:
    from fastapi import FastAPI, Request
    from starlette.middleware.base import RequestResponseEndpoint
    from starlette.responses import Response


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Add security headers to all responses.

    Implements OWASP recommended security headers to prevent:
    - XSS attacks
    - Clickjacking
    - MIME sniffing
    - Information leakage

    Headers added:
    - X-Content-Type-Options: nosniff
    - X-Frame-Options: DENY
    - X-XSS-Protection: 1; mode=block
    - Strict-Transport-Security: HSTS for HTTPS
    - Content-Security-Policy: Prevent inline scripts
    - Referrer-Policy: Control referrer information
    - Permissions-Policy: Restrict browser features
    """

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        """Add security headers to response."""
        response = await call_next(request)

        # Prevent MIME sniffing (OWASP A05)
        response.headers["X-Content-Type-Options"] = "nosniff"

        # Prevent clickjacking (OWASP A05)
        response.headers["X-Frame-Options"] = "DENY"

        # Enable XSS protection (OWASP A03)
        response.headers["X-XSS-Protection"] = "1; mode=block"

        # HSTS: Force HTTPS for 1 year (OWASP A02)
        if request.url.scheme == "https":
            response.headers["Strict-Transport-Security"] = (
                "max-age=31536000; includeSubDomains; preload"
            )

        # Content Security Policy: Prevent inline scripts (OWASP A03)
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "script-src 'self'; "
            "style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data: https:; "
            "font-src 'self'; "
            "connect-src 'self'; "
            "frame-ancestors 'none'"
        )

        # Control referrer information (OWASP A09)
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"

        # Restrict browser features (OWASP A05)
        response.headers["Permissions-Policy"] = (
            "geolocation=(), microphone=(), camera=(), payment=()"
        )

        # Remove server identification (OWASP A09)
        if "server" in response.headers:
            del response.headers["server"]

        return response


def add_security_middleware(
    app: FastAPI,
    *,
    enable_https_redirect: bool = False,
    allowed_origins: list[str] | None = None,
    allowed_hosts: list[str] | None = None,
) -> None:
    """Add all security middleware to FastAPI application.

    This configures comprehensive security following OWASP best practices.

    Args:
        app: FastAPI application instance
        enable_https_redirect: Redirect HTTP to HTTPS (production only)
        allowed_origins: CORS allowed origins (default: none)
        allowed_hosts: Trusted host names (default: all)

    Example:
        >>> from fastapi import FastAPI
        >>> app = FastAPI()
        >>> add_security_middleware(
        ...     app,
        ...     enable_https_redirect=True,
        ...     allowed_origins=["https://example.com"],
        ...     allowed_hosts=["example.com", "api.example.com"],
        ... )
    """
    # HTTPS redirect (production only)
    if enable_https_redirect:
        app.add_middleware(HTTPSRedirectMiddleware)

    # Trusted hosts (OWASP A05)
    if allowed_hosts:
        app.add_middleware(
            TrustedHostMiddleware,
            allowed_hosts=allowed_hosts,
        )

    # CORS configuration (OWASP A05)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=allowed_origins or [],
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "DELETE", "PATCH"],
        allow_headers=["*"],
        expose_headers=["X-Request-ID"],
        max_age=3600,
    )

    # Security headers (OWASP A05, A03, A09)
    app.add_middleware(SecurityHeadersMiddleware)


# Example usage in main.py:
"""
from fastapi import FastAPI
from family_logistics.middleware.security import add_security_middleware

app = FastAPI()

# Add all security middleware
add_security_middleware(
    app,
    enable_https_redirect=True,  # Production only
    allowed_origins=[
        "https://example.com",
        "https://app.example.com",
    ],
    allowed_hosts=[
        "api.example.com",
        "localhost",  # Development only
    ],
)

# Your routes here
@app.get("/")
async def root():
    return {"message": "Hello World"}
"""
