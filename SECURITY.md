# Security Policy

## Supported Versions

| Version | Supported |
|---------|-----------|
| 0.1.x   | Yes       |

## Reporting a Vulnerability

Do not open a public GitHub issue for security vulnerabilities, and never
include real household data (names, addresses, schedules, calendar
identifiers, credentials) in any report.

[Report a vulnerability privately](https://github.com/ByronWilliamsCPA/family-logistics/security/advisories/new)
through GitHub private vulnerability reporting. This is the only channel for
this repository; reports stay confidential until a fix is published.

## Response Timeline

These are targets, not guarantees, and they match the
[organization security policy](https://github.com/ByronWilliamsCPA/.github/blob/main/SECURITY.md).
All windows run from acknowledgement of the report.

| Stage | Target |
|-------|--------|
| Acknowledgement of report | 5 business days |
| Triage and severity (non-critical) | 10 business days |
| Triage and severity (critical) | 2 business days |
| Fix or mitigation for critical reports | 14 calendar days from acknowledgement |
| Fix released for other severities | 30 calendar days from acknowledgement |

Fixes for an assigned CVE reference the identifier in the commit message
(for example `fix(security): resolve CVE-YYYY-NNNNN`), so the generated
changelog records it.

## Security Surface

Family Logistics handles credentials for a family's calendars and personal
data about children, including children from other families. The main attack
surface follows. The status column says what is true at this commit: the
project is at milestone M0 and the application serves only `/healthz`.

| Surface | Risk | Control | Status |
|---------|------|---------|--------|
| iCloud CalDAV credentials | An app-specific password unlocks the whole Apple ID it belongs to | Operating requirement: a dedicated Apple ID that holds only View Only calendar shares; the password is a runtime secret, never in Git or logs | Operating requirement. Settings hold secrets as `SecretStr`; no collector uses the credential yet (M1) |
| CalDAV access | Accidental writes to the authoritative calendars | The HTTP transport allows only read methods (`GET`, `HEAD`, `OPTIONS`, `PROPFIND`, `REPORT`) and HTTPS, and rejects anything else before network I/O | Implemented and tested; not yet wired into a collector (M1) |
| Home Assistant API | Unauthenticated triggers or data injection | Home Assistant initiates every exchange with a bearer token of at least 32 characters, compared in constant time; the app holds no Home Assistant credential | Token check implemented but not wired: no Home Assistant route is registered yet (planned M2 to M4) |
| Registry web UI | Unauthorized edits to people and locations | Single sign-on at the reverse proxy; identity headers trusted only from configured proxy addresses (none by default) and only for allow-listed users | Planned (M4): the settings exist but nothing consumes them and no UI exists |
| Children's personal data | Exposure of names, addresses and routines | Data minimization per the [privacy and retention spec](docs/privacy/privacy-and-retention-spec.md); protected addresses encrypted at rest; addresses never sent in push notifications or to the LLM; the `ha_relay` route provider sends Home Assistant a zone id, and sends a plaintext address for a zone-less location only when it is not another family's home (the home of a registered contact), so such a home without a zone never leaves the app and rides that need it stay incomplete (ROUTE-010); an external passenger's default pickup or drop-off counts as a venue, so a passenger's home is protected only when a contact from that household is registered with it as home or it has a zone | Address encryption (Fernet) implemented but not yet used by any storage; notification and LLM exclusions are design rules for M3 and M5 and are not yet enforced in code; the Home Assistant endpoint rule is implemented and tested as a pure policy function (`domain/address_release.py`) but no provider calls it until `ha_relay` lands (M3) |
| Local LLM | Model output altering results | The deterministic engine is the only authority; LLM output can explain findings but never create, dismiss or downgrade them | Design rule ([ADR 0001](docs/ADRs/0001-deterministic-authority-and-llm-boundary.md)); the LLM adapter is not implemented (planned M5) |
| This public repository | Household data or site details committed by mistake | Fictional household in all tests and docs; gitleaks rules for private IPs, personal e-mail, phone numbers and street addresses; an out-of-tree private-terms deny-list | Implemented: pre-commit hooks locally and the Private Data Guard workflow in CI |
| Supply chain | Compromised dependencies or images | Locked dependencies (`uv.lock`), digest-pinned base images, SHA-pinned actions, dependency and container scanning, SBOM generation | Implemented in the CI workflows |

## Organization Policy

See also the [organization security policy](https://github.com/ByronWilliamsCPA/.github/blob/main/SECURITY.md).
