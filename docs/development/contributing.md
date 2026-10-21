---
title: "Contributing"
schema_type: common
status: published
owner: core-maintainer
purpose: "Contributing guide for Family Logistics."
tags:
  - development
  - contributing
---

Thank you for your interest in contributing to Family Logistics!

## Getting Started

### Prerequisites

- Python 3.12+
- UV package manager
- Git

### Development Setup

```bash
# Clone the repository
git clone https://github.com/ByronWilliamsCPA/family-logistics
cd family-logistics

# Install dependencies
uv sync --all-extras

# Install pre-commit hooks
uv run pre-commit install
```

## Household data stays out of Git

This repository is public. Use only the fictional household in tests, fixtures
and documentation (parents Morgan and Riley; children Alex, Sam, Jordan and
Casey; "Passenger E" and "Driver S" from other families). Never commit real
names, addresses, coordinates, calendar identifiers, phone numbers, e-mail
addresses or site network details. gitleaks and the private-terms hook check
every commit; keep your own deny-list at
`~/.config/family-logistics/private-terms.txt`.

## Development Workflow

### 1. Create a Branch

```bash
git checkout -b feat/your-feature-name
```

### 2. Make Changes

- Write code with type hints
- Add tests for new functionality
- Update documentation

### 3. Run Quality Checks

```bash
# Run tests
uv run pytest -v

# Run linting
uv run ruff check .

# Run type checking
uv run basedpyright src/
```

### 4. Commit Changes

Use conventional commit messages:

```bash
git commit -m "feat: add new feature"
git commit -m "fix: resolve bug in module"
git commit -m "docs: update documentation"
```

### 5. Create Pull Request

Push your branch and create a PR against `main`.

## Code Review

All contributions require:

- Passing CI checks
- Code review approval
- Documentation updates (if applicable)

## Questions?

Open an issue for questions or discussions.
