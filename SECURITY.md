# Security Policy

## Supported Versions

| Version | Supported |
|---------|-----------|
| 0.1.x   | Yes       |

## Reporting a Vulnerability

Do not open a public GitHub issue for security vulnerabilities, and never
include real household data (names, addresses, schedules, calendar
identifiers, credentials) in any report.

- [Report a vulnerability privately](https://github.com/ByronWilliamsCPA/family-logistics/security/advisories/new)
  through GitHub Security Advisories, or
- email [security@byronwilliams.dev](mailto:security@byronwilliams.dev).

## Response Timeline

We commit to an initial response to all vulnerability reports within 14 days
of submission. In practice:

- Acknowledgement: within 48 hours
- Initial assessment: within 5 business days
- Resolution target: 30 days for critical, 90 days for others

Fixes for an assigned CVE reference the identifier in the commit message
(for example `fix(security): resolve CVE-YYYY-NNNNN`), so the generated
changelog records it.

## Security Surface

Family Logistics handles credentials for a family's calendars and personal
data about children, including children from other families. The project is
at milestone M0: several components below are designed but not built yet.
The Status column says which mitigations exist in this tree today and which
are planned (with the milestone that delivers them).

| Surface | Risk | Mitigation | Status |
|---------|------|------------|--------|
| iCloud CalDAV credentials | An app-specific password unlocks the whole Apple ID it belongs to | A dedicated Apple ID that holds only View Only calendar shares; the password is a runtime secret, never in Git or logs | Deployment control; the setting is `SecretStr` today, the collector that uses it arrives in M1 |
| CalDAV access | Accidental writes to the authoritative calendars | The HTTP transport allows only read methods (`GET`, `HEAD`, `OPTIONS`, `PROPFIND`, `REPORT`) and HTTPS, and rejects anything else before network I/O | Implemented and tested (transport); the collector that will use it arrives in M1 |
| Home Assistant API | Unauthenticated triggers or data injection | Home Assistant will initiate every exchange with a bearer token of at least 32 characters, compared in constant time; the app holds no Home Assistant credential | Token check implemented and tested; the HA routes are planned (M2/M4) and not yet served |
| Registry web UI | Unauthorized edits to people and locations | Single sign-on at the reverse proxy; identity headers will be trusted only from configured proxy addresses (none by default) and only for allow-listed users | Planned (M4) |
| Children's personal data | Exposure of names, addresses and routines | Data minimization per the [privacy and retention spec](docs/privacy/privacy-and-retention-spec.md); protected addresses encrypted at rest; addresses never sent in push notifications or to the LLM | Encryption helper implemented; registry storage and notifications planned (M1-M3) |
| Local LLM | Model output altering results | The deterministic engine is the only authority; LLM output will be able to explain findings but never create, dismiss or downgrade them | Planned (M5); disabled by default |
| This public repository | Household data or site details committed by mistake | Fictional household in all tests and docs; gitleaks rules for private IPs, personal e-mail, phone numbers and street addresses; an out-of-tree private-terms deny-list checked locally and in CI | Implemented |
| Supply chain | Compromised dependencies or images | Locked dependencies (`uv.lock`), digest-pinned base images, SHA-pinned actions, dependency and container scanning, SBOM generation | Implemented |

## Organization Policy

See also the [organization security policy](https://github.com/ByronWilliamsCPA/.github/blob/main/SECURITY.md).
