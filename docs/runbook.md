---
title: "Runbook"
schema_type: common
status: published
owner: core-maintainer
purpose: "Generic operating procedures for Family Logistics: start, stop, health, modes, key rotation, restore and escalation."
tags:
  - operations
  - runbook
---

This runbook is generic. Hosts, paths, recipients, monitors and backup
locations for a real deployment live in the private deployment repository;
never add them here.

## Start and stop

The container runs one process with one SQLite writer.

- **Start:** start the deployment's container. The image runs
  `family-logistics serve --upgrade-db`, which migrates the schema to head
  before serving. Outside a container, run `family-logistics migrate` and then
  `family-logistics serve`. The process refuses to start when `FL_HA_TOKEN` is
  missing or shorter than 32 characters, when `FL_DATA_KEY` is missing in
  production mode, when the schema is behind head (without `--upgrade-db`), or
  when a setting fails validation; read the startup error and fix it.
- **Stop:** stop the container. Wait for it to exit before touching the
  database file, so the WAL is checkpointed.
- Never run two instances against the same database file.

## Health

```bash
family-logistics healthcheck      # exit 0 when healthy
curl -s http://127.0.0.1:8080/healthz
```

| Response | Meaning | Action |
|----------|---------|--------|
| `200 {"status": "ok", "database": true}` | Healthy | None |
| `503 {"database": false}` | Database unreachable | Check the data volume is mounted and writable, free disk space, then restart |
| No response | Process down | Check container logs, then start |

A healthy process does not mean the last assessment succeeded. The
family-wide status card reports `incomplete` or `failed` runs separately, and
either state is never shown as clear.

## Notification modes

| `FL_NOTIFICATION_MODE` | Behaviour |
|------------------------|-----------|
| `shadow` (default) | Real collection and validation; all output goes to the operator only (`fl-shadow` tag); nothing reaches the other parent |
| `production` | Reports, status and findings go to both parents through the dedicated `fl-*` tags |

Switching to `production` requires the joint approval of both parents after a
shadow period, recorded in the private deployment repository. To switch back,
set `shadow` and restart; this is always safe.

## Rotating secrets

### `FL_HA_TOKEN`

1. Generate a new token: `python3 -c 'import secrets; print(secrets.token_urlsafe(32))'`.
2. Set it in the deployment's secret store and in Home Assistant's secrets.
3. Restart the container, then confirm Home Assistant's status sensor updates.

Rotate after any suspected exposure and at least once a year.

### `FL_DATA_KEY`

`FL_DATA_KEY` encrypts protected addresses at rest. A wrong or missing key
fails closed: protected addresses cannot be read and nothing is stored in
plain text.

1. Generate a key: `python3 -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'`.
2. Stop the container and take a backup (see below).
3. Re-encrypt stored addresses with the old and new keys (a re-key command is
   planned with the registry in M1; until then no addresses are stored).
4. Set the new key and start the container.

Keep the previous key until a restore test with the new key succeeds.

### CalDAV app-specific password

Revoke the old password in the Apple ID account, create a new one, update
`FL_CALDAV_APP_PASSWORD` and restart. Use only the dedicated, View Only Apple
ID.

## Backup and restore

Backups are verified copies of the SQLite file taken by a separate job in the
deployment. To restore:

1. Stop the container (the only writer).
2. Copy the chosen backup over the database file; remove any stale `-wal` and
   `-shm` files next to it.
3. Check the restored file (read-only; it is not modified):

   ```bash
   family-logistics verify-db /path/to/family_logistics.db
   ```

   It must report `Integrity: ok` and print the schema revision next to the
   head revision of the image you will start. A revision behind head is
   upgraded by `serve --upgrade-db` at start.
4. Start the container and confirm `/healthz` returns `200`.
5. Run an on-demand assessment and confirm the status card is not stale.

### Rolling back a schema change

Downgrades are explicit and may destroy data, including audit history. Take a
backup first, stop the container, then:

```bash
family-logistics migrate --downgrade --revision <revision>
```

Start the matching older image afterwards; a newer image would upgrade the
schema again.

Test a restore at least monthly.

## Ops alerting

The Apprise client sends only to the six configured `FL_APPRISE_TAG_*` tags and
refuses any other tag before making a request; `ops_alert()` sends operator
alerts to the `fl-ops` tag. Triggering it from the scheduler (failed or
incomplete runs, collection failures, routing budget threshold, database
unreachable) arrives with the scheduler in M3. Until then, rely on the
external monitor of `/healthz`.

## Escalation

1. A run reports `incomplete` or `failed` close to an imminent ride: treat the
   schedule as unverified and confirm rides directly with the drivers.
2. Collection or database failures that persist after a restart: switch to
   `shadow`, take a backup, and investigate from the logs.
3. Suspected credential or data exposure: rotate the affected secret
   immediately and follow [SECURITY.md](https://github.com/ByronWilliamsCPA/family-logistics/blob/main/SECURITY.md).
