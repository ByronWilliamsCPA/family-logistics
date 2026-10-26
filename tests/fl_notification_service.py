"""Shared builders for the notification service tests (ADR 0008, T-svc-*).

Everything here is fictional: the fictional household, a synthetic ride and
findings whose free text is a sentinel that must never reach a push, a log
event or an audit row.
"""

from __future__ import annotations

import base64
import json
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import httpx
import structlog
from fl_factories import FRIDAY_START, notes, trip
from fl_service import make_settings, seed_registry
from pydantic import SecretStr
from structlog.testing import capture_logs

from family_logistics.adapters.apprise import client as client_module
from family_logistics.adapters.apprise.client import AppriseClient
from family_logistics.db import notifications as repo_module
from family_logistics.domain.engine import AssessmentResult
from family_logistics.domain.lifecycle import Lifecycle, TrackedFinding
from family_logistics.domain.models import RunStatus, Severity
from family_logistics.domain.notification_subjects import RideIndex
from family_logistics.domain.rules import make_finding
from family_logistics.services import notifications as service_module
from family_logistics.services.assessments import RunOutcome
from family_logistics.services.notifications import NotificationService

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    import pytest

    from family_logistics.core.config import Settings
    from family_logistics.db.audit import AuditRepository

APPRISE_URL = "http://apprise.example.invalid"
SENTINEL = "SENTINEL-FREE-TEXT-MUST-NEVER-LEAK"
"""Free text carried by every finding the tests build; it is never rendered."""
SUGGESTION = "SENTINEL-SUGGESTION-MUST-NEVER-LEAK"

RIDE = trip(notes(driver="Riley"), uid="ride")
RIDE_KEY = RIDE.key.as_str()
RIDES = RideIndex.build(current=(RIDE,), previous=(), unparsed=())
EMPTY_RIDES = RideIndex.build(current=(), previous=(), unparsed=())

NOW = FRIDAY_START - timedelta(hours=1)
"""14:30 local, one hour before the ride: imminent, outside quiet hours."""
FAR = FRIDAY_START - timedelta(hours=9)
"""06:30 local: the ride is 9 hours away (not imminent), quiet hours just ended."""
NIGHT = datetime(2026, 10, 24, 6, 30, tzinfo=UTC)
"""23:30 local on 23 October: inside quiet hours."""
MORNING = datetime(2026, 10, 24, 13, 10, tzinfo=UTC)
"""06:10 local on 24 October: quiet hours are over."""
NIGHT_RIDE = trip(
    notes(driver="Riley"),
    uid="night-ride",
    start=datetime(2026, 10, 24, 7, 0, tzinfo=UTC),
    end=datetime(2026, 10, 24, 7, 45, tzinfo=UTC),
)
NIGHT_RIDE_KEY = NIGHT_RIDE.key.as_str()
NIGHT_RIDES = RideIndex.build(current=(NIGHT_RIDE,), previous=(), unparsed=())


def audit_key_text() -> str:
    """Return a valid audit key built at run time (no secret literal)."""
    return base64.urlsafe_b64encode(bytes(range(32))).decode()


class Clock:
    """A settable clock; ``sleep`` records the wait and advances it."""

    def __init__(self, now: datetime = NOW) -> None:
        self.now = now
        self.sleeps: list[float] = []

    def __call__(self) -> datetime:
        return self.now

    def advance(self, delta: timedelta) -> None:
        self.now += delta

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += timedelta(seconds=seconds)


@dataclass
class Posts:
    """An Apprise transport: records each payload and answers from a script.

    Attributes:
        script: Statuses answered in order; an exhausted script answers 200.
            An ``int`` is a status, an ``Exception`` is raised by the transport.
        payloads: The JSON bodies received, in order.
    """

    script: list[int | Exception] = field(default_factory=list)
    payloads: list[dict[str, str]] = field(default_factory=list)
    on_post: Callable[[], None] | None = None

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.payloads.append(json.loads(request.content))
        if self.on_post is not None:
            self.on_post()
        step = self.script.pop(0) if self.script else 200
        if isinstance(step, Exception):
            raise step
        return httpx.Response(step)

    @property
    def tags(self) -> list[str]:
        return [p["tag"] for p in self.payloads]

    @property
    def bodies(self) -> list[str]:
        return [p["body"] for p in self.payloads]


def make_service(
    engine,
    *,
    posts: Posts | None = None,
    clock: Clock | None = None,
    mode: str = "shadow",
    audit: bool = False,
    configured: bool = True,
    **overrides: object,
) -> tuple[NotificationService, Posts, Clock]:
    """Build a service over a fake Apprise transport and a settable clock."""
    posts = posts if posts is not None else Posts()
    clock = clock if clock is not None else Clock()
    values: dict[str, object] = {"notification_mode": mode}
    if configured:
        values["apprise_url"] = APPRISE_URL
    if audit:
        values["audit_key"] = SecretStr(audit_key_text())
    settings: Settings = make_settings(**(values | overrides))
    client = AppriseClient.from_settings(settings, transport=httpx.MockTransport(posts))
    service = NotificationService(
        engine, settings, client, clock=clock, sleep=clock.sleep
    )
    return service, posts, clock


def audit_of(service: NotificationService) -> AuditRepository | None:
    """Return the service's audit repository (a test reads what it wrote)."""
    return service._audit


def finding(
    rule: str = "TIME-007",
    keys: tuple[str, ...] = (RIDE_KEY, "route"),
    severity: Severity = Severity.CRITICAL,
):
    """Build a finding whose free text and suggestion are sentinels."""
    return make_finding(rule, SENTINEL, keys, severity=severity, suggestion=SUGGESTION)


def result_of(
    *findings,
    status: RunStatus = RunStatus.ATTENTION_REQUIRED,
    lifecycle: Lifecycle = Lifecycle.NEW,
    evaluated_at: datetime = NOW,
) -> AssessmentResult:
    """Build a completed result over ``findings``."""
    tracked = tuple(
        f if isinstance(f, TrackedFinding) else TrackedFinding(f, lifecycle)
        for f in findings
    )
    return AssessmentResult(status, tracked, (), (), evaluated_at)


def outcome(
    result: AssessmentResult,
    run_id: str = "run-1",
    trigger: str = "incremental",
    rides: RideIndex | None = RIDES,
) -> RunOutcome:
    """Wrap a result as a stored run."""
    return RunOutcome(run_id, result, trigger, rides)


def seeded(engine) -> None:
    """Store the fictional household so the name directory has rows."""
    seed_registry(engine)


def go(
    service: NotificationService,
    clock: Clock,
    at: datetime,
    *findings,
    run_id: str = "run-1",
    status: RunStatus = RunStatus.ATTENTION_REQUIRED,
    trigger: str = "incremental",
    lifecycle: Lifecycle = Lifecycle.NEW,
    rides: RideIndex | None = RIDES,
):
    """Set the clock to ``at`` and dispatch a run evaluated at ``at``."""
    clock.now = at
    result = result_of(*findings, status=status, lifecycle=lifecycle, evaluated_at=at)
    return service.dispatch(outcome(result, run_id, trigger, rides))


def texts(posts: Posts) -> str:
    """Return every title and body posted, joined (for substring checks)."""
    return "\n".join(f"{p['title']}\n{p['body']}" for p in posts.payloads)


@contextmanager
def captured_events(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[dict]]:
    """Capture every structured event of the service, repository and client.

    Other tests may have configured structlog with ``cache_logger_on_first_use``,
    so each module's logger is replaced by a fresh one for the test.
    """
    with capture_logs() as events:
        for module in (service_module, repo_module, client_module):
            monkeypatch.setattr(module, "logger", structlog.get_logger(module.__name__))
        yield events
