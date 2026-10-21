---
title: "Template Feedback"
schema_type: common
status: published
owner: core-maintainer
purpose: "Document template issues for upstream fixes."
tags:
  - feedback
  - template
---

> **Purpose**: Document issues discovered in this project that should be addressed in the [cookiecutter-python-template](https://github.com/ByronWilliamsCPA/cookiecutter-python-template).
>
> **Generated From**: cookiecutter-python-template v0.1.0
> **Project Created**: 2026-10-21

---

## How to Use This File

When working on this project, if you discover any issue that originates from the template itself (not project-specific), add it here with the following format:

````markdown
### [Short Title]

- **Priority**: Critical / High / Medium / Low
- **Category**: [Configuration / Documentation / Tooling / Structure / CI/CD / Security / Other]
- **Discovered**: YYYY-MM-DD

**Issue**: [Clear description of what's wrong or missing]

**Context**: [How was this discovered? What were you trying to do?]

**Suggested Fix**: [What should the template do differently?]

**Affected Files**: [List template files that need changes]
````

---

## Feedback Items

<!-- Add your feedback below this line -->

### Generated baseline fails its own quality gates

- **Priority**: High
- **Category**: Tooling
- **Discovered**: 2026-10-21

**Issue**: A fresh render with include_api_framework=yes, include_database=sqlalchemy_migrations and include_health_checks=yes fails every gate: pytest coverage 56.61% against --cov-fail-under=80, ruff 1 error (PLR0917 in middleware/security.py RateLimitMiddleware.__init__), basedpyright 7 errors (unresolved core.database import, missing `error` arguments in health.py, CorrelationMiddleware.dispatch override incompatible with BaseHTTPMiddleware, MutableHeaders.pop does not exist) and 39 warnings.

**Context**: Generated family-logistics on 2026-10-21 with cruft and ran `uv run pytest`, `uv run ruff check .`, `uv run basedpyright src/` before writing any code.

**Suggested Fix**: Add a template CI job that renders the main option combinations and runs ruff, basedpyright and pytest on each render. Type call_next as starlette's RequestResponseEndpoint, use `del response.headers[...]`, make RateLimitMiddleware options keyword-only.

**Affected Files**: {{cookiecutter.project_slug}}/src/*/api/health.py, middleware/security.py, middleware/correlation.py

### requires-python ignores the python_version choice

- **Priority**: Medium
- **Category**: Configuration
- **Discovered**: 2026-10-21

**Issue**: python_version=3.12 still renders `requires-python = ">=3.10,<3.15"` while ruff target-version and basedpyright pythonVersion are 3.12. uv then resolved for 3.13 in the generated venv.

**Context**: Comparing pyproject.toml sections after generation.

**Suggested Fix**: Render requires-python from python_version (for example `>={{ python_version }},<3.{{ minor+2 }}`) and write a `.python-version` file.

**Affected Files**: {{cookiecutter.project_slug}}/pyproject.toml

### include_database=sqlalchemy_migrations generates no database module

- **Priority**: High
- **Category**: Structure
- **Discovered**: 2026-10-21

**Issue**: The option adds sqlalchemy and alembic to an `api` extra and health.py imports `core.database.get_session`, but no core/database.py, alembic.ini or migrations/ directory is generated, so the import never resolves. database_dialect=sqlite and include_repository_pattern=yes produced nothing either.

**Context**: basedpyright reportMissingImports on health.py.

**Suggested Fix**: Generate core/database.py (engine/session factory honoring database_dialect, WAL pragmas for sqlite), alembic.ini and migrations/env.py, or drop the import when the option is off.

**Affected Files**: hooks/post_gen_project.py, {{cookiecutter.project_slug}}/src/*/core/

### Financial utilities stub generated when include_financial_validators=no

- **Priority**: Low
- **Category**: Structure
- **Discovered**: 2026-10-21

**Issue**: src/<pkg>/utils/financial.py (a one-line docstring) and its ruff per-file-ignore are generated even though financial validators were declined.

**Context**: Reviewing generated files.

**Suggested Fix**: Remove the stub and its pyproject per-file-ignore in post_gen when the option is no.

**Affected Files**: {{cookiecutter.project_slug}}/src/*/utils/financial.py, pyproject.toml

### Runtime API dependencies placed in an optional extra

- **Priority**: Medium
- **Category**: Configuration
- **Discovered**: 2026-10-21

**Issue**: fastapi, uvicorn, sqlalchemy and alembic land in `[project.optional-dependencies].api`, so `uv export --no-dev` for a service image omits the web framework unless extras are requested explicitly.

**Context**: Building the production image.

**Suggested Fix**: When include_api_framework=yes, put these in `[project].dependencies`.

**Affected Files**: {{cookiecutter.project_slug}}/pyproject.toml

### Output directory uses project_slug instead of github_repo_name

- **Priority**: Low
- **Category**: Structure
- **Discovered**: 2026-10-21

**Issue**: cruft create wrote the project to `family_logistics/` (underscore) although github_repo_name renders `family-logistics`; the directory had to be renamed by hand to match the repository.

**Context**: Running `cruft create` for a new repo.

**Suggested Fix**: Name the top-level template directory `{{cookiecutter.github_repo_name}}`.

**Affected Files**: {{cookiecutter.project_slug}}/ (template root directory name)

### Generated Dockerfile and compose ignore homelab supply-chain standards

- **Priority**: Medium
- **Category**: Security
- **Discovered**: 2026-10-21

**Issue**: include_docker=yes renders python:3.12-slim with `uv:latest`, apt-get installs, a curl healthcheck against /health/live, a Postgres service and docker-compose.prod.yml. The homelab standard (ADR-012) is digest-pinned DHI images, pinned uv, distroless runtime with UID 65532, read_only, cap_drop ALL and an exec-form python healthcheck.

**Context**: Replacing the Dockerfile with the vehicle-log pattern from the private deployment repository.

**Suggested Fix**: Offer a `container_base=dhi` option that renders the DHI digest-pinned multi-stage pattern and a hardened compose file; never render `:latest` tags.

**Affected Files**: {{cookiecutter.project_slug}}/Dockerfile, docker-compose.yml, docker-compose.prod.yml

### .env.example lists unrelated third-party tokens

- **Priority**: Low
- **Category**: Documentation
- **Discovered**: 2026-10-21

**Issue**: .env.example ships OpenRouter, Modal, HuggingFace, GCS HMAC, Sentry, Codecov and Sonar placeholders regardless of options, and none of the app's own settings (the settings use a different prefix).

**Context**: Writing the FL_ settings.

**Suggested Fix**: Render only variables for enabled options and derive the settings prefix from the project slug.

**Affected Files**: {{cookiecutter.project_slug}}/.env.example

### template_feedback.md is tracked and has an unrendered placeholder

- **Priority**: Low
- **Category**: Documentation
- **Discovered**: 2026-10-21

**Issue**: the private deployment repository's CLAUDE.md says docs/template_feedback.md is gitignored, but the generated .gitignore does not ignore it, and the header still reads `2026-10-21`.

**Context**: Adding these items.

**Suggested Fix**: Add docs/template_feedback.md to the generated .gitignore and render the creation date.

**Affected Files**: {{cookiecutter.project_slug}}/.gitignore, docs/template_feedback.md

### Template ADR template fails its own front-matter validator

- **Priority**: Medium
- **Category**: Documentation
- **Discovered**: 2026-10-21

**Issue**: The generated docs/ADRs/adr-template.md declares `schema_type: adr`, and the template's own front-matter validator rejects it, so a project fails validation before writing any ADR.

**Context**: Reported by the docs workstream while writing the first family-logistics ADRs.

**Suggested Fix**: Align adr-template.md front matter with the validator schema (or add `adr` to the schema) and validate rendered docs in template CI.

**Affected Files**: {{cookiecutter.project_slug}}/docs/ADRs/adr-template.md, front-matter validator schema

### Generated docs fail markdownlint while no markdownlint hook is configured

- **Priority**: Low
- **Category**: Tooling
- **Discovered**: 2026-10-21

**Issue**: Template docs and README fail markdownlint (MD060, MD022 and others), and the generated .pre-commit-config.yaml has no markdownlint hook, so the violations are never caught.

**Context**: Reported by the docs workstream while running markdownlint on new docs.

**Suggested Fix**: Fix the template markdown and add a pinned markdownlint hook to the generated pre-commit config, as the private deployment repository standards require.

**Affected Files**: {{cookiecutter.project_slug}}/README.md, docs/**, .pre-commit-config.yaml

---

## Submitting Feedback

Once you've collected feedback, you can:

1. **Create an issue** in the [cookiecutter-python-template repository](https://github.com/ByronWilliamsCPA/cookiecutter-python-template/issues)
2. **Submit a PR** if you have fixes for the template
3. **Share this file** with the template maintainers

When submitting, reference this project as the source of the feedback.

## pydoclint config contradicts the template's own docstring convention

- **Issue**: `[tool.pydoclint] arg-type-hints-in-docstring = true` requires types in docstrings, but the generated CLAUDE.md Google-style example puts types only in signatures; the generated code itself fails ~250 pydoclint checks (DOC105/109/110/203).
- **Context**: First `pre-commit run --all-files` on a freshly generated family-logistics project.
- **Suggested Fix**: Default `arg-type-hints-in-docstring = false` (signatures are type-checked by BasedPyright), and make the generated sources pass pydoclint and interrogate out of the box.
- **Priority**: Medium

## Public-repo safety gaps in generated projects

- **Issue**: Generated projects default to things that leak or misfire in a public repository: `infisical_domain` defaults to the owner's homelab hostname (rendered into `.github/workflows/sbom.yml` and `.cruft.json`); README badges use the underscore slug (`family_logistics`) instead of the repo name; `publish-pypi.yml` is always generated even for container-only apps; there is no `.gitleaks.toml` or gitleaks hook (only a local trufflehog hook that silently skips when not installed).
- **Context**: Preparing family-logistics to be published as a public repository with household data kept local.
- **Suggested Fix**: Default `infisical_domain` to a placeholder; render badges from `github_repo_name`; add an `include_pypi_publish` option; ship gitleaks (pinned) with an extendable `.gitleaks.toml`, and an optional out-of-tree deny-list hook for projects that handle personal data.
- **Priority**: High
