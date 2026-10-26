---
title: "Architecture"
schema_type: common
status: published
owner: core-maintainer
purpose: "Module layout and design principles of Family Logistics."
tags:
  - development
  - architecture
---

## Module layout

```text
src/family_logistics/
├── adapters/
│   ├── caldav/        # read-only HTTP transport (rejects write methods)
│   ├── routing/       # RouteProvider protocol and the static provider
│   ├── apprise/       # notification client (dedicated fl-* tags only)
│   ├── home_assistant/
│   └── llama_cpp/     # narrative LLM client (M5)
├── api/               # FastAPI app, /healthz, Home Assistant routes (snapshot ingest, route relay; M4 adds the rest)
├── core/              # settings, address encryption, SQLite, exceptions
├── db/                # table definitions, Alembic migrations, audit log
├── domain/            # models, notes parser, confirmation codes, change
│                      # impact, capacity, stop ordering, rules, run status
├── middleware/        # correlation IDs, security headers
├── services/          # collector, scheduler, assessments, notifications
├── utils/             # structured logging
└── cli.py             # serve, migrate, verify-db, healthcheck, config
```

## Design principles

1. **The deterministic engine is the only authority.** Rules in `domain/`
   decide every finding. An LLM may explain results but cannot change them
   ([ADR 0001](../ADRs/0001-deterministic-authority-and-llm-boundary.md)).
2. **Read-only calendar access.** The CalDAV transport refuses any method
   other than read methods before network I/O
   ([ADR 0003](../ADRs/0003-read-only-caldav-ingestion.md)).
3. **Passenger-level commitments.** Each passenger leg is its own obligation
   keyed by calendar, UID, recurrence ID and passenger
   ([ADR 0004](../ADRs/0004-passenger-level-commitments.md)).
4. **Single writer.** SQLite in WAL mode with one process and one writer
   ([ADR 0005](../ADRs/0005-sqlite-wal-single-writer.md)).
5. **Fail visibly.** Missing or stale inputs produce `incomplete`, never
   `clear`; runtime errors produce `failed`.
6. **Typed and validated.** BasedPyright strict mode; Pydantic models and
   settings validate input at the boundary.

See the [architecture decisions](../ADRs/README.md) for the full records.
