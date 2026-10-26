---
schema_type: planning
title: "ADR-005: SQLite WAL, Single Writer, Repository Interfaces"
description: "Persist registries, events, findings and audit in one SQLite database in WAL mode on the Docker host local disk, behind repository interfaces that allow a later move to PostgreSQL."
tags:
  - architecture
  - decisions
  - adr
  - storage
status: published
owner: core-maintainer
component: Strategy
source: "Family Logistics development handoff v1.0, section 5"
purpose: "Choose the storage engine and its operating constraints."
---

> **Status**: `published` (accepted 2026-10-25)

**Decision Date**: 2026-10-21
**Deciders**: Byron Williams
**Affected Components**: storage, migrations, backup sidecar
**Implementation Target**: M1

## Context

Data volume is small (tens of events per week, a few hundred registry rows).
Small single-host apps commonly use SQLite in WAL mode on local disk under
`/data/<app>`, never on network storage; larger apps get a dedicated PostgreSQL.
There is no shared PostgreSQL.

## Decision

- SQLite in WAL mode at `/data/family_logistics.db` (host
  `/data/family-logistics`), `foreign_keys=ON`, `busy_timeout` set.
- **Single writer**: only the `family-logistics` container opens the database
  for writing; the scheduler runs in-process. No other container mounts it.
- SQLAlchemy 2.x models with Alembic migrations; every migration has a tested
  downgrade.
- Domain code depends on repository protocols, not on SQLAlchemy sessions, so a
  PostgreSQL implementation can replace SQLite without domain changes.
- Backups: a sidecar with `network_mode: none` runs `sqlite3 .backup` (online,
  consistent), runs `PRAGMA integrity_check` on the copy, and writes it to
  a backup location configured by the deployment. A restore test runs at least
  monthly.
- Sensitive columns (external household addresses) follow the privacy spec.

## Alternatives Considered

| Option | Assessment |
|--------|------------|
| Dedicated PostgreSQL | More moving parts for a tiny dataset. Revisit if multiple writers appear. |
| SQLite on network storage (NFS) | Locking on NFS is unsafe for SQLite. Rejected. |

## Consequences

- Simple operations and backups; no network database.
- Horizontal scaling is impossible by design; not needed.

## References

- [privacy-and-retention-spec.md](../privacy/privacy-and-retention-spec.md)
- the private deployment repository
