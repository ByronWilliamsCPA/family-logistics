---
title: "OpenSSF Compliance"
schema_type: common
status: published
owner: core-maintainer
purpose: "Current OpenSSF Scorecard and Best Practices posture for Family Logistics, and the work left to reach the target."
tags:
  - security
  - compliance
  - quality
---

Family Logistics targets an [OpenSSF Scorecard](https://github.com/ossf/scorecard)
score of at least 7.0 (goal 8.5) and the passing level of the
[OpenSSF Best Practices Badge](https://www.bestpractices.dev/). The repository
became public on 2026-10-21, so live scores are not available yet; the table
below records the expected posture and the open work.

## Scorecard checks

| Check | Expected state | Notes |
|-------|----------------|-------|
| Branch-Protection | Met | Organization ruleset `ByronWilliamsCPA-default-branch-baseline` on `main`: pull requests, three required status checks, signed commits, linear history, no force pushes, no deletion; zero approving reviews required |
| Code-Review | Capped | Solo-maintainer project; reviews are not required by count |
| CI-Tests | Met | Unit and scenario tests run on every pull request |
| Dangerous-Workflow | Met | No `pull_request_target`; `qlty.yml` uses `workflow_run` but only consumes coverage artifacts from same-repository runs and never checks out pull request code |
| Dependency-Update-Tool | Met | Renovate manages Python, GitHub Actions, Docker and pre-commit pins |
| Fuzzing | In progress | Hypothesis property tests over the notes parser and validation functions; no dedicated fuzzing harness |
| License | Met | MIT, REUSE-compliant |
| Maintained | Pending | Scorecard scores repositories younger than 90 days as 0 |
| Packaging | Pending | The release workflow publishes a container image to GHCR |
| Pinned-Dependencies | Met | Actions and reusable workflows pinned to commit SHAs; base images pinned by digest; `uv.lock` committed |
| SAST | Partial | Bandit in the security workflow and SonarCloud in `sonarcloud.yml`; Semgrep is not run (`.semgrep.yml` is unused), and the Scorecard check scores only the analyzers it recognizes |
| Security-Policy | Met | [SECURITY.md](https://github.com/ByronWilliamsCPA/family-logistics/blob/main/SECURITY.md) with private reporting, response-time targets and a security-surface summary |
| Signed-Releases | Pending | Container images are attested with build provenance at release |
| Token-Permissions | Met | Read-only defaults; write scopes granted per job |
| Vulnerabilities | Met | OSV-Scanner and dependency review on locked dependencies; Trivy container scanning; `pip-audit` is a manual developer check, not a CI step |

## Best Practices Badge

The badge project must be filed by a maintainer at
[bestpractices.dev](https://www.bestpractices.dev/en/projects/new) for
`https://github.com/ByronWilliamsCPA/family-logistics`. Evidence for the
passing criteria:

- **Basics**: README, [CONTRIBUTING.md](https://github.com/ByronWilliamsCPA/family-logistics/blob/main/CONTRIBUTING.md),
  MIT license, public issue tracker, documentation in `docs/`. The MkDocs site is built in CI but not yet
  published: GitHub Pages is not enabled and `docs.yml` sets `deploy-to-pages: false`.
- **Change control**: Git on GitHub, semantic versioning, generated changelog.
- **Reporting**: private vulnerability reporting and response-time targets in
  SECURITY.md.
- **Quality**: automated tests with an 80% coverage gate, Ruff, BasedPyright
  strict mode, pre-commit hooks.
- **Security**: dependency, secret (gitleaks) and static analysis scanning;
  read-only calendar access by design; secrets supplied only at runtime.
- **Analysis**: Bandit and OSV-Scanner in the security workflow, SonarCloud analysis, and Trivy container
  scanning in CI.

## Repository settings to verify

These need a repository administrator:

- Secret scanning and push protection enabled.
- Private vulnerability reporting enabled (required for the advisory link in
  SECURITY.md to work).
- Dependabot alerts: currently **disabled** (the `vulnerability-alerts` API returned 404 on 2026-10-22). Enable them
  under Settings, Advanced Security, Dependabot alerts. Version updates come from Renovate; the alerts only add
  advisory notifications on top of OSV-Scanner.
- Workflow runs from forks require approval for outside contributors.
- The `FL_PRIVATE_TERMS` secret is set so the private-terms check runs.

## Verifying a release image

Release images carry GitHub build-provenance attestations:

```bash
gh attestation verify oci://ghcr.io/byronwilliamscpa/family-logistics:<tag> \
  --owner ByronWilliamsCPA
```

## Resources

- [OpenSSF Scorecard checks](https://github.com/ossf/scorecard/blob/main/docs/checks.md)
- [Best Practices Badge criteria](https://www.bestpractices.dev/en/criteria/0)
- [SLSA](https://slsa.dev/)
