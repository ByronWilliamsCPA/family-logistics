# Claude Code Project Guidelines

> This file holds **project-specific** guidance. The universal development
> standards (security-first rules, code quality, FIPS, OpenSSF, pre-commit
> checklist) live in [.standards/CLAUDE.baseline.md](.standards/CLAUDE.baseline.md),
> imported below, and in the global `~/.claude/CLAUDE.md`. Do not copy them
> here; merge baseline updates with `/merge-standards` after `cruft update`.

@.standards/CLAUDE.baseline.md

## Public Repository: No Household Data (CRITICAL)

This repository is **public**. It holds only the generic engine.

- Use only the fictional household in code, tests, fixtures, docs, commit
  messages and PR text: parents **Morgan** (operator) and **Riley**; children
  **Alex**, **Sam**, **Jordan** and **Casey** (away at university, monitoring
  off); **Passenger E** and **Passenger R** from other families;
  **Driver S**, an adult driver from another family.
- Never commit real names, addresses, coordinates, calendar identifiers,
  credentials, phone numbers, personal e-mail addresses, real transportation
  histories, or site network details (hosts, IPs, hostnames, monitoring or
  notification-service weaknesses).
- Site-specific requirements, the defaults register and deployment
  configuration live in the **private deployment repository**. Refer to it
  generically.
- gitleaks (`.gitleaks.toml`) and the private-terms check
  (`scripts/check_private_terms.py`, deny-list outside the repo) guard every
  commit. Never bypass them.

## Template Feedback Requirement (CRITICAL)

This project was generated from
[cookiecutter-python-template](https://github.com/ByronWilliamsCPA/cookiecutter-python-template)
with cruft. When you find an issue the template should have handled, add it to
`docs/template_feedback.md` with **Issue**, **Context**, **Suggested Fix** and
**Priority** (Critical / High / Medium / Low). The file is gitignored: it is a
local working buffer, passed to the template team out-of-band.

## Project Overview

**Name**: Family Logistics
**Description**: Deterministic family transportation assurance: validates
iCloud ride commitments before a child is stranded.
**Author**: Byron Williams
**Repository**: <https://github.com/ByronWilliamsCPA/family-logistics>
**Status**: M0 (discovery). Domain core implemented; collection, scheduling,
notifications and the Home Assistant API arrive in M1 to M5.
**Distribution**: container image `ghcr.io/byronwilliamscpa/family-logistics`
only. Not published to PyPI.

### Non-negotiable design rules

1. iCloud is the authoritative record; the app never writes to it. The CalDAV
   transport allows only read methods ([ADR 0003](docs/ADRs/0003-read-only-caldav-ingestion.md)).
2. The deterministic engine decides every finding; the LLM may only explain
   ([ADR 0001](docs/ADRs/0001-deterministic-authority-and-llm-boundary.md)).
3. Each passenger leg is its own obligation; changing one passenger never
   removes another's ride ([ADR 0004](docs/ADRs/0004-passenger-level-commitments.md)).
   `tests/scenarios/test_friday_external_passenger.py` is a permanent
   regression test; never weaken it.
4. A failed or incomplete analysis is never reported as clear.
5. Notifications default to shadow mode; only dedicated `fl-*` Apprise tags
   are used.

### Technology Stack

- **Python** 3.12, **uv**, **Ruff**, **BasedPyright** (strict)
- **FastAPI** + **uvicorn**, **Click** CLI, **Pydantic Settings** (`FL_` prefix)
- **SQLite** (WAL, single writer), **SQLAlchemy**/**Alembic**
- **httpx**, **icalendar**/**recurring-ical-events**, **APScheduler** 3.x
- **cryptography** (Fernet) for protected addresses at rest
- **pytest** (80% coverage gate), **Bandit**, **pip-audit**, **OSV-Scanner**, **gitleaks**
- **MkDocs Material** documentation site

## Model Selection

Follow the global model selection table in `~/.claude/CLAUDE.md`. For this
project: Haiku for read-only exploration, Sonnet for standard development and
test work, and Opus for architecture decisions, ADRs, and any change to the
parser, confirmation, change-impact or run-status rules.

## Response-Aware Development (RAD)

Tag assumptions that could cause production failures with `#CRITICAL`,
`#ASSUME` or `#EDGE`, each followed by a `#VERIFY` line. Syntax and categories:
[docs/development/response-aware-development.md](docs/development/response-aware-development.md).
Mandatory categories here: **security** (credentials, tokens, identity headers,
children's data), **external resources** (iCloud, Home Assistant, Apprise,
routing providers, the inference server) and **timing** (scheduler runs,
recurrence expansion, quiet hours, imminent-ride windows).

## Branch Workflow

Never commit directly to `main`. Branch first: `feat/`, `fix/`, `docs/`,
`refactor/`, `test/`, `chore/`, `ci/`, followed by a descriptive slug. Commits
use Conventional Commits; python-semantic-release derives versions and the
changelog from them.

## Quick Start Commands

```bash
uv sync --all-extras
uv run pre-commit install

uv run pytest                              # tests + coverage gate
uv run ruff format . && uv run ruff check . --fix
uv run basedpyright src/
uv run bandit -r src
uv run pip-audit
uv run pre-commit run --all-files
uv run mkdocs build --strict               # docs

uv run family-logistics --help             # serve | migrate | verify-db | healthcheck | config
uv run family-logistics migrate            # schema to head (serve refuses an old schema)
```

## Project Structure

```text
src/family_logistics/
├── adapters/      # caldav (read-only transport), routing, apprise, home_assistant, llama_cpp
├── api/           # FastAPI app, /healthz, Home Assistant routes (M3)
├── core/          # config, crypto, database, exceptions
├── domain/        # models, parser, confirmation, change_impact, capacity,
│                  # stop_order, registry, rules, run_status
├── middleware/    # correlation IDs, security headers
├── services/      # collector, scheduler, assessments, notifications (M1-M3)
├── utils/         # structured logging
└── cli.py
tests/
├── unit/          # one module per domain area
├── scenarios/     # acceptance matrix and the Friday regression
├── fixtures/synthetic/   # the only tracked .ics fixtures
└── fl_factories.py       # shared builders for the fictional household
tools/m0/          # M0 discovery checks
docs/              # MkDocs site (ADRs, privacy spec, runbook, guides)
```

## Code Conventions

- Configuration: `family_logistics.core.config.get_settings()`; every setting
  uses the `FL_` prefix and is documented in `docs/guides/configuration.md`
  and `.env.example`. Secrets are `SecretStr` and never logged.
- Logging: `family_logistics.utils.logging` (structlog, correlation IDs).
- Errors: the hierarchy in `family_logistics.core.exceptions`.
- Docstrings: Google style; types live in signatures, not docstrings.
- Finding rule IDs come from `family_logistics.domain.rules`; never invent
  ad hoc IDs.

## Git Worktrees

Create worktrees inside the project at `.worktrees/<branch-slug>` (gitignored)
and run `uv sync --all-extras` in each.

```bash
git worktree add .worktrees/feat-my-feature -b feat/my-feature
git worktree remove .worktrees/feat-my-feature
```

## CI/CD

Workflows in `.github/workflows/` delegate to the organization's reusable
workflows pinned by commit SHA. Key gates: CI (tests, lint, types), security
analysis, Private Data Guard (gitleaks over full history plus the private-terms
check), REUSE, documentation build, and the release workflow that builds and
attests the GHCR image.

## Cruft Template Updates

```bash
cruft check
cruft update --skip CLAUDE.md --skip REUSE.toml --skip docs/template_feedback.md
git diff .standards/        # then /merge-standards if baselines changed
```

Always skip `CLAUDE.md`, `REUSE.toml`, `docs/template_feedback.md`,
`docs/planning/*` and `.env`.

## Additional Resources

- [README.md](README.md), [CONTRIBUTING.md](CONTRIBUTING.md), [SECURITY.md](SECURITY.md)
- [Runbook](docs/runbook.md) and [Privacy spec](docs/privacy/privacy-and-retention-spec.md)
- [Baseline standards](.standards/CLAUDE.baseline.md)

---

**Last Updated**: 2026-10-21
