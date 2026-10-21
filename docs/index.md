---
title: "Family Logistics"
schema_type: common
status: published
owner: core-maintainer
purpose: "Documentation home page for Family Logistics."
tags:
  - documentation
  - home
---

Family Logistics checks a family's ride commitments before anyone is stranded.
It reads iCloud calendars read-only, builds a location timeline for each child,
and uses deterministic rules to find missing or conflicting rides, including
rides promised to children from other families. Results feed a Home Assistant
status card and Apprise notifications.

**Status: M0 (discovery).** The domain core is implemented and tested;
collection, scheduling, notifications and the Home Assistant API arrive in
milestones M1 to M5. Notifications default to shadow mode.

## Quick start

```bash
git clone https://github.com/ByronWilliamsCPA/family-logistics.git
cd family-logistics
uv sync --all-extras
uv run family-logistics --help
uv run family-logistics config
```

The application ships as a container image, not a Python package:

```bash
docker pull ghcr.io/byronwilliamscpa/family-logistics:<tag>
```

## Where to go next

- [Overview](guides/overview.md): what the engine checks and why
- [Configuration](guides/configuration.md): every `FL_` setting
- [Usage](guides/usage.md): running the CLI and API locally
- [Runbook](runbook.md): operating, rotating keys and restoring
- [Architecture decisions](ADRs/README.md): ADRs 0001 to 0007
- [Privacy and retention](privacy/privacy-and-retention-spec.md): handling of
  children's and other families' data

## License

MIT; see the [license page](project/license.md).
