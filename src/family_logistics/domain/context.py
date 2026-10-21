"""Engine settings and the per-run context shared by the rule modules.

``EngineSettings`` holds every threshold the deterministic rules use. Values
come from ``Settings`` (``EngineSettings.from_config``) so the engine itself
reads no environment and performs no I/O.

#ASSUME: Timing: every threshold here is a proposed default pending owner
approval (defaults register). #VERIFY during the shadow period that the merge
gap, pickup tolerance and buffers neither hide real gaps nor flood reports.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from family_logistics.domain.locations import LocationResolver

if TYPE_CHECKING:
    from datetime import date, datetime

    from family_logistics.adapters.routing.base import RouteProvider
    from family_logistics.core.config import Settings
    from family_logistics.domain.events import (
        AssessmentInput,
        SchoolDay,
        TransportDefault,
    )
    from family_logistics.domain.models import Person, Trip, TripPassengerLeg, Vehicle
    from family_logistics.domain.timeline import Stay

ICLOUD_SOURCE = "icloud"
HA_SNAPSHOT_SOURCE = "ha_snapshot"

DEFAULT_SOURCE_MAX_AGES: tuple[tuple[str, timedelta], ...] = (
    (ICLOUD_SOURCE, timedelta(hours=2)),
    (HA_SNAPSHOT_SOURCE, timedelta(hours=26)),
)
"""Proposed staleness limits per source (defaults register, pending approval)."""


@dataclass(frozen=True, slots=True)
class EngineSettings:
    """Thresholds the deterministic rules use.

    Attributes:
        same_location_gap: Largest idle gap that still merges two activities at
            one place into a single stay.
        travel_buffer: Arrive this long before a deadline.
        stop_buffer: Loading or unloading time per stop.
        pickup_tolerance: A ride covering a pickup must start no later than
            this after the activity ends.
        early_arrival_window: A morning ride covers an activity when it ends
            no earlier than this before the activity starts.
        reachability_window: Driver reachability between back-to-back
            commitments is checked only when the gap is at most this long.
        imminent_window: Rides starting within this window are imminent; an
            unverified destination is then critical.
        unverified_capacity_seats: Report an unknown vehicle capacity only
            when a ride needs at least this many seats (driver included).
        source_max_ages: Staleness limit per source name.
        default_source_max_age: Limit for a source not listed above.
    """

    same_location_gap: timedelta = timedelta(minutes=60)
    travel_buffer: timedelta = timedelta(minutes=10)
    stop_buffer: timedelta = timedelta(minutes=5)
    pickup_tolerance: timedelta = timedelta(minutes=30)
    early_arrival_window: timedelta = timedelta(minutes=90)
    reachability_window: timedelta = timedelta(minutes=90)
    imminent_window: timedelta = timedelta(minutes=180)
    unverified_capacity_seats: int = 5
    source_max_ages: tuple[tuple[str, timedelta], ...] = DEFAULT_SOURCE_MAX_AGES
    default_source_max_age: timedelta = timedelta(hours=2)

    @classmethod
    def from_config(cls, settings: Settings) -> EngineSettings:
        """Build engine thresholds from application settings.

        Args:
            settings: Loaded application settings.

        Returns:
            Engine settings using the configured values.
        """
        return cls(
            same_location_gap=timedelta(minutes=settings.same_location_gap_minutes),
            travel_buffer=timedelta(minutes=settings.travel_buffer_minutes),
            stop_buffer=timedelta(minutes=settings.stop_buffer_minutes),
            imminent_window=timedelta(minutes=settings.imminent_window_minutes),
        )

    def max_age_for(self, source: str) -> timedelta:
        """Return the staleness limit of a source.

        Args:
            source: Source name.

        Returns:
            Its configured limit, or the default limit.
        """
        for name, limit in self.source_max_ages:
            if name == source:
                return limit
        return self.default_source_max_age


@dataclass(frozen=True)
class AssessmentContext:
    """Read-only view of one run shared by the rule modules.

    Attributes:
        inp: The assessment input.
        settings: Engine thresholds.
        resolver: Location resolver for the run.
        route_provider: Travel-time source; may return None for any leg.
        now: Evaluation time (UTC).
        zone: Local timezone.
        stays: Each monitored child's stays across the window.
    """

    inp: AssessmentInput
    settings: EngineSettings
    resolver: LocationResolver
    route_provider: RouteProvider
    now: datetime
    zone: ZoneInfo
    stays: dict[str, tuple[Stay, ...]] = field(default_factory=dict)

    @classmethod
    def create(
        cls,
        inp: AssessmentInput,
        settings: EngineSettings,
        route_provider: RouteProvider,
        now: datetime,
    ) -> AssessmentContext:
        """Build the context for one run.

        Args:
            inp: Assessment input.
            settings: Engine thresholds.
            route_provider: Travel-time source.
            now: Evaluation time (UTC).

        Returns:
            The context.
        """
        resolver = LocationResolver(inp.locations, inp.people, inp.external_passengers)
        return cls(inp, settings, resolver, route_provider, now, ZoneInfo(inp.timezone))

    def person(self, person_id: str) -> Person | None:
        """Return a registered person.

        Args:
            person_id: Person identifier.

        Returns:
            The person, or None when not registered.
        """
        return next((p for p in self.inp.people if p.person_id == person_id), None)

    def display_name(self, person_id: str) -> str:
        """Return a person's or passenger's display name.

        Args:
            person_id: Person or passenger identifier.

        Returns:
            The registered display name, or the identifier.
        """
        person = self.person(person_id)
        if person is not None:
            return person.display_name
        external = next(
            (e for e in self.inp.external_passengers if e.passenger_id == person_id),
            None,
        )
        return external.display_first_name if external else person_id

    def vehicle(self, vehicle_id: str | None) -> Vehicle | None:
        """Return a registered vehicle.

        Args:
            vehicle_id: Vehicle identifier, or None.

        Returns:
            The vehicle, or None.
        """
        return next((v for v in self.inp.vehicles if v.vehicle_id == vehicle_id), None)

    def upcoming_trips(self) -> tuple[Trip, ...]:
        """Return trips that have not ended yet.

        Returns:
            Trips whose window ends at or after ``now``.
        """
        return tuple(t for t in self.inp.trips if t.end >= self.now)

    def legs_for(self, passenger_id: str) -> tuple[TripPassengerLeg, ...]:
        """Return every current leg carrying a passenger.

        Args:
            passenger_id: Passenger identifier.

        Returns:
            Legs from all trips in the input.
        """
        return tuple(
            leg
            for trip in self.inp.trips
            for leg in trip.legs()
            if leg.passenger_id == passenger_id
        )

    def school_day(self, person_id: str, day: date) -> SchoolDay | None:
        """Return a person's school schedule for a local day.

        Args:
            person_id: Student.
            day: Local calendar day.

        Returns:
            The school day, or None when no schedule was supplied.
        """
        return next(
            (
                sd
                for sd in self.inp.school_days
                if sd.person_id == person_id and sd.date == day
            ),
            None,
        )

    def defaults_for(self, person_id: str, day: date) -> tuple[TransportDefault, ...]:
        """Return the transport defaults that apply to a person on a day.

        Args:
            person_id: Traveller.
            day: Local calendar day.

        Returns:
            Applicable defaults.
        """
        return tuple(
            d
            for d in self.inp.transport_defaults
            if d.person_id == person_id and d.applies_on(day)
        )

    def is_imminent(self, start: datetime) -> bool:
        """Report whether a ride starts within the imminent window.

        Args:
            start: Ride start (UTC).

        Returns:
            True when the ride starts between now and the window's end.
        """
        return start - self.now <= self.settings.imminent_window

    def when(self, value: datetime) -> str:
        """Format a UTC time as a short local label for messages.

        Args:
            value: Time (UTC).

        Returns:
            For example ``Fri 17:30``.
        """
        return value.astimezone(self.zone).strftime("%a %H:%M")

    def local_day(self, value: datetime) -> date:
        """Return the local calendar day of a UTC time.

        Args:
            value: Time (UTC).

        Returns:
            The local day.
        """
        return value.astimezone(self.zone).date()
