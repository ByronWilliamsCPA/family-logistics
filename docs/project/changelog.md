---
title: "Changelog"
schema_type: common
status: published
owner: core-maintainer
purpose: "Changelog for Family Logistics."
tags:
  - project
  - changelog
---

All notable changes to Family Logistics will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

No release has been cut: the version is 0.1.0 and the repository has no git tags. Every entry below is
unreleased, and the scheduler and on-demand assessments ship disabled.

### Added

- Domain core: models, notes parser, confirmation codes, change impact and run status (initial public release),
  with the event contracts and core schema migration (#4)
- Rule engine and location timeline (#6)
- Registry repositories, the private seed loader and the `registry load` and `registry show` commands (#5)
- Read-only CalDAV collector, recurrence expansion and the `collect` command (#7)
- Assessment orchestrator with run and finding persistence (#17); a missing school snapshot makes a run
  incomplete (#26)
- Home Assistant school snapshot ingest: storage, event mapping and `POST /api/v1/ingest/ha-snapshot`
  (#27, #28, #29)
- Route providers: the null provider, the provider factory (#30) and the `ha_relay` provider with the route
  cache and the Home Assistant relay routes (#32)
- Notification service: ADR 0008 (#34), policy and subjects (#35), text renderer with name masking (#36),
  state storage and retention purge (#37) and dispatch (#38)
- Scheduler running collect, assess and notify in shadow mode (#40)
- Shared pipeline lock and on-demand run service (#41), status card and the `GET /api/v1/status`,
  `POST /api/v1/assessments` and `GET /api/v1/assessments/{run_id}` routes (#42)

### Changed

- None

### Deprecated

- None

### Removed

- None

### Fixed

- None

### Security

- None

## [0.1.0] - not yet released

### Added

- Initial release of Family Logistics (planned; see Unreleased above for what it will contain)

---

[Unreleased]: https://github.com/ByronWilliamsCPA/family-logistics/commits/main
[0.1.0]: https://github.com/ByronWilliamsCPA/family-logistics/releases
