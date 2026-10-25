"""Home Assistant data exchange (HA-initiated only).

The app never calls Home Assistant and holds no HA credential. HA pushes school
schedule, district calendar and zone snapshots, polls status, triggers
assessments and relays Waze route estimates.

``schemas`` holds the school snapshot payload models (ADR 0007) and
``school_days`` the deterministic mapping from calendar events to school days.

TODO(M3): schemas for the remaining HA-only routes in ``api/ha.py``.
"""
