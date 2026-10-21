---
title: "Usage"
schema_type: common
status: published
owner: core-maintainer
purpose: "Running the Family Logistics CLI and API locally."
tags:
  - guide
  - usage
---

## Install from source

```bash
git clone https://github.com/ByronWilliamsCPA/family-logistics.git
cd family-logistics
uv sync --all-extras
```

The application is distributed as the container image
`ghcr.io/byronwilliamscpa/family-logistics`; it is not published to PyPI.

## Command line

```bash
uv run family-logistics --help
uv run family-logistics --version
uv run family-logistics config        # non-secret settings only
```

| Command | Purpose |
|---------|---------|
| `serve [--upgrade-db]` | Run the API (single process, single SQLite writer); refuses an out-of-date schema unless `--upgrade-db` is given |
| `migrate [--revision R] [--downgrade [--yes]]` | Upgrade the schema to head (or to `R`) in one transaction; a target below the current revision is refused unless `--downgrade` is given, and a downgrade needs an explicit `--revision R` and a confirmation prompt, which `--yes` skips |
| `verify-db PATH` | Read-only integrity and schema-revision check of a database file |
| `healthcheck` | Probe the local `/healthz`; exit 0 when healthy |
| `config` | Print non-secret configuration values |
| `registry load [PATH] [--dry-run]` | Validate the private registry seed and apply it in one audited transaction |
| `registry show` | List people, passengers, places, vehicles and calendars (never addresses) |

## Run the API locally

`serve` refuses to start without a Home Assistant token of at least 32
characters, when the database schema is behind head (unless `--upgrade-db` is
given; that upgrade is atomic and serialized by a lock), or when the schema is
newer than this release (a clean message and a non-zero exit, never a
downgrade). Use a throwaway token and database for development:

```bash
export FL_DATABASE_PATH=./data/dev.db
export FL_HA_TOKEN="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
uv run family-logistics migrate
uv run family-logistics serve &
sleep 3
uv run family-logistics healthcheck
curl -s http://127.0.0.1:8080/healthz
kill %1
```

`/healthz` returns `{"status": "ok", "database": true, "version": "..."}`, or
HTTP 503 when the database is unreachable.

## Load the household registry

People, external passengers, places, vehicles, calendars and school
transport defaults come from one private YAML seed kept **outside** this
repository. Start from the fictional example and keep the real copy
somewhere private:

```bash
mkdir -p ~/.config/family-logistics
cp config/registry.example.yaml ~/.config/family-logistics/registry.yaml
# edit it, then:
export FL_REGISTRY_SEED=~/.config/family-logistics/registry.yaml
export FL_DATA_KEY="$(uv run python -c 'from family_logistics.core.crypto import generate_key; print(generate_key())')"
uv run family-logistics registry load --dry-run
uv run family-logistics registry load
uv run family-logistics registry show
```

Rules the loader enforces:

- Loading the same seed twice changes nothing; each change writes one audit
  row holding hashes only.
- People, passengers and calendars removed from the seed are deactivated,
  never deleted. Places, vehicles and households are kept.
- Addresses and coordinates are stored encrypted with `FL_DATA_KEY`; a seed
  or registry holding them cannot be loaded without the key.
- A seed inside a git working tree is refused (`--allow-repo-path` is for the
  fictional example only).

## Library use

The domain modules are importable for tests and tooling:

```python
from family_logistics import __version__
from family_logistics.utils.logging import get_logger, setup_logging

setup_logging(level="DEBUG", json_logs=False)
logger = get_logger(__name__)
logger.info("family-logistics", version=__version__)
```
