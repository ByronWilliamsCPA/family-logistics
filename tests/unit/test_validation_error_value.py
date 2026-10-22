"""``ValidationError(value=...)`` truncates; it does not redact (documented)."""

from __future__ import annotations

import pytest

from family_logistics.core.exceptions import ValidationError


@pytest.mark.unit
class TestValidationErrorValue:
    def test_value_is_truncated_to_100_characters(self):
        error = ValidationError("bad", field="x", value="v" * 150)
        assert error.details["value"] == "v" * 100 + "..."

    def test_short_value_is_kept_verbatim_and_reaches_to_dict(self):
        error = ValidationError("bad", value="abc")
        assert error.to_dict()["details"] == {"value": "abc"}

    def test_docstring_does_not_promise_redaction(self):
        doc = ValidationError.__doc__ or ""
        assert "sanitized" not in doc
        assert "never pass" in doc.lower()
