"""Tests for the shared failure-logging helper in ``utils.logging``."""

from structlog.testing import capture_logs

from family_logistics.utils.logging import get_logger, log_failure

# Stands in for household text that an exception message could carry.
SENTINEL = "SENTINEL Passenger E pickup note"


class TestLogFailure:
    """``log_failure`` logs the class name and the given fields, nothing else."""

    def test_the_event_carries_the_class_name_and_the_fields(self):
        logger = get_logger("test_logging_helpers")
        with capture_logs() as captured:
            log_failure(logger, "job_failed", ValueError(SENTINEL), job="scan")
        assert captured == [
            {
                "event": "job_failed",
                "log_level": "error",
                "error": "ValueError",
                "job": "scan",
            }
        ]

    def test_the_message_never_reaches_the_log(self):
        logger = get_logger("test_logging_helpers")
        with capture_logs() as captured:
            log_failure(logger, "job_failed", RuntimeError(SENTINEL), run_id="run-1")
        assert SENTINEL not in repr(captured)

    def test_no_traceback_is_attached(self):
        logger = get_logger("test_logging_helpers")
        try:
            raise KeyError(SENTINEL)
        except KeyError as exc:
            with capture_logs() as captured:
                log_failure(logger, "job_failed", exc)
        assert "exc_info" not in captured[0]
        assert SENTINEL not in repr(captured)
