---
title: "Configuration"
schema_type: common
status: published
owner: core-maintainer
purpose: "Every FL_ setting Family Logistics reads, with defaults."
tags:
  - guide
  - configuration
---

Family Logistics reads its configuration from environment variables with the
`FL_` prefix (Pydantic Settings, `family_logistics.core.config.Settings`). A
`.env` file in the working directory is also read, after the process
environment, which always wins; it is meant for local development, and
production values come from the deployment's secret store. A blank value
(`FL_X=`) counts as unset, so a template line or a compose default leaves the
setting at its default (or, for a required secret, fails closed with a clear
message). Start from
[`.env.example`](https://github.com/ByronWilliamsCPA/family-logistics/blob/main/.env.example).
Secrets are supplied only at runtime and are never printed: `family-logistics config`
shows non-secret values only.

## Runtime

| Variable | Default | Purpose |
|----------|---------|---------|
| `FL_LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR` or `CRITICAL` |
| `FL_JSON_LOGS` | `false` | Emit JSON logs; the container image sets `true` |
| `FL_INCLUDE_TIMESTAMP` | `true` | Include timestamps in logs |
| `FL_BIND_HOST` | `127.0.0.1` | Listen address; the container image sets `0.0.0.0` |
| `FL_PORT` | `8080` | Listen port |
| `FL_TIMEZONE` | `America/Los_Angeles` | IANA zone for human-facing times; storage is UTC |
| `FL_DATABASE_PATH` | `/data/family_logistics.db` | SQLite database (WAL mode, single writer) |
| `FL_REGISTRY_SEED` | unset | Private registry seed YAML, kept outside the repository; `registry load` reads it when no path is given |

## Calendars (read-only)

| Variable | Default | Purpose |
|----------|---------|---------|
| `FL_CALDAV_URL` | `https://caldav.icloud.com` | CalDAV base URL; must use `https://` |
| `FL_CALDAV_ALLOWED_HOSTS` | `icloud.com` | Comma-separated registrable domains the collector may reach, for the first request and every redirect hop; a host matches the domain or any subdomain (`p42-caldav.icloud.com`), never a lookalike (`evil-icloud.com`). Entries are bare domains: no scheme, path, port or wildcard. `FL_CALDAV_URL` must be inside it |
| `FL_CALDAV_USERNAME` | unset | Dedicated Apple ID holding View Only calendar shares |
| `FL_CALDAV_APP_PASSWORD` | unset | **Secret.** App-specific password for that Apple ID |
| `FL_CALDAV_CALENDARS` | empty | Calendars to collect, by display name or calendar ID (comma-separated); unlisted calendars are never read, and a calendar removed from the list stops being collected without raising DATA-011. An individual's calendar must also be in the registry seed with its owner, or it raises DATA-011 as unregistered |
| `FL_CALDAV_TRANSPORT_CALENDAR` | `Family Transportation` | Display name of the shared ride calendar (registered as the transport calendar) |
| `FL_COLLECT_DAYS_BACK` | `2` | Local days before today in each collection window |
| `FL_COLLECT_DAYS_AHEAD` | `14` | Local days after today in each collection window |
| `FL_SOURCE_MAX_STALENESS_MINUTES` | `120` | A calendar not collected completely within this time raises DATA-011 (incomplete) |

## Home Assistant and the registry UI

| Variable | Default | Purpose |
|----------|---------|---------|
| `FL_HA_TOKEN` | unset | **Secret.** Bearer token Home Assistant presents; at least 32 characters; `serve` refuses to start without it |
| `FL_HA_SCHOOLS` | empty | JSON list of schools whose schedules Home Assistant pushes to `POST /api/v1/ingest/ha-snapshot`. Each entry has `location_id` (registry location), `person_ids` (registry people who attend it, 1 to 16; a person belongs to one school), `bell_calendars` and `district_calendars` (`calendar.*` entity IDs, up to 8 each, at least one in total). A bell calendar belongs to one school; only a district calendar may serve several. A repeated JSON key is rejected rather than silently dropped. Only listed calendars are accepted; empty accepts none and the ingest answers 503. At most 8 schools. Keep real entity IDs in the private deployment repository. See [Home Assistant school snapshot](ha-school-snapshot.md) |
| `FL_PROXY_TRUSTED_CIDRS` | empty | Reverse-proxy addresses whose identity headers are believed; empty trusts none |
| `FL_UI_ALLOWED_USERS` | empty | Single sign-on usernames allowed into the registry UI; empty denies everyone |
| `FL_DATA_KEY` | unset | **Secret.** Fernet key encrypting protected addresses at rest; required before any address is stored, and `serve` refuses to start without it when `FL_NOTIFICATION_MODE=production` |
| `FL_AUDIT_KEY` | unset | **Secret.** HMAC-SHA256 key for the audit-log state hashes: 32 random bytes as url-safe base64 (44 characters, `python -c "import base64, secrets; print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())"`); must differ from `FL_DATA_KEY`; no audit row is written without it, and `serve` refuses to start without it when `FL_NOTIFICATION_MODE=production` |
| `FL_AUDIT_KEY_VERSION` | `1` | Version stored with every audit row so a key rotation can be told apart; at least 1 |

## Notifications

| Variable | Default | Purpose |
|----------|---------|---------|
| `FL_APPRISE_URL` | unset | Apprise API base URL (`http` or `https`, no embedded credentials); `serve` refuses to start without it when `FL_NOTIFICATION_MODE=production` |
| `FL_APPRISE_CONFIG_KEY` | `family-logistics` | Key of the saved Apprise configuration holding the app's services; 1 to 64 letters, digits, `_` or `-` |
| `FL_APPRISE_TAG_REPORT` | `fl-report` | Weekly and daily report e-mail |
| `FL_APPRISE_TAG_STATUS` | `fl-status` | Completed-assessment push |
| `FL_APPRISE_TAG_WARNING` | `fl-warning` | New validation error push |
| `FL_APPRISE_TAG_URGENT` | `fl-urgent` | Critical issue on an imminent ride (bypasses quiet hours) |
| `FL_APPRISE_TAG_OPS` | `fl-ops` | Service-health alerts to the operator |
| `FL_APPRISE_TAG_SHADOW` | `fl-shadow` | All output while in shadow mode |
| `FL_NOTIFICATION_MODE` | `shadow` | `shadow` or `production`; production needs both parents' approval |
| `FL_QUIET_HOURS_START` | `22:00` | Start of quiet hours (local time) |
| `FL_QUIET_HOURS_END` | `06:00` | End of quiet hours (local time) |
| `FL_IMMINENT_WINDOW_MINUTES` | `180` | Rides starting within this window may bypass quiet hours; an unverified destination on them is critical |
| `FL_SAME_LOCATION_GAP_MINUTES` | `60` | Largest idle gap between activities at one place that still counts as one stay (no ride needed) |
| `FL_OVERNIGHT_GAP_HOURS` | `14` | Largest idle gap between stays at one place on consecutive days that still counts as an overnight (no return home, no morning trip) |
| `FL_TRAVEL_BUFFER_MINUTES` | `10` | Arrive this long before a passenger's next activity |
| `FL_STOP_BUFFER_MINUTES` | `5` | Loading or unloading time per ride stop |

The client sends through one saved Apprise configuration: `POST
/notify/{FL_APPRISE_CONFIG_KEY}` with a JSON body of `title`, `body` and the
`fl-*` tag. The key selects the configuration; the tag selects the services
inside it. The key is not a tag.

Recipients for each tag are configured in Apprise, never in this application.
Every `FL_APPRISE_TAG_*` value must be one plain dedicated tag,
`fl-<word>[-<word>]` in lower case with no tag expression, and the six must
differ, or settings fail to load. The Apprise client also refuses any tag other
than these six before making a request, so shared severity tags can never be
used.

## Routing

| Variable | Default | Purpose |
|----------|---------|---------|
| `FL_ROUTE_PROVIDER` | `ha_relay` | `ha_relay`, `google_routes` or `static`. Until the relay lands (ADR 0006, PR 2), `ha_relay` yields a provider whose every estimate is unknown (ROUTE-010, run incomplete), `google_routes` is refused as not implemented, and `static` is for tests and needs an explicit travel-time table |
| `FL_GOOGLE_ROUTES_API_KEY` | unset | **Secret.** Optional paid routing key |
| `FL_ROUTES_MONTHLY_BUDGET_USD` | `10` | Hard monthly cap enforced by the application |
| `FL_ROUTES_BUDGET_WARN_USD` | `8` | Spend that triggers an operator warning |

## Scheduling

| Variable | Default | Purpose |
|----------|---------|---------|
| `FL_WEEKLY_CRON` | `0 15 * * SUN` | Weekly review (local time) |
| `FL_DAILY_CRON` | `0 19 * * *` | Daily review (local time) |
| `FL_CHANGE_SCAN_MINUTES` | `60` | Incremental change scan interval (minimum 5) |

## Narrative LLM (M5)

| Variable | Default | Purpose |
|----------|---------|---------|
| `FL_LLM_ENABLED` | `false` | Attempt the narrative call |
| `FL_LLM_BASE_URL` | unset | OpenAI-compatible base URL |
| `FL_LLM_API_KEY` | unset | **Secret.** Bearer key for the inference route |
| `FL_LLM_MODEL` | unset | Model identifier |

## Accessing settings in code

```python
from family_logistics.core.config import get_settings

settings = get_settings()
print(settings.notification_mode)
```
