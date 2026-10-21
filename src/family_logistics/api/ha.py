"""HA-only routes (bearer token; network restriction is a deployment control, ADR-0007).

TODO(M2/M4): GET /api/v1/status, POST /api/v1/assessments,
GET /api/v1/assessments/{run_id}, POST /api/v1/ingest/ha-snapshot,
GET /api/v1/route-requests and POST /api/v1/route-results. Compare the token
with ``hmac.compare_digest``.
"""
