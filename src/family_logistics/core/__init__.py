"""Core configuration, settings, and exception modules."""

from family_logistics.core.config import Settings
from family_logistics.core.exceptions import (
    APIError,
    AuthenticationError,
    AuthorizationError,
    BusinessLogicError,
    ConfigurationError,
    DatabaseError,
    ExternalServiceError,
    ProjectBaseError,
    ResourceNotFoundError,
    ValidationError,
)

__all__ = [
    # Exceptions (sorted alphabetically)
    "APIError",
    "AuthenticationError",
    "AuthorizationError",
    "BusinessLogicError",
    "ConfigurationError",
    "DatabaseError",
    "ExternalServiceError",
    "ProjectBaseError",
    "ResourceNotFoundError",
    # Configuration
    "Settings",
    "ValidationError",
]
