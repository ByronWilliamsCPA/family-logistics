"""Registry seed: validation, idempotent apply, deactivation, audit, privacy."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
import yaml
from pydantic import SecretStr
from sqlalchemy import text

from family_logistics.core.crypto import AddressCipher, generate_key
from family_logistics.core.database import create_sqlite_engine
from family_logistics.core.exceptions import ConfigurationError
from family_logistics.db import migrations_api
from family_logistics.db.audit import AuditRepository
from family_logistics.db.registry import RegistryStore, load_snapshot
from family_logistics.domain.events import TransportMode
from family_logistics.services import registry_seed
from family_logistics.services.registry_seed import (
    SeedError,
    apply_seed,
    inside_git_worktree,
    load_seed_file,
    parse_seed,
)

EXAMPLE = Path(__file__).resolve().parents[2] / "config" / "registry.example.yaml"
ADDRESS = "100 Example Lane"
TODAY = date(2026, 10, 21)


def base_seed() -> dict[str, object]:
    """A small fictional seed as plain data."""
    return {
        "version": 1,
        "vehicles": [{"id": "family-suv", "name": "family SUV", "seats": 7}],
        "locations": [
            {"id": "home", "name": "Home", "address": ADDRESS, "verified": True},
            {"id": "school", "name": "School", "aliases": ["Central"]},
        ],
        "households": [{"id": "household-e", "label": "Household E"}],
        "people": [
            {"id": "riley", "name": "Riley", "role": "parent", "can_drive": True},
            {"id": "alex", "name": "Alex", "role": "child", "home": "home"},
            {
                "id": "ext-driver-s",
                "name": "Driver S",
                "role": "contact",
                "can_drive": True,
            },
        ],
        "external_passengers": [
            {
                "id": "ext-e",
                "first_name": "Passenger E",
                "household": "household-e",
                "contact": "ext-driver-s",
                "dropoff": "home",
            }
        ],
        "calendars": [
            {"id": "cal-transport", "name": "Transport", "kind": "transport"},
            {"id": "cal-alex", "name": "Alex", "kind": "person", "person": "alex"},
        ],
        "transport_defaults": [
            {
                "person": "alex",
                "weekdays": ["mon", "Wednesday", 4],
                "direction": "to_school",
                "mode": "bike",
                "origin": "home",
                "destination": "school",
            }
        ],
    }


def seed_text(data: dict[str, object]) -> str:
    return yaml.safe_dump(data, sort_keys=False)


@pytest.fixture
def engine(tmp_path):
    engine = create_sqlite_engine(tmp_path / "fl.db")
    migrations_api.upgrade(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def cipher():
    return AddressCipher(SecretStr(generate_key()))


def audit_count(engine) -> int:
    with engine.connect() as connection:
        return int(
            connection.execute(text("SELECT count(*) FROM audit_log")).scalar_one()
        )


@pytest.mark.unit
class TestParsing:
    def test_example_file_is_valid(self):
        seed = load_seed_file(EXAMPLE, allow_repo_path=True)
        assert {p.id for p in seed.people} >= {"morgan", "riley", "alex", "casey"}
        casey = next(p for p in seed.people if p.id == "casey")
        assert casey.monitoring.enabled is False

    def test_weekday_names_and_numbers(self):
        seed = parse_seed(seed_text(base_seed()))
        assert seed.transport_defaults[0].weekdays == frozenset({0, 2, 4})

    def test_weekday_names_are_case_insensitive_and_stripped(self):
        data = base_seed()
        data["transport_defaults"][0]["weekdays"] = [" FRIDAY ", "Sun", "tue"]  # type: ignore[index]
        seed = parse_seed(seed_text(data))
        assert seed.transport_defaults[0].weekdays == frozenset({4, 6, 1})

    @pytest.mark.parametrize(
        "weekday", ["monkey", "friend", "satur", "mo", True, False, 7, -1]
    )
    def test_weekday_prefixes_and_booleans_are_rejected(self, weekday):
        data = base_seed()
        data["transport_defaults"][0]["weekdays"] = [weekday]  # type: ignore[index]
        text_input = seed_text(data)
        with pytest.raises(SeedError, match="weekdays must be"):
            parse_seed(text_input)

    def test_unprintable_character_is_a_seed_error(self):
        with pytest.raises(SeedError, match="not valid YAML"):
            parse_seed("version: 1\x07\n")

    def test_duplicate_nested_key_names_line_only(self):
        text_input = (
            "version: 1\n"
            "people:\n"
            "  - id: riley\n"
            "    name: Riley\n"
            "    role: parent\n"
            "    can_drive: false\n"
            "    can_drive: true\n"
        )
        with pytest.raises(SeedError, match="near line 7") as caught:
            parse_seed(text_input)
        message = str(caught.value)
        assert "can_drive" not in message
        assert "true" not in message

    def test_duplicate_top_level_section_names_line_only(self):
        text_input = (
            "version: 1\n"
            "people:\n"
            "  - {id: riley, name: Riley, role: parent}\n"
            "people:\n"
            "  - {id: alex, name: Alex, role: child}\n"
        )
        with pytest.raises(SeedError, match="near line 4") as caught:
            parse_seed(text_input)
        message = str(caught.value)
        assert "people" not in message
        assert "Alex" not in message

    def test_duplicate_alias_within_a_kind_is_rejected(self):
        data = base_seed()
        data["locations"][0]["aliases"] = ["  central "]  # type: ignore[index]
        text_input = seed_text(data)
        with pytest.raises(SeedError, match="repeats an alias") as caught:
            parse_seed(text_input)
        assert "central" not in str(caught.value).casefold()

    def test_same_alias_in_different_kinds_is_allowed(self):
        data = base_seed()
        data["people"][1]["aliases"] = ["Central"]  # type: ignore[index]
        seed = parse_seed(seed_text(data))
        assert seed.people[1].aliases == ("Central",)

    def test_example_inside_repo_is_refused(self):
        with pytest.raises(SeedError, match="inside a git working tree"):
            load_seed_file(EXAMPLE)

    def test_missing_file(self, tmp_path):
        with pytest.raises(SeedError, match="not found"):
            load_seed_file(tmp_path / "registry.yaml")

    def test_oversized_file(self, tmp_path, monkeypatch):
        path = tmp_path / "registry.yaml"
        path.write_text(seed_text(base_seed()), encoding="utf-8")
        monkeypatch.setattr(registry_seed, "MAX_SEED_BYTES", 10)
        with pytest.raises(SeedError, match="larger than"):
            load_seed_file(path)

    def test_git_detection(self, tmp_path):
        repo = tmp_path / "repo"
        (repo / ".git").mkdir(parents=True)
        assert inside_git_worktree(repo / "config" / "registry.yaml") is True
        assert inside_git_worktree(tmp_path / "outside.yaml") is False

    def test_malformed_yaml_names_line_only(self):
        with pytest.raises(SeedError, match=r"near line \d") as caught:
            parse_seed("version: 1\npeople: [unclosed\n")
        assert "unclosed" not in str(caught.value)

    @pytest.mark.parametrize(
        ("mutate", "message"),
        [
            (lambda d: d.update(colour="blue"), "Extra inputs"),
            (lambda d: d["people"].append(dict(d["people"][1])), "duplicate id alex"),
            (
                lambda d: d["people"][1].update(home="nowhere"),
                "unknown location nowhere",
            ),
            (
                lambda d: d["external_passengers"][0].update(household="x"),
                "unknown household x",
            ),
            (lambda d: d["calendars"][1].pop("person"), "person calendar needs"),
            (lambda d: d["people"][0].update(id="Bad Id"), "people.0.id"),
            (
                lambda d: d["transport_defaults"][0].update(weekdays=["someday"]),
                "weekdays must be",
            ),
            (
                lambda d: d["transport_defaults"][0].update(mode="parent"),
                "mode parent needs a driver",
            ),
            (
                lambda d: d["transport_defaults"][0].update(driver="riley"),
                "mode bike must not name a driver",
            ),
            (
                lambda d: d["transport_defaults"][0].update(
                    mode="carpool", driver="alex"
                ),
                "driver alex is not authorized",
            ),
            (
                lambda d: d["transport_defaults"][0].update(
                    effective_from="2026-09-01", effective_to="2026-08-01"
                ),
                "effective_to is before",
            ),
            (
                lambda d: d["people"][1].update(
                    monitoring={"start": "2026-09-01", "end": "2026-08-01"}
                ),
                "monitoring end is before start",
            ),
            (lambda d: d["locations"][0].update(coordinates="north"), "lat,lon"),
            (lambda d: d.update(version=2), "version"),
        ],
    )
    def test_invalid_seeds(self, mutate, message):
        data = base_seed()
        mutate(data)
        with pytest.raises(SeedError, match=message) as caught:
            parse_seed(seed_text(data))
        assert ADDRESS not in str(caught.value)

    def test_rejected_secret_is_not_echoed(self):
        data = base_seed()
        data["locations"][0]["coordinates"] = "secret-place-9"  # type: ignore[index]
        with pytest.raises(SeedError) as caught:
            parse_seed(seed_text(data))
        assert "secret-place-9" not in str(caught.value)
        assert "secret-place-9" not in repr(caught.value.details)


@pytest.mark.unit
class TestApply:
    def test_first_load_creates_then_second_load_is_unchanged(self, engine, cipher):
        seed = parse_seed(seed_text(base_seed()))
        first = apply_seed(engine, seed, cipher=cipher, today=TODAY)
        assert first.counts() == {
            "create": 11,
            "update": 0,
            "deactivate": 0,
            "unchanged": 0,
            "retained": 0,
        }
        assert audit_count(engine) == 11
        second = apply_seed(engine, seed, cipher=cipher, today=TODAY)
        assert second.changes == ()
        assert second.unchanged == 11
        assert audit_count(engine) == 11

    def test_snapshot_after_load(self, engine, cipher):
        apply_seed(
            engine, parse_seed(seed_text(base_seed())), cipher=cipher, today=TODAY
        )
        snapshot = load_snapshot(engine, cipher)
        alex = snapshot.registry.resolve_family_child("Alex")
        assert alex is not None
        assert alex.home_location_id == "home"
        [bike] = snapshot.transport_defaults
        assert bike.mode is TransportMode.BIKE
        assert [c.calendar_id for c in snapshot.calendar_sources] == [
            "cal-alex",
            "cal-transport",
        ]
        school = snapshot.location("school")
        assert school is not None
        assert school.aliases == ("Central",)

    def test_alias_moves_between_locations(self, engine, cipher):
        apply_seed(
            engine, parse_seed(seed_text(base_seed())), cipher=cipher, today=TODAY
        )
        data = base_seed()
        # "home" is written before "school", so without clearing first the
        # alias would still belong to "school" when "home" claims it.
        data["locations"][0]["aliases"] = ["central"]  # type: ignore[index]
        data["locations"][1]["aliases"] = []  # type: ignore[index]
        plan = apply_seed(
            engine, parse_seed(seed_text(data)), cipher=cipher, today=TODAY
        )
        assert {(c.object_ref, c.action) for c in plan.changes} == {
            ("location:home", "update"),
            ("location:school", "update"),
        }
        snapshot = load_snapshot(engine, cipher)
        home = snapshot.location("home")
        school = snapshot.location("school")
        assert home is not None
        assert school is not None
        assert home.aliases == ("central",)
        assert school.aliases == ()

    def test_audit_failure_rolls_back_registry_changes(
        self, engine, cipher, monkeypatch
    ):
        def failing_record(*_args: object, **_kwargs: object) -> None:
            raise RuntimeError("audit unavailable")

        monkeypatch.setattr(registry_seed, "record_on", failing_record)
        seed = parse_seed(seed_text(base_seed()))
        with pytest.raises(RuntimeError, match="audit unavailable"):
            apply_seed(engine, seed, cipher=cipher, today=TODAY)
        with engine.connect() as connection:
            assert RegistryStore(connection).people() == []
        assert audit_count(engine) == 0

    def test_dry_run_writes_nothing(self, engine, cipher):
        plan = apply_seed(
            engine, parse_seed(seed_text(base_seed())), cipher=cipher, dry_run=True
        )
        assert plan.counts()["create"] == 11
        with engine.connect() as connection:
            assert RegistryStore(connection).people() == []
        assert audit_count(engine) == 0

    def test_removed_entries_are_deactivated_or_retained(self, engine, cipher):
        apply_seed(
            engine, parse_seed(seed_text(base_seed())), cipher=cipher, today=TODAY
        )
        data = base_seed()
        data["people"] = [p for p in data["people"] if p["id"] != "riley"]  # type: ignore[index, union-attr]
        data["external_passengers"] = []
        data["households"] = []
        data["calendars"] = data["calendars"][:1]  # type: ignore[index]
        data["transport_defaults"] = []
        plan = apply_seed(
            engine, parse_seed(seed_text(data)), cipher=cipher, today=TODAY
        )
        actions = {c.object_ref: c.action for c in plan.changes}
        assert actions == {
            "person:riley": "deactivate",
            "passenger:ext-e": "deactivate",
            "calendar:cal-alex": "deactivate",
            "transport_defaults:alex": "update",
        }
        assert plan.retained == ("household:household-e",)
        with engine.connect() as connection:
            store = RegistryStore(connection, cipher)
            riley = next(p for p in store.people() if p.person_id == "riley")
            assert riley.active is False
            assert store.passenger_inactive_since() == {"ext-e": TODAY}
            assert [
                c.calendar_id for c in store.calendar_sources(enabled_only=True)
            ] == ["cal-transport"]
            assert store.transport_defaults() == []

    def test_address_change_is_an_update_without_plaintext(self, engine, cipher):
        apply_seed(
            engine, parse_seed(seed_text(base_seed())), cipher=cipher, today=TODAY
        )
        data = base_seed()
        data["locations"][0]["address"] = "300 Example Court"  # type: ignore[index]
        plan = apply_seed(
            engine, parse_seed(seed_text(data)), cipher=cipher, today=TODAY
        )
        assert [(c.object_ref, c.action) for c in plan.changes] == [
            ("location:home", "update")
        ]
        [entry] = AuditRepository(engine).entries_for("location:home")[1:]
        assert entry.action == "seed-update"
        assert entry.prior_hash != entry.new_hash
        with engine.connect() as connection:
            dump = "".join(
                str(row)
                for table in ("location", "audit_log")
                for row in connection.execute(text(f"SELECT * FROM {table}"))  # noqa: S608 - fixed names
            )
        assert "Example Court" not in dump
        assert ADDRESS not in dump
        assert "Example Court" not in repr(plan)

    def test_protected_seed_needs_a_key(self, engine):
        seed = parse_seed(seed_text(base_seed()))
        with pytest.raises(ConfigurationError, match="FL_DATA_KEY"):
            apply_seed(engine, seed, cipher=None, dry_run=True)

    def test_protected_registry_needs_a_key(self, engine, cipher):
        apply_seed(
            engine, parse_seed(seed_text(base_seed())), cipher=cipher, today=TODAY
        )
        data = base_seed()
        data["locations"][0].pop("address")  # type: ignore[index]
        seed = parse_seed(seed_text(data))
        with pytest.raises(ConfigurationError, match="registry holds protected"):
            apply_seed(engine, seed, cipher=None)

    def test_seed_without_addresses_needs_no_key(self, engine):
        data = base_seed()
        data["locations"][0].pop("address")  # type: ignore[index]
        plan = apply_seed(engine, parse_seed(seed_text(data)), cipher=None, today=TODAY)
        assert plan.counts()["create"] == 11

    def test_reactivated_passenger_keeps_history_until_active(self, engine, cipher):
        data = base_seed()
        data["external_passengers"][0]["active"] = False  # type: ignore[index]
        apply_seed(engine, parse_seed(seed_text(data)), cipher=cipher, today=TODAY)
        later = date(2026, 12, 1)
        apply_seed(engine, parse_seed(seed_text(data)), cipher=cipher, today=later)
        with engine.connect() as connection:
            assert RegistryStore(connection).passenger_inactive_since() == {
                "ext-e": TODAY
            }
        apply_seed(
            engine, parse_seed(seed_text(base_seed())), cipher=cipher, today=later
        )
        with engine.connect() as connection:
            assert RegistryStore(connection).passenger_inactive_since() == {}
