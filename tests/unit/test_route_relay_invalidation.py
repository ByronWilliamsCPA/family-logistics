"""A location change drops its route cache entries and pending requests (ADR 0006).

The invalidation lives in the database (migration 0005), so it covers every
write path; these tests drive the real one, ``RegistryStore.upsert_location``,
plus a delete and a raw statement.
"""

from __future__ import annotations

import dataclasses

import pytest
from fl_relay import CIPHER, HOME_ADDRESS, LOCATIONS, NOW, ZONE, make_engine
from sqlalchemy import text

from family_logistics.db.registry import RegistryStore
from family_logistics.db.route_relay import (
    EnqueueStatus,
    Leg,
    ResultStatus,
    RouteAnswer,
    RouteRelayRepository,
)
from family_logistics.domain.models import SensitiveStr

pytestmark = pytest.mark.unit

FRIDAY = 4
HOME_TO_SCHOOL = Leg("home", "high-school", FRIDAY, 32)
SCHOOL_TO_HOME = Leg("high-school", "home", FRIDAY, 32)
HOME_TO_XC = Leg("home", "xc", FRIDAY, 32)
STUDIO_TO_XC = Leg("studio", "xc", FRIDAY, 40)
PENDING_HOME_SCHOOL = Leg("home", "high-school", FRIDAY, 36)
PENDING_STUDIO_XC = Leg("studio", "xc", FRIDAY, 44)


@pytest.fixture
def engine(tmp_path):
    engine = make_engine(tmp_path)
    yield engine
    engine.dispose()


@pytest.fixture
def repo(engine):
    return RouteRelayRepository(engine)


def answer(repo, leg, seconds):
    assert repo.enqueue(leg, now=NOW) is EnqueueStatus.CREATED
    (request,) = [r for r in repo.pending(now=NOW) if r.leg == leg]
    outcome = repo.record_results(
        [RouteAnswer(request.request_id, seconds)], now=NOW, zone=ZONE
    )
    assert outcome.items[0].status is ResultStatus.ACCEPTED


@pytest.fixture
def seeded(repo):
    for leg in (HOME_TO_SCHOOL, SCHOOL_TO_HOME, HOME_TO_XC, STUDIO_TO_XC):
        answer(repo, leg, 900)
    for leg in (PENDING_HOME_SCHOOL, PENDING_STUDIO_XC):
        assert repo.enqueue(leg, now=NOW) is EnqueueStatus.CREATED
    return repo


def served(repo, leg):
    return repo.lookup(leg, now=NOW) is not None


def pending_legs(repo):
    return {r.leg for r in repo.pending(now=NOW)}


def current(engine, location_id):
    with engine.connect() as connection:
        found = [
            item
            for item in RegistryStore(connection, CIPHER).locations()
            if item.location_id == location_id
        ]
    return found[0]


def edit(engine, location_id, **changes):
    item = dataclasses.replace(current(engine, location_id), **changes)
    with engine.begin() as connection:
        RegistryStore(connection, CIPHER).upsert_location(item)


def test_location_edit_drops_its_route_cache_entries(engine, seeded):
    repo = seeded
    assert all(
        served(repo, leg)
        for leg in (HOME_TO_SCHOOL, SCHOOL_TO_HOME, HOME_TO_XC, STUDIO_TO_XC)
    )

    edit(engine, "high-school", ha_zone_entity_id="zone.school_b")

    # high-school as destination and as origin: both gone, so the next
    # estimate is unknown (ROUTE-010) until Home Assistant posts a fresh result.
    assert not served(repo, HOME_TO_SCHOOL)
    assert not served(repo, SCHOOL_TO_HOME)
    # Its pending requests are dropped too, so a late answer cannot refill it.
    assert PENDING_HOME_SCHOOL not in pending_legs(repo)
    # Another location's entries and requests survive.
    assert served(repo, HOME_TO_XC)
    assert served(repo, STUDIO_TO_XC)
    assert pending_legs(repo) == {PENDING_STUDIO_XC}


def test_a_late_answer_for_a_dropped_request_is_rejected(engine, seeded):
    repo = seeded
    (doomed,) = [r for r in repo.pending(now=NOW) if r.leg == PENDING_HOME_SCHOOL]
    edit(engine, "home", name="Family Home", verified=False)
    outcome = repo.record_results(
        [RouteAnswer(doomed.request_id, 900)], now=NOW, zone=ZONE
    )
    assert outcome.items[0].status is ResultStatus.REJECTED
    assert repo.lookup(PENDING_HOME_SCHOOL, now=NOW) is None


def test_a_corrected_address_drops_entries_for_both_directions(engine, seeded):
    repo = seeded
    edit(engine, "home", address=SensitiveStr("9 Corrected Row, Exampleton"))
    assert not served(repo, HOME_TO_SCHOOL)
    assert not served(repo, SCHOOL_TO_HOME)
    assert not served(repo, HOME_TO_XC)
    assert served(repo, STUDIO_TO_XC)


def test_changed_coordinates_drop_the_entries(engine, seeded):
    repo = seeded
    edit(engine, "xc", coordinates=SensitiveStr("0.0,0.0"))
    assert not served(repo, HOME_TO_XC)
    assert not served(repo, STUDIO_TO_XC)
    assert served(repo, HOME_TO_SCHOOL)


def test_reverifying_a_location_drops_the_entries(engine, seeded):
    repo = seeded
    edit(engine, "high-school", verified=False)
    assert not served(repo, HOME_TO_SCHOOL)
    answer(repo, HOME_TO_SCHOOL, 700)
    edit(engine, "high-school", verified=True)
    assert not served(repo, HOME_TO_SCHOOL)


def test_deleting_a_location_drops_its_entries(engine, seeded):
    repo = seeded
    with engine.begin() as connection:
        connection.execute(text("DELETE FROM location WHERE location_id = 'xc'"))
    assert not served(repo, HOME_TO_XC)
    assert not served(repo, STUDIO_TO_XC)
    assert served(repo, HOME_TO_SCHOOL)
    assert pending_legs(repo) == {PENDING_HOME_SCHOOL}


def test_a_raw_statement_cannot_bypass_the_invalidation(engine, seeded):
    repo = seeded
    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE location SET ha_zone_entity_id = 'zone.elsewhere'"
                " WHERE location_id = 'high-school'"
            )
        )
    assert not served(repo, HOME_TO_SCHOOL)


def test_reapplying_an_unchanged_registry_keeps_the_entries(engine, seeded):
    repo = seeded
    # The seed loader upserts every location; Fernet encrypts differently each
    # time, so an unchanged address must not look like an edit.
    with engine.begin() as connection:
        store = RegistryStore(connection, CIPHER)
        for item in LOCATIONS:
            store.upsert_location(
                dataclasses.replace(item, **_protected(engine, item.location_id))
            )
    assert all(
        served(repo, leg)
        for leg in (HOME_TO_SCHOOL, SCHOOL_TO_HOME, HOME_TO_XC, STUDIO_TO_XC)
    )
    assert pending_legs(repo) == {PENDING_HOME_SCHOOL, PENDING_STUDIO_XC}


def _protected(engine, location_id):
    item = current(engine, location_id)
    return {"address": item.address, "coordinates": item.coordinates}


def test_a_new_address_for_a_location_without_one_counts_as_an_edit(engine, seeded):
    repo = seeded
    assert current(engine, "home").address is not None
    edit(engine, "studio", address=SensitiveStr("5 Moved Row, Exampleton"))
    assert not served(repo, STUDIO_TO_XC)
    assert served(repo, HOME_TO_XC)


def test_the_plaintext_address_is_never_stored_in_the_relay_tables(engine, seeded):
    with engine.connect() as connection:
        cache = connection.execute(text("SELECT * FROM route_cache")).fetchall()
        queue = connection.execute(text("SELECT * FROM route_request")).fetchall()
    assert cache
    assert queue
    assert HOME_ADDRESS not in repr(cache) + repr(queue)
