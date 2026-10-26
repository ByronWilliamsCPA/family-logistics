"""FL_HA_SCHOOLS: which Home Assistant calendars describe which school."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from family_logistics.core.config import (
    MAX_SCHOOL_CALENDARS,
    HaSchool,
    Settings,
    load_settings,
)
from family_logistics.core.exceptions import ConfigurationError

pytestmark = pytest.mark.unit

MIDDLE = {
    "location_id": "middle-school",
    "person_ids": ["sam"],
    "bell_calendars": ["calendar.middle_school_bell"],
    "district_calendars": ["calendar.district"],
}
HIGH = {
    "location_id": "high-school",
    "person_ids": ["alex", "jordan"],
    "bell_calendars": ["calendar.high_school_bell"],
    "district_calendars": ["calendar.district"],
}


def build(schools: object) -> Settings:
    return Settings.model_validate({"ha_schools": schools})


class TestDefaults:
    def test_no_school_is_configured_by_default(self):
        assert Settings().ha_schools == ()

    def test_environment_json_is_parsed(self, monkeypatch):
        monkeypatch.setenv("FL_HA_SCHOOLS", json.dumps([MIDDLE, HIGH]))
        settings = Settings()
        assert [s.location_id for s in settings.ha_schools] == [
            "middle-school",
            "high-school",
        ]
        assert settings.ha_schools[1].person_ids == ("alex", "jordan")

    def test_blank_environment_value_means_none(self, monkeypatch):
        monkeypatch.setenv("FL_HA_SCHOOLS", "")
        assert Settings().ha_schools == ()


class TestHaSchool:
    def test_entities_lists_every_calendar_with_its_role(self):
        school = HaSchool.model_validate(MIDDLE)
        assert school.entities() == {
            "calendar.middle_school_bell": "bell",
            "calendar.district": "district",
        }

    def test_district_only_school_is_allowed(self):
        school = HaSchool.model_validate(MIDDLE | {"bell_calendars": []})
        assert school.bell_calendars == ()

    def test_school_is_immutable(self):
        school = HaSchool.model_validate(MIDDLE)
        field = "location_id"
        with pytest.raises(ValidationError):
            setattr(school, field, "other")

    def test_a_person_is_listed_once_per_school(self):
        with pytest.raises(ValidationError, match="listed twice"):
            HaSchool.model_validate(MIDDLE | {"person_ids": ["sam", "sam"]})

    @pytest.mark.parametrize("role", ["bell_calendars", "district_calendars"])
    def test_calendar_count_is_bounded(self, role):
        at_limit = [f"calendar.c{i}" for i in range(MAX_SCHOOL_CALENDARS)]
        other = "district_calendars" if role == "bell_calendars" else "bell_calendars"
        base = MIDDLE | {other: []}
        assert len(HaSchool.model_validate(base | {role: at_limit}).entities()) == (
            MAX_SCHOOL_CALENDARS
        )
        with pytest.raises(ValidationError):
            HaSchool.model_validate(base | {role: [*at_limit, "calendar.extra"]})


class TestRejected:
    @pytest.mark.parametrize(
        "school",
        [
            MIDDLE | {"person_ids": []},
            MIDDLE | {"bell_calendars": [], "district_calendars": []},
            MIDDLE | {"bell_calendars": ["sensor.phone_location"]},
            MIDDLE | {"district_calendars": ["person.sam"]},
            MIDDLE | {"bell_calendars": ["device_tracker.sam_phone"]},
            MIDDLE | {"bell_calendars": ["calendar.District"]},
            MIDDLE | {"district_calendars": ["calendar.middle_school_bell"]},
            MIDDLE | {"person_ids": ["sam", "sam"]},
            MIDDLE | {"location_id": ""},
            MIDDLE | {"location_id": "a b"},
            MIDDLE | {"extra": True},
            MIDDLE | {"person_ids": [f"p{i}" for i in range(17)]},
        ],
        ids=[
            "no-person",
            "no-calendar",
            "sensor-entity",
            "person-entity",
            "device-tracker",
            "upper-case",
            "bell-and-district",
            "duplicate-person",
            "empty-location",
            "space-in-location",
            "unknown-key",
            "too-many-people",
        ],
    )
    def test_bad_school_is_rejected(self, school):
        with pytest.raises(ValidationError):
            build([school])

    def test_one_location_is_configured_once(self):
        with pytest.raises(ValidationError, match="location"):
            build([MIDDLE, MIDDLE | {"person_ids": ["alex"]}])

    def test_a_person_belongs_to_one_school(self):
        with pytest.raises(ValidationError, match="person"):
            build([MIDDLE, HIGH | {"person_ids": ["sam"]}])

    def test_a_bell_calendar_belongs_to_one_school(self):
        shared = HIGH | {"bell_calendars": MIDDLE["bell_calendars"]}
        with pytest.raises(ValidationError, match="bell calendar"):
            build([MIDDLE, shared])

    def test_a_bell_calendar_is_not_another_schools_district_calendar(self):
        other = HIGH | {"district_calendars": MIDDLE["bell_calendars"]}
        with pytest.raises(ValidationError, match="bell calendar"):
            build([MIDDLE, other])

    def test_a_district_calendar_may_serve_several_schools(self):
        assert len(build([MIDDLE, HIGH]).ha_schools) == 2

    def test_too_many_schools(self):
        schools = [
            MIDDLE | {"location_id": f"school-{i}", "person_ids": [f"p{i}"]}
            for i in range(9)
        ]
        with pytest.raises(ValidationError):
            build(schools)

    def test_errors_never_echo_the_value(self, monkeypatch):
        unechoed_calendar = "calendar.Private_Value_Not_Echoed"
        monkeypatch.setenv(
            "FL_HA_SCHOOLS",
            json.dumps([MIDDLE | {"bell_calendars": [unechoed_calendar]}]),
        )
        with pytest.raises(ConfigurationError) as caught:
            load_settings()
        assert "Private_Value_Not_Echoed" not in str(caught.value)
        assert "FL_HA_SCHOOLS" in str(caught.value)

    def test_malformed_json_is_a_configuration_error(self, monkeypatch):
        monkeypatch.setenv("FL_HA_SCHOOLS", "[{not json Private_Value")
        with pytest.raises(ConfigurationError) as caught:
            load_settings()
        assert "Private_Value" not in str(caught.value)
        assert "FL_HA_SCHOOLS" in str(caught.value)
        assert "not valid JSON" in str(caught.value)

    def test_repeated_json_key_is_rejected_not_dropped(self, monkeypatch):
        # json.loads alone would keep only ["alex"] and drop Sam silently.
        monkeypatch.setenv(
            "FL_HA_SCHOOLS",
            '[{"location_id": "middle-school", "person_ids": ["sam"], '
            '"person_ids": ["alex"], "bell_calendars": ["calendar.bell"]}]',
        )
        with pytest.raises(ConfigurationError) as caught:
            load_settings()
        assert "FL_HA_SCHOOLS" in str(caught.value)
        assert "repeats a key" in str(caught.value)
        assert "alex" not in str(caught.value)

    def test_repeated_key_in_a_later_school_is_rejected(self, monkeypatch):
        later = '{"location_id": "a", "location_id": "b", "person_ids": ["x"]}'
        monkeypatch.setenv("FL_HA_SCHOOLS", f"[{json.dumps(MIDDLE)}, {later}]")
        with pytest.raises(ConfigurationError, match="repeats a key"):
            load_settings()

    def test_blank_text_passed_directly_means_none(self):
        assert Settings.model_validate({"ha_schools": "  "}).ha_schools == ()
