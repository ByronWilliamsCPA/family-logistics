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
data about children, including children from other families. The main attack
surface and its mitigations:

| Surface | Risk | Mitigation |
|---------|------|------------|
| iCloud CalDAV credentials | An app-specific password unlocks the whole Apple ID it belongs to | A dedicated Apple ID that holds only View Only calendar shares; the password is a runtime secret, never in Git or logs |
| CalDAV access | Accidental writes to the authoritative calendars | The HTTP transport allows only read methods (`GET`, `HEAD`, `OPTIONS`, `PROPFIND`, `REPORT`) and rejects any other method before network I/O; tested |
| Home Assistant API | Unauthenticated triggers or data injection | Home Assistant initiates every exchange with a bearer token of at least 32 characters, compared in constant time; the app holds no Home Assistant credential |
| Registry web UI | Unauthorized edits to people and locations | Single sign-on at the reverse proxy; identity headers are trusted only from configured proxy addresses (none by default) and only for allow-listed users |
| Children's personal data | Exposure of names, addresses and routines | Data minimization per the [privacy and retention spec](docs/privacy/privacy-and-retention-spec.md); protected addresses encrypted at rest; addresses never sent in push notifications or to the LLM |
| Local LLM | Model output altering results | The deterministic engine is the only authority; LLM output can explain findings but never create, dismiss or downgrade them |
| This public repository | Household data or site details committed by mistake | Fictional household in all tests and docs; gitleaks rules for private IPs, personal e-mail, phone numbers and street addresses; an out-of-tree private-terms deny-list checked locally and in CI |
| Supply chain | Compromised dependencies or images | Locked dependencies (`uv.lock`), digest-pinned base images, SHA-pinned actions, dependency and container scanning, SBOM generation |

## Organization Policy

See also the [organization security policy](https://github.com/ByronWilliamsCPA/.github/blob/main/SECURITY.md).
