"""Home Assistant data exchange (HA-initiated only).

The app never calls Home Assistant and holds no HA credential. HA pushes school
schedule, district calendar and zone snapshots, polls status, triggers
assessments and relays Waze route estimates.

TODO(M2/M3): request/response schemas for the HA-only routes in ``api/ha.py``.
"""
