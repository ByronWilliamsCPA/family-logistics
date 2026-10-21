# Contributing to Family Logistics

Thank you for your interest in contributing to Family Logistics! This document provides guidelines and instructions for contributing to the project.

## Table of Contents

- [Code of Conduct](#code-of-conduct)
- [Getting Started](#getting-started)
- [Development Workflow](#development-workflow)
- [Code Quality Standards](#code-quality-standards)
- [Testing Requirements](#testing-requirements)
- [Commit Convention](#commit-convention)
- [Pull Request Process](#pull-request-process)
- [Questions?](#questions)

## Code of Conduct

This project adheres to the [ByronWilliamsCPA organization Code of Conduct](https://github.com/ByronWilliamsCPA/.github/blob/main/CODE_OF_CONDUCT.md). By participating, you are expected to uphold this code. Please report unacceptable behavior to <byron@byronwilliams.dev>.

## Getting Started

### Prerequisites

- Python 3.12 or higher
- UV 1.7+ for dependency management
- Git
- GPG key configured for commit signing (recommended)

### Development Environment Setup

```bash
# Clone the repository
git clone https://github.com/ByronWilliamsCPA/family-logistics.git
cd family-logistics

# Install dependencies with UV
uv sync --all-extras
# Setup pre-commit hooks (REQUIRED)
uv run pre-commit install

# Install Qlty CLI for unified code quality checks (REQUIRED)
curl https://qlty.sh | bash

# Verify installation
uv run pytest -v
qlty check
```

### Project Structure

See [Project layout in the README](README.md#project-layout).

### Household data stays out of Git

This repository is public. Tests, fixtures and documentation use only the
fictional household (parents Morgan and Riley; children Alex, Sam, Jordan and
Casey; "Passenger E" and "Driver S" from other families). Never commit real
names, addresses, coordinates, calendar identifiers, phone numbers, e-mail
addresses or site network details. gitleaks and the private-terms pre-commit
hook check every commit; keep your deny-list at
`~/.config/family-logistics/private-terms.txt`.

## Development Workflow

### 1. Create a Feature Branch

```bash
# Create and checkout a new branch
git checkout -b feature/your-feature-name

# For bug fixes
git checkout -b fix/issue-description

# For documentation
git checkout -b docs/documentation-update
```

### Branch Naming Convention

- `feature/` - New features or enhancements
- `fix/` - Bug fixes
- `docs/` - Documentation updates
- `refactor/` - Code refactoring
- `test/` - Test additions or improvements
- `chore/` - Maintenance tasks

### 2. Make Your Changes

- Write clean, readable code following [PEP 8](https://peps.python.org/pep-0008/)
- Add type hints to all functions and methods
- Update documentation for API changes
- Add tests for new functionality
- Ensure all tests pass locally

### 3. Run Quality Checks

Before committing, ensure all quality checks pass:

```bash
# Run all quality checks with Qlty (recommended)
qlty check

# Or run checks on only changed files (fastest)
qlty check --filter=diff

# Auto-format code
qlty fmt
# Or run all pre-commit hooks manually
uv run pre-commit run --all-files

# Run tests with coverage
uv run pytest --cov=family_logistics --cov-report=term-missing
```

**Note**: Qlty consolidates all quality tools (Ruff, BasedPyright, Bandit, Markdownlint, etc.) into a single fast CLI. See [`.qlty/qlty.toml`](.qlty/qlty.toml) for configuration.

## Code Quality Standards

All contributions MUST meet these requirements:

### Formatting

- **Tool**: Ruff
- **Line Length**: 88 characters
- **Indentation**: 4 spaces (no tabs)
- **Imports**: Sorted with Ruff
- **Quotes**: Double quotes for strings
- **Verification**: `uv run ruff format src tests`

### Linting

- **Tool**: Ruff with project configuration
- **Rules**: PyStrict-aligned rule set (see `pyproject.toml`)
- **Auto-fix**: `uv run ruff check --fix src tests`
- **Verification**: `uv run ruff check src tests`

### Type Checking

- **Tool**: BasedPyright strict mode for `src/`
- **Coverage**: All public functions must have type hints
- **Verification**: `uv run basedpyright src`

### Security

- **No Hardcoded Secrets**: Use environment variables or secure vaults
- **Input Validation**: Validate all user inputs and file paths
- **Path Sanitization**: Use `pathlib.Path.resolve()` to prevent directory traversal
- **Dependency Security**: Run `uv run pip-audit` before submitting PRs

### Dependency Management

This project uses secure package indexes to protect against supply chain attacks:

Dependencies resolve from PyPI and are locked with hashes in `uv.lock`.
Optional Assured OSS and internal indexes are declared in `pyproject.toml` as
explicit indexes and are used only for packages routed to them in
`[tool.uv.sources]` (none today).

**Adding New Dependencies:**

```bash
# Add a dependency
uv add package-name

# Add a dev dependency
uv add --dev package-name

# Verify dependency security
uv run pip-audit
```

**Guidelines for Dependencies:**

- Prefer packages available in Google Assured OSS when possible
- Check package provenance before adding new dependencies
- Pin specific versions for production dependencies
- Review transitive dependencies for security concerns
- Document why each dependency is needed in PR descriptions

**Checking Package Availability:**

```bash
# Check if package is in Assured OSS
pip index versions package-name --index-url https://us-python.pkg.dev/cloud-aoss/cloud-aoss-python/simple

# View package sources
uv pip compile pyproject.toml --verbose 2>&1 | grep package-name
```

**Internal Packages:**

To use a package from the internal registry, add it to `[tool.uv.sources]` in `pyproject.toml`:

```toml
[tool.uv.sources]
internal-package = { index = "internal" }
```

### Type Hints

All functions must include type hints:

```python
from pathlib import Path
from typing import Optional, Any


def process_data(
    input_path: Path,
    output_dir: Optional[Path] = None,
) -> dict[str, Any]:
    """Process data and return results.

    Args:
        input_path: Path to the input data
        output_dir: Optional output directory for results

    Returns:
        Dictionary containing processed data and metadata

    Raises:
        FileNotFoundError: If input_path does not exist
        ValueError: If data format is not supported
    """
    pass
```

### Documentation

- **Docstrings**: Use Google-style docstrings for all public APIs
- **Comments**: Explain *why*, not *what* (code should be self-documenting)
- **README Updates**: Update README.md for significant feature additions
- **Architecture Docs**: Add/update ADRs for architectural changes

## Testing Requirements

### Testing Policy

All new functionality MUST include corresponding tests:

- **Unit tests**: Required for all new functions/classes
- **Integration tests**: Required for new modules/workflows
- **Coverage**: Must maintain ≥80% overall coverage
- **Test types**: Use pytest markers (`@pytest.mark.unit`, `@pytest.mark.integration`)

### Test Guidelines

- Test both success and failure cases
- Test edge cases and boundary conditions
- Use descriptive test names: `test_<function>_<scenario>_<expected>`
- Include docstrings explaining test purpose
- Use fixtures for common setup

### Minimum Coverage

- **Overall Coverage**: 80% minimum (enforced by CI)
- **New Code**: 90% coverage for new features
- **Critical Paths**: 100% coverage for security-sensitive code

### Test Categories

```bash
# Run all tests
uv run pytest -v

# Run only unit tests
uv run pytest -v -m unit

# Run only integration tests
uv run pytest -v -m integration

# Run tests with coverage report
uv run pytest --cov=family_logistics --cov-report=html
# Open htmlcov/index.html to view coverage report

# Run specific test file
uv run pytest tests/unit/test_parser.py -v
```

### Writing Tests

Use the shared factories in `tests/fl_factories.py` and the fictional
household. For example, a parser test:

```python
import pytest
from fl_factories import notes, parse


@pytest.mark.unit
def test_misspelled_family_name_is_rejected_with_suggestion():
    result = parse(notes(family="Sma", destinations=(("Sma", "HOME"),)))
    assert not result.ok
    assert any(f.rule_id == "NAME-002" for f in result.findings)
```

## Commit Convention

We follow [Conventional Commits](https://www.conventionalcommits.org/) specification:

### Commit Message Format

```text
<type>(<scope>): <subject>

<body>

<footer>
```

### Types

- `feat`: New feature
- `fix`: Bug fix
- `docs`: Documentation changes
- `style`: Code style changes (formatting, no logic changes)
- `refactor`: Code refactoring
- `test`: Adding or updating tests
- `chore`: Maintenance tasks
- `perf`: Performance improvements
- `ci`: CI/CD changes

### Examples

```bash
# Feature addition
feat(core): add data processing function

Implements new data processing capabilities with input
validation and error handling.

Refs: #42

# Bug fix
fix(utils): handle edge case in validation

Previously, certain input patterns caused incorrect results.
Now we properly handle all edge cases.

Fixes: #56

# Breaking change
feat(core)!: redesign API for better ergonomics

BREAKING CHANGE: API has been redesigned for improved usability.
See migration guide in docs/migration/v1.0.0.md
```

## Pull Request Process

### Before Submitting

- [ ] Branch is up-to-date with `main`
- [ ] All tests pass locally
- [ ] Code coverage meets minimum requirements (80%)
- [ ] Pre-commit hooks pass
- [ ] Documentation is updated
- [ ] Commits follow conventional commit format

### Submitting a Pull Request

1. **Push your branch**:

   ```bash
   git push origin feature/your-feature-name
   ```

2. **Create Pull Request** on GitHub with:
   - **Clear title**: Following conventional commit format
   - **Description**: What changes were made and why
   - **Issue reference**: `Fixes #123` or `Refs #456`
   - **Testing notes**: How reviewers can test the changes
   - **Breaking changes**: Clearly documented (if applicable)

3. **Wait for CI checks**:
   - All GitHub Actions workflows must pass
   - Test coverage must meet requirements

4. **Address review feedback**:
   - Respond to all reviewer comments
   - Push additional commits to the same branch
   - Request re-review when ready

5. **Merge**:
   - Maintainer will merge when approved
   - Follow conventional commits for final commit message

### PR Review Criteria

Reviewers will check:

- **Code Quality**: Follows style guide and best practices
- **Tests**: Adequate test coverage and meaningful tests
- **Documentation**: Clear docstrings and updated docs
- **Security**: No security vulnerabilities introduced
- **Performance**: No performance regressions
- **Compatibility**: Maintains backward compatibility (or documents breaking changes)

## Reporting Issues

### Reporting Bugs

Use the bug report template and include:

- **Description**: Clear description of the bug
- **Reproduction Steps**: Minimal steps to reproduce
- **Expected Behavior**: What should happen
- **Actual Behavior**: What actually happens
- **Environment**: Python version, OS, package versions
- **Logs**: Relevant error messages or stack traces

### Requesting Features

Use the feature request template and include:

- **Use Case**: Why is this feature needed?
- **Proposed Solution**: How should it work?
- **Alternatives**: Other approaches considered
- **Additional Context**: Screenshots or examples

## Questions?

- **General Questions**: Open a [GitHub Discussion](https://github.com/ByronWilliamsCPA/family-logistics/discussions)
- **Bug Reports**: Open a [GitHub Issue](https://github.com/ByronWilliamsCPA/family-logistics/issues)
- **Security Issues**: See [ByronWilliamsCPA Security Policy](https://github.com/ByronWilliamsCPA/.github/blob/main/SECURITY.md)
- **Email**: <byron@byronwilliams.dev>

## Recognition

Contributors are recognized in:

- Repository contributors page
- Release notes for significant contributions
- Project documentation

Thank you for contributing to Family Logistics!
