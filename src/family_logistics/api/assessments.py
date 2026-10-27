"""Home Assistant status and on-demand assessment routes (ADR 0007, M4).

``GET /api/v1/status`` is the family-wide card, ``POST /api/v1/assessments``
starts (or joins) an on-demand run and answers 202 with its ``run_id``, and
``GET /api/v1/assessments/{run_id}`` reports one run. All three are registered
on the router from ``build_ha_router``, so the bearer-token dependency applies
at router level and cannot be forgotten here.

``POST /api/v1/assessments`` is gated by ``FL_ON_DEMAND_ENABLED`` (off by
default): while it is off the route answers a fixed 503 and starts nothing. The
two GET routes only read, so they stay available either way.

Answers carry counts, times and fixed strings only: no child names, addresses,
calendar ids or finding text (privacy spec, section 4). An error body is a fixed
sentence and never echoes the path or any input.

#CRITICAL: Safety: a disabled POST must not allocate a run id or start a
thread, because an enabled one makes the process a production caller of
``AssessmentService.run`` before the M3 blockers (issues #19 to #23) close.
#VERIFY: tests/unit/test_assessments_api.py (TestDisabled).
#CRITICAL: Data integrity: a store error is a 503, never a card, so Home
Assistant cannot read an unreadable store as "clear" (design rule 4).
#VERIFY: tests/unit/test_status_api.py (TestStoreFailure).
#CRITICAL: Security: ``run_id`` is matched against the UUID shape by hand
instead of a FastAPI path pattern, because FastAPI's default 422 body echoes the
rejected input.
#VERIFY: tests/unit/test_assessments_api.py (TestGetAssessment malformed ids).
"""

from __future__ import annotations

import re
from datetime import datetime  # noqa: TC003 - pydantic resolves it at class build
from typing import TYPE_CHECKING, Final, Literal

from fastapi import status
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy.exc import SQLAlchemyError

from family_logistics.api.ha import ErrorBody
from family_logistics.services.on_demand import (
    OnDemandStartError,
    OnDemandStoppedError,
)
from family_logistics.services.status import (  # noqa: TC001 - pydantic needs it
    StatusReason,
)
from family_logistics.utils.logging import get_logger

if TYPE_CHECKING:
    from fastapi import APIRouter

    from family_logistics.services.on_demand import OnDemandService, RunProgress
    from family_logistics.services.status import StatusCard, StatusService

logger = get_logger(__name__)

STORE_UNAVAILABLE: Final = "Status store unavailable"
"""Fixed 503 text for a store error; it never carries SQL detail."""
STOPPING: Final = "Service is shutting down"
"""Fixed 503 text for a request that arrives during shutdown."""
ON_DEMAND_DISABLED: Final = "On-demand runs are disabled"
"""Fixed 503 text for ``POST /assessments`` while ``FL_ON_DEMAND_ENABLED`` is off."""
ON_DEMAND_DISABLED_CODE: Final = "on_demand_disabled"
"""Machine-readable ``code`` of that 503."""
ON_DEMAND_START_FAILED: Final = "Could not start the run"
"""Fixed 503 text when the run's thread could not be started."""
ON_DEMAND_START_FAILED_CODE: Final = "on_demand_start_failed"
"""Machine-readable ``code`` of that 503; a retry gets a new run."""
RUN_NOT_FOUND: Final = "Run not found"
"""Fixed 404 text for an unknown run id."""
INVALID_RUN_ID: Final = "Invalid run id"
"""Fixed 422 text for an id that is not a UUID."""

RunStatusValue = Literal["clear", "attention_required", "incomplete", "failed"]
"""The ``RunStatus`` values; ``TestResponseSchema`` pins them to the enum."""

_RUN_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
"""The shape ``uuid.uuid4()`` gives a run id (lower-case hex)."""


class StatusBody(BaseModel):
    """Body of ``GET /api/v1/status`` (see ``services.status.StatusCard``).

    Attributes:
        status: ``clear``, ``attention_required``, ``incomplete`` or ``failed``.
        reasons: Every condition that limits how far the card can be trusted,
            whatever the status: ``no_run``, ``run_interrupted``,
            ``analysis_stale``, ``calendar_stale``, ``school_snapshot_missing``
            or ``school_snapshot_stale``. A clear run with any reason is
            reported ``incomplete``; the reasons of other statuses (for example
            ``attention_required`` with ``school_snapshot_stale``) explain what
            else is out of date.
        counts: Open findings per severity; null without a finished run.
        lifecycle: Open findings per lifecycle state; null without a run.
        last_analysis_at: When the latest finished run finished.
        last_school_sync_at: The school snapshot's fetch time.
        analysis_stale: The latest finished run is too old, or there is none.
        calendar_stale: The calendar data the run read is too old, or no
            calendar has ever been collected.
        school_snapshot_stale: The snapshot is too old or missing.
        run_in_flight: A run is queued, or a pipeline (a scheduled collect,
            assess and notify, or a purge) holds the pipeline lock.
        generated_at: When this answer was built.
    """

    status: RunStatusValue
    reasons: list[StatusReason]
    counts: dict[str, int] | None
    lifecycle: dict[str, int] | None
    last_analysis_at: datetime | None
    last_school_sync_at: datetime | None
    analysis_stale: bool
    calendar_stale: bool
    school_snapshot_stale: bool
    run_in_flight: bool
    generated_at: datetime


class AssessmentStartedBody(BaseModel):
    """Body of ``POST /api/v1/assessments`` (202).

    Attributes:
        run_id: The run to poll.
        coalesced: True when a run was already queued or executing and its id
            is returned instead of a new run.
    """

    run_id: str
    coalesced: bool


class AssessmentBody(BaseModel):
    """Body of ``GET /api/v1/assessments/{run_id}``.

    Attributes:
        run_id: The run.
        state: ``queued``, ``running`` or ``finished``.
        trigger: What started the run, once it has begun.
        status: The run status, only when finished; a run that died is
            ``failed``. Null while queued or running.
        counts: Open findings per severity, for a finished run with a row.
        started_at: When it began.
        finished_at: When it finished.
    """

    run_id: str
    state: Literal["queued", "running", "finished"]
    trigger: str | None
    status: RunStatusValue | None
    counts: dict[str, int] | None
    started_at: datetime | None
    finished_at: datetime | None


def _fixed(code: int, detail: str, reason: str | None = None) -> JSONResponse:
    """Build an error answer with a fixed sentence and nothing from the input."""
    return JSONResponse(
        status_code=code,
        content=ErrorBody(detail=detail, code=reason).model_dump(exclude_none=True),
    )


def _status_body(card: StatusCard) -> StatusBody:
    return StatusBody(
        status=card.status.value,
        reasons=list(card.reasons),
        counts=None if card.counts is None else dict(card.counts),
        lifecycle=None if card.lifecycle is None else dict(card.lifecycle),
        last_analysis_at=card.last_analysis_at,
        last_school_sync_at=card.last_school_sync_at,
        analysis_stale=card.analysis_stale,
        calendar_stale=card.calendar_stale,
        school_snapshot_stale=card.school_snapshot_stale,
        run_in_flight=card.run_in_flight,
        generated_at=card.generated_at,
    )


def _assessment_body(run: RunProgress) -> AssessmentBody:
    return AssessmentBody(
        run_id=run.run_id,
        state=run.state,
        trigger=run.trigger,
        status=None if run.status is None else run.status.value,
        counts=None if run.counts is None else dict(run.counts),
        started_at=run.started_at,
        finished_at=run.finished_at,
    )


def _store_failed(event: str, exc: SQLAlchemyError) -> JSONResponse:
    """Log a store error by class only and answer the fixed 503."""
    # A traceback can carry SQL parameters, so only the class name is logged.
    logger.error(event, error=type(exc).__name__)
    return _fixed(status.HTTP_503_SERVICE_UNAVAILABLE, STORE_UNAVAILABLE)


def _start_failed(exc: OnDemandStartError) -> JSONResponse:
    """Log a failed thread start by class only and answer the fixed 503."""
    # The message and the chained cause stay out of the log and the body.
    logger.error("assessments.start_failed", error=type(exc).__name__)
    return _fixed(
        status.HTTP_503_SERVICE_UNAVAILABLE,
        ON_DEMAND_START_FAILED,
        ON_DEMAND_START_FAILED_CODE,
    )


def _card_answer(status_service: StatusService) -> StatusBody | JSONResponse:
    try:
        card = status_service.card()
    except SQLAlchemyError as exc:
        return _store_failed("status.store_failed", exc)
    return _status_body(card)


def _start_answer(
    on_demand: OnDemandService, *, enabled: bool
) -> AssessmentStartedBody | JSONResponse:
    if not enabled:
        return _fixed(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            ON_DEMAND_DISABLED,
            ON_DEMAND_DISABLED_CODE,
        )
    try:
        ticket = on_demand.request()
    except OnDemandStoppedError:
        return _fixed(status.HTTP_503_SERVICE_UNAVAILABLE, STOPPING)
    except OnDemandStartError as exc:
        return _start_failed(exc)
    logger.info(
        "assessments.requested", run_id=ticket.run_id, coalesced=ticket.coalesced
    )
    return AssessmentStartedBody(run_id=ticket.run_id, coalesced=ticket.coalesced)


def _run_answer(
    on_demand: OnDemandService, run_id: str
) -> AssessmentBody | JSONResponse:
    if _RUN_ID.fullmatch(run_id) is None:
        return _fixed(status.HTTP_422_UNPROCESSABLE_CONTENT, INVALID_RUN_ID)
    try:
        run = on_demand.progress(run_id)
    except SQLAlchemyError as exc:
        return _store_failed("assessments.store_failed", exc)
    if run is None:
        return _fixed(status.HTTP_404_NOT_FOUND, RUN_NOT_FOUND)
    return _assessment_body(run)


def register_assessment_routes(
    router: APIRouter,
    status_service: StatusService,
    on_demand: OnDemandService,
    *,
    on_demand_enabled: bool,
) -> None:
    """Register the status and assessment routes on the authenticated HA router.

    Args:
        router: The router from ``build_ha_router`` (token check at router level).
        status_service: Builds the status card.
        on_demand: Starts and reports on-demand runs.
        on_demand_enabled: ``FL_ON_DEMAND_ENABLED``; False makes the POST route
            a fixed 503 that starts nothing.
    """
    store_unavailable: dict[int | str, dict[str, object]] = {
        status.HTTP_503_SERVICE_UNAVAILABLE: {
            "model": ErrorBody,
            "description": "The store is unavailable; retry",
        }
    }
    not_accepted: dict[int | str, dict[str, object]] = {
        status.HTTP_503_SERVICE_UNAVAILABLE: {
            "model": ErrorBody,
            "description": (
                "Not accepted. With code on_demand_disabled the setting is off "
                "and retrying cannot succeed until the owner enables it; "
                "with code on_demand_start_failed no run was started (the "
                "process could not start its thread) and a retry gets a new "
                "run; with no code the service is shutting down, so retry "
                "once it is back"
            ),
        }
    }

    @router.get("/status", response_model=StatusBody, responses=store_unavailable)
    def get_status() -> StatusBody | JSONResponse:
        """Return the family-wide card: status, counts, last times, stale flags."""
        return _card_answer(status_service)

    @router.post(
        "/assessments",
        status_code=status.HTTP_202_ACCEPTED,
        response_model=AssessmentStartedBody,
        responses=not_accepted,
    )
    def post_assessment() -> AssessmentStartedBody | JSONResponse:
        """Start an on-demand run, or join the one already queued or running."""
        return _start_answer(on_demand, enabled=on_demand_enabled)

    @router.get(
        "/assessments/{run_id}",
        response_model=AssessmentBody,
        responses={
            **store_unavailable,
            status.HTTP_404_NOT_FOUND: {
                "model": ErrorBody,
                "description": "Unknown run",
            },
            status.HTTP_422_UNPROCESSABLE_CONTENT: {
                "model": ErrorBody,
                "description": "Not a run id",
            },
        },
    )
    def get_assessment(run_id: str) -> AssessmentBody | JSONResponse:
        """Report whether one run is queued, running or finished, and how it ended."""
        return _run_answer(on_demand, run_id)
