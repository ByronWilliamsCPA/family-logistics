"""Canary: no household text leaves the service on any surface (ADR 0008, D2, D12).

A fake end-to-end dispatch runs over a real database, the real client on a mock
transport and the real audit repository. Every finding, ride and error carries a
sentinel; afterwards none of them may appear in an Apprise payload, a log event
or an audit row. Family first names are allowed in a push (D2) but never in a
log event or an audit row; an external child's name never appears anywhere.
"""

from __future__ import annotations

import json
import re
from datetime import timedelta

import httpx
import pytest
from fl_factories import CALENDAR, notes, trip
from fl_notification_service import (
    EMPTY_RIDES,
    MORNING,
    NIGHT,
    NOW,
    Clock,
    Posts,
    captured_events,
    finding,
    go,
    make_service,
    seeded,
)
from fl_notification_state import make_engine
from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from family_logistics.db import schema
from family_logistics.domain.lifecycle import Lifecycle
from family_logistics.domain.models import RunStatus, Severity
from family_logistics.domain.notification_subjects import RideIndex
from family_logistics.services import notifications as service_module
from family_logistics.services.notifications import NotificationStateError

pytestmark = pytest.mark.unit

NOTE_SENTINEL = "SENTINEL-NOTE-MUST-NEVER-LEAK"
ERROR_SENTINEL = "SENTINEL-ERROR-MUST-NEVER-LEAK"
CANARY_UID = "uid-canary-7731"
PLACE = "Music Studio"

CANARY_RIDE = trip(
    notes(driver="Riley", free=NOTE_SENTINEL, pickup=PLACE), uid=CANARY_UID
)
RIDE_KEY = CANARY_RIDE.key.as_str()
RIDES = RideIndex.build(current=(CANARY_RIDE,), previous=(), unparsed=())
URGENT_KEYS = (RIDE_KEY, "passenger:ext-e", "destination")
NOTICE_KEYS = (RIDE_KEY, "passenger:sam", "driver:morgan")
EXTERNAL_NAMES = ("Passenger E", "Passenger R", "Driver S")
FAMILY_NAMES = ("Morgan", "Riley", "Alex", "Sam", "Jordan", "Casey")
FORBIDDEN = (
    "SENTINEL",
    CALENDAR,
    CANARY_UID,
    PLACE,
    RIDE_KEY,
    "passenger:ext-e",
    "passenger:sam",
    "driver:morgan",
    "person:sam",
    "from:a",
    "to:b",
    *EXTERNAL_NAMES,
)
EDGE_EVENTS = (
    "notification.claim_skipped",
    "notification.suppressed",
    "notification.stale_outcome",
    "notification.render_failed",
    "notification.audit_failed",
    "notification.state_write_failed",
    "notification.subject_unresolved",
)


def locked(*args, **kwargs):
    raise OperationalError("update", {}, Exception(ERROR_SENTINEL))


def refused(*args, **kwargs):
    raise ValueError(ERROR_SENTINEL)


def edge_scenario(engine, mode, monkeypatch, recorded):
    """Reach every log event the main scenario does not (all fixed fields).

    Quiet hours (suppressed), a duplicate (claim_skipped), an older outcome
    (stale_outcome), a refused render, a failed audit write, an unresolved
    subject and a failed state write. Every error carries the error sentinel.

    Returns:
        The payloads posted to Apprise.
    """
    posts = Posts()
    clock = Clock()
    service, _, _ = make_service(
        engine, posts=posts, clock=clock, mode=mode, audit=True
    )
    audit = service._audit
    real_record = audit.record

    def spy(**kwargs):
        recorded.append(kwargs)
        return real_record(**kwargs)

    monkeypatch.setattr(audit, "record", spy)
    notice = finding("PASS-003", NOTICE_KEYS, Severity.WARNING)
    other = finding("CONF-008", (RIDE_KEY, "k-other"), Severity.WARNING)
    third = finding("CONF-008", (RIDE_KEY, "k-third"), Severity.WARNING)
    coverage = finding(
        "RIDE-005", ("person:sam", "day:2026-10-23", "from:a", "to:b"), Severity.WARNING
    )
    step = timedelta(minutes=5)
    go(service, clock, NIGHT, notice, run_id="e1", rides=RIDES)
    go(service, clock, MORNING, notice, run_id="e2", rides=RIDES)
    go(
        service,
        clock,
        MORNING + step,
        notice,
        run_id="e3",
        lifecycle=Lifecycle.ONGOING,
        rides=RIDES,
    )
    go(service, clock, MORNING - step * 12, notice, run_id="e0", rides=RIDES)
    real_render = service_module.render
    monkeypatch.setattr(service_module, "render", refused)
    go(service, clock, MORNING + step * 2, other, run_id="e4", rides=RIDES)
    monkeypatch.setattr(service_module, "render", real_render)
    monkeypatch.setattr(audit, "record", locked)
    go(service, clock, MORNING + step * 3, third, run_id="e5", rides=RIDES)
    monkeypatch.setattr(audit, "record", spy)
    go(service, clock, MORNING + step * 4, coverage, run_id="e6", rides=EMPTY_RIDES)
    monkeypatch.setattr(service._repo, "complete", locked)
    with pytest.raises(NotificationStateError):
        go(service, clock, MORNING + step * 5, other, run_id="e7", rides=RIDES)
    return posts.payloads


@pytest.fixture(params=["shadow", "production"])
def surfaces(request, tmp_path, monkeypatch):
    """Run the scenarios in one mode; return payload, log and audit text."""
    engine = make_engine(tmp_path)
    seeded(engine)
    posts = Posts([httpx.ConnectError(ERROR_SENTINEL), 500, 200])
    clock = Clock()
    service, _, _ = make_service(
        engine, posts=posts, clock=clock, mode=request.param, audit=True
    )
    recorded: list[dict] = []
    audit = service._audit
    real_record = audit.record

    def spy(**kwargs):
        recorded.append(kwargs)
        return real_record(**kwargs)

    monkeypatch.setattr(audit, "record", spy)

    def broken_names(built):
        raise OperationalError("select", {}, Exception(ERROR_SENTINEL))

    urgent = finding("DEST-004", URGENT_KEYS)
    notice = finding("PASS-003", NOTICE_KEYS, Severity.WARNING)
    real_names = service_module.load_name_rows
    with captured_events(monkeypatch) as events:
        monkeypatch.setattr(service_module, "load_name_rows", broken_names)
        go(service, clock, NOW, urgent, notice, rides=RIDES, trigger="daily")
        monkeypatch.setattr(service_module, "load_name_rows", real_names)
        later = NOW + timedelta(minutes=10)
        go(
            service,
            clock,
            later,
            urgent,
            notice,
            run_id="run-2",
            status=RunStatus.INCOMPLETE,
            trigger="weekly",
            rides=RIDES,
        )
        service.dispatch_unavailable("daily")
        (tmp_path / "edge").mkdir()
        edge_engine = make_engine(tmp_path / "edge")
        seeded(edge_engine)
        edge_payloads = edge_scenario(edge_engine, request.param, monkeypatch, recorded)
    with engine.connect() as connection:
        rows = connection.execute(select(schema.audit_log)).all()
    with edge_engine.connect() as connection:
        rows += connection.execute(select(schema.audit_log)).all()
    engine.dispose()
    edge_engine.dispose()
    payloads = posts.payloads + edge_payloads
    return {
        "mode": request.param,
        "payload": "\n".join(f"{p['title']}\n{p['body']}" for p in payloads),
        "tags": [p["tag"] for p in payloads],
        "logs": json.dumps(events, default=str),
        "audit": json.dumps(recorded, default=str) + repr(rows),
        "audit_rows": len(rows),
    }


class TestCanary:
    def test_the_scenario_sent_pushes_and_wrote_audit_rows(self, surfaces):
        assert surfaces["payload"]
        assert surfaces["audit_rows"] > 0
        assert "notification.names_unavailable" in surfaces["logs"]
        assert "notification.delivery_failed" in surfaces["logs"]

    @pytest.mark.parametrize("event", EDGE_EVENTS)
    def test_the_scenarios_reach_every_edge_event(self, surfaces, event):
        assert event in surfaces["logs"]

    @pytest.mark.parametrize("surface", ["payload", "logs", "audit"])
    def test_no_finding_text_key_uid_calendar_note_place_or_error_text(
        self, surfaces, surface
    ):
        text = surfaces[surface]
        assert [word for word in FORBIDDEN if word in text] == []

    @pytest.mark.parametrize("surface", ["logs", "audit"])
    def test_no_name_in_a_log_event_or_audit_row(self, surfaces, surface):
        text = surfaces[surface]
        assert [n for n in FAMILY_NAMES if re.search(rf"\b{n}\b", text)] == []

    def test_a_push_names_no_external_child_and_only_masks(self, surfaces):
        assert (
            re.search(r"\bPassenger [A-Z]\b|\bDriver S\b", surfaces["payload"]) is None
        )

    def test_tags_stay_within_the_mode(self, surfaces):
        allowed = (
            {"fl-shadow"}
            if surfaces["mode"] == "shadow"
            else {"fl-urgent", "fl-warning", "fl-status", "fl-report", "fl-ops"}
        )
        assert set(surfaces["tags"]) <= allowed
