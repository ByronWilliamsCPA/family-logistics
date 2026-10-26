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
from family_logistics.domain.models import RunStatus, Severity
from family_logistics.domain.notification_subjects import RideIndex
from family_logistics.services import notifications as service_module

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
    *EXTERNAL_NAMES,
)


@pytest.fixture(params=["shadow", "production"])
def surfaces(request, tmp_path, monkeypatch):
    """Run the scenario in one mode; return payload, log and audit text."""
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
    with engine.connect() as connection:
        rows = connection.execute(select(schema.audit_log)).all()
    engine.dispose()
    return {
        "mode": request.param,
        "payload": "\n".join(f"{p['title']}\n{p['body']}" for p in posts.payloads),
        "tags": posts.tags,
        "logs": json.dumps(events, default=str),
        "audit": json.dumps(recorded, default=str) + repr(rows),
        "audit_rows": len(rows),
    }


class TestCanary:
    def test_the_scenario_sent_pushes_and_wrote_audit_rows(self, surfaces):
        assert surfaces["payload"]
        assert surfaces["audit_rows"] > 0
        assert "notification.render_failed" not in surfaces["logs"]
        assert "notification.names_unavailable" in surfaces["logs"]
        assert "notification.delivery_failed" in surfaces["logs"]

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
