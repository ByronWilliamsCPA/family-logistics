"""Opaque calendar identifiers."""

from __future__ import annotations

import pytest
from caldav_fakes import PARTITION, SAM_PATH

from family_logistics.adapters.caldav.ids import (
    calendar_id_for_href,
    normalize_calendar_href,
)


@pytest.mark.unit
class TestCalendarIds:
    def test_normalization(self):
        assert (
            normalize_calendar_href(
                "HTTPS://P01-CalDAV.Example.Test:443/1000/cal?x=1#f"
            )
            == "https://p01-caldav.example.test/1000/cal/"
        )
        assert (
            normalize_calendar_href("https://caldav.example.test:8443/a//")
            == "https://caldav.example.test:8443/a/"
        )

    def test_equivalent_urls_share_one_id(self):
        href = f"{PARTITION}{SAM_PATH}"
        variants = [
            href,
            href.rstrip("/"),
            href.upper().replace("/1000/CALENDARS/SAM/", "/1000/calendars/sam/"),
        ]
        assert len({calendar_id_for_href(v) for v in variants}) == 1

    def test_id_is_short_and_opaque(self):
        calendar_id = calendar_id_for_href(f"{PARTITION}{SAM_PATH}")
        assert calendar_id.startswith("cal-")
        assert len(calendar_id) == 20
        assert "1000" not in calendar_id
        assert calendar_id != calendar_id_for_href(
            f"{PARTITION}/1000/calendars/jordan/"
        )

    def test_path_case_is_significant(self):
        assert calendar_id_for_href(f"{PARTITION}/A/") != calendar_id_for_href(
            f"{PARTITION}/a/"
        )

    def test_relative_href_is_refused(self):
        with pytest.raises(ValueError, match="absolute"):
            calendar_id_for_href(SAM_PATH)
