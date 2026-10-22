# Family Logistics

[![OpenSSF Scorecard](https://api.securityscorecards.dev/projects/github.com/ByronWilliamsCPA/family-logistics/badge)](https://securityscorecards.dev/viewer/?uri=github.com/ByronWilliamsCPA/family-logistics)
[![codecov](https://codecov.io/gh/ByronWilliamsCPA/family-logistics/graph/badge.svg)](https://codecov.io/gh/ByronWilliamsCPA/family-logistics)
[![Quality Gate Status](https://sonarcloud.io/api/project_badges/measure?project=ByronWilliamsCPA_family-logistics&metric=alert_status)](https://sonarcloud.io/summary/new_code?id=ByronWilliamsCPA_family-logistics)
[![REUSE Compliance](https://github.com/ByronWilliamsCPA/family-logistics/actions/workflows/reuse.yml/badge.svg)](https://github.com/ByronWilliamsCPA/family-logistics/actions/workflows/reuse.yml)
[![CI Pipeline](https://github.com/ByronWilliamsCPA/family-logistics/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/ByronWilliamsCPA/family-logistics/actions/workflows/ci.yml?query=branch%3Amain)
[![Security Analysis](https://github.com/ByronWilliamsCPA/family-logistics/actions/workflows/security-analysis.yml/badge.svg?branch=main)](https://github.com/ByronWilliamsCPA/family-logistics/actions/workflows/security-analysis.yml?query=branch%3Amain)
[![Python 3.12](https://img.shields.io/badge/python-3.12-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

Family Logistics is a self-hosted transportation assurance service for a
family. It reads ride commitments from the family's iCloud calendars
(read-only), builds a location timeline for each child, and uses
deterministic rules to find missing or conflicting rides **before** a child is
stranded, including children from other families who share a carpool. Results
feed a Home Assistant status card and Apprise notifications. A local LLM may
later explain findings, but it never decides them.

**Status: M0 (discovery).** The domain core (notes parser, confirmation codes,
passenger-level change impact, run status) is implemented and tested, as is the
platform around it: Alembic schema migrations, a hash-only audit log, a minimal
Apprise API client, and `serve` startup checks that refuse to start on missing
secrets or an out-of-date schema. Collection, scheduling, notifications and the
Home Assistant API arrive in milestones M1 to M5. Production notifications stay off
(`FL_NOTIFICATION_MODE=shadow`) until the household approves the defaults and a
two-week shadow period completes.

Key rules:

- iCloud calendars are the authoritative record; the app never writes to them.
- Each passenger's ride leg is its own obligation; changing one child's plan
  never silently removes another child's ride.
- A failed or incomplete analysis is never shown as clear.

## This repository is public

It holds only the generic engine. Tests, fixtures and examples use a fictional
household (parents Morgan and Riley; children Alex, Sam, Jordan and Casey;
"Passenger E" and "Driver S" from other families). Real names, addresses,
calendar identifiers, the defaults register and deployment configuration live
in a private deployment repository. gitleaks and a private-terms check guard
every commit; see [SECURITY.md](SECURITY.md).

## Quick start (development)

Prerequisites: Python 3.12 and [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/ByronWilliamsCPA/family-logistics.git
cd family-logistics
uv sync --all-extras
uv run pre-commit install

uv run family-logistics --help
uv run family-logistics config     # non-secret settings only
uv run pytest
```

Run the API locally with a throwaway database and token:

```bash
export FL_DATABASE_PATH=./data/dev.db
export FL_HA_TOKEN="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
uv run family-logistics migrate       # create or upgrade the schema
uv run family-logistics serve &
sleep 3
uv run family-logistics healthcheck   # exit 0 when /healthz reports ok
kill %1
```

`serve` refuses to start without `FL_HA_TOKEN` (32 characters or more) or when
the database schema is behind; run `migrate` first or start with
`serve --upgrade-db` (the container image does the latter). All
settings use the `FL_` prefix; see
[docs/guides/configuration.md](docs/guides/configuration.md) and
[.env.example](.env.example).

## Container image

The application ships only as a container image built from the
[Dockerfile](Dockerfile) on digest-pinned hardened Python base images, running
as a non-root user:

```bash
docker pull ghcr.io/byronwilliamscpa/family-logistics:<tag>
```

Tags are `sha-<7>` and the release version. It is not published to PyPI.
Deployment (Compose, secrets, reverse proxy, backups) is defined in the
private deployment repository.

## CLI

| Command | Purpose |
|---------|---------|
| `family-logistics serve [--upgrade-db]` | Run the API (single process, single SQLite writer); `--upgrade-db` migrates to head first |
| `family-logistics migrate [--revision R] [--downgrade]` | Upgrade the schema to head, or explicitly downgrade to revision `R` |
| `family-logistics verify-db PATH` | Read-only integrity and schema-revision check of a database file (for restored backups) |
| `family-logistics healthcheck` | Probe the local `/healthz`; used by the container healthcheck |
| `family-logistics config` | Print non-secret configuration |

## Project layout

```text
src/family_logistics/
├── adapters/      # caldav (read-only transport), routing, apprise, home_assistant, llama_cpp
├── api/           # FastAPI app, /healthz, Home Assistant routes (M3)
├── core/          # settings, crypto (address encryption), database, exceptions
├── db/            # table definitions, Alembic migrations, audit log repository
├── domain/        # models, notes parser, confirmation, change impact, rules, run status
├── middleware/    # correlation IDs, security headers
├── services/      # collector, scheduler, assessments, notifications (M1 to M3)
└── utils/         # structured logging
tests/             # unit/ and scenarios/ (acceptance matrix, Friday regression)
tools/m0/          # M0 discovery checks
docs/              # MkDocs site: guides, ADRs, privacy spec, runbook
```

## Documentation

| Document | Purpose |
|----------|---------|
| [Documentation site](https://byronwilliamscpa.github.io/family-logistics/) | Guides, ADRs and runbook |
| [Architecture decisions](docs/ADRs/README.md) | ADRs 0001 to 0007 |
| [Privacy and retention spec](docs/privacy/privacy-and-retention-spec.md) | Handling of minors' and other families' data |
| [Runbook](docs/runbook.md) | Operating, rotating keys and restoring |
| [M0 validation kit](tools/m0/README.md) | Discovery checks with scripts and protocols |
| [CONTRIBUTING.md](CONTRIBUTING.md) | Development workflow and quality gates |

## Quality gates

```bash
uv run ruff format --check .
uv run ruff check .
uv run basedpyright src/
uv run pytest --cov=src --cov-fail-under=80
uv run bandit -r src
uv run pre-commit run --all-files
```

Commits follow [Conventional Commits](https://www.conventionalcommits.org/);
python-semantic-release derives versions and the changelog from them.

## Security

Report vulnerabilities privately as described in [SECURITY.md](SECURITY.md).
Never include real household data in an issue or pull request.

## License

MIT; see [LICENSE](LICENSE). Generated from
[cookiecutter-python-template](https://github.com/ByronWilliamsCPA/cookiecutter-python-template).
