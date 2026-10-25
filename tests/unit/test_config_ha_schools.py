"""FL_HA_SCHOOLS: which Home Assistant calendars describe which school."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from family_logistics.core.config import HaSchool, Settings, load_settings
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

    def test_too_many_schools(self):
        schools = [
            MIDDLE | {"location_id": f"school-{i}", "person_ids": [f"p{i}"]}
            for i in range(9)
        ]
        with pytest.raises(ValidationError):
            build(schools)

    def test_errors_never_echo_the_value(self, monkeypatch):
        secret_looking = "calendar.Private_Value_Not_Echoed"
        monkeypatch.setenv(
            "FL_HA_SCHOOLS",
            json.dumps([MIDDLE | {"bell_calendars": [secret_looking]}]),
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
