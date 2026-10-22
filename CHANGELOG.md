# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- Initial project setup and structure

## [0.1.0] - TBD

### Added
- Initial project structure with uv package management
- Pydantic v2 JSON schema validation
- Structured logging with structlog and rich console output
- Pre-commit hooks (Ruff format, Ruff lint, BasedPyright, Bandit, pip-audit)
- Test suite with pytest and an 80% coverage gate
- GitHub Actions CI/CD pipeline with quality gates
- CLI tool foundation
- License

### Documentation
- README with project overview and quick start
- CONTRIBUTING guidelines with development workflow
- References to ByronWilliamsCPA org-level Security Policy
- References to ByronWilliamsCPA org-level Code of Conduct

### Infrastructure
- uv dependency management with a committed lock file
- pytest test framework with coverage reporting
- GitHub issue tracking and templates
- Automated dependency security scanning (OSV-Scanner, Bandit)
- Code quality enforcement (Ruff, BasedPyright)
- CI/CD pipeline with multiple quality gates

### Security
- Bandit security linting
- OSV-Scanner dependency vulnerability scanning
- Pre-commit hooks for security validation

[Unreleased]: https://github.com/ByronWilliamsCPA/family-logistics/commits/main
[0.1.0]: https://github.com/ByronWilliamsCPA/family-logistics/releases
