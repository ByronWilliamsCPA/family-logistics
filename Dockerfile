# =============================================================================
# family-logistics: transportation assurance API and scheduler
# =============================================================================
# Distroless runtime with tag-and-digest DHI base-image pins, and a uv export
# with hashes from uv.lock. Published by CI as
# ghcr.io/byronwilliamscpa/family-logistics:sha-<7>.
#
# #ASSUME external resources: the build needs the GHCR mirror for both pinned
# DHI base images, ghcr.io/astral-sh for uv, and PyPI for locked wheels. An
# outage fails the build, never a running container.
# #VERIFY: `docker build .` exits 0 in CI; a wheel whose hash differs from
# uv.lock fails the build rather than installing silently. Nothing else is
# fetched: the project is copied, not built (see Stage 1).
# #VERIFY: Renovate's dockerfile manager proposes each base-image digest
# refresh for the pinned tag as a reviewed pull request (never automerged),
# and the container-image workflow builds every change and scans it with Trivy.

# -----------------------------------------------------------------------------
# Stage 1: builder. The -dev variant has a shell and package tooling.
# Source: dhi.io/python 3.12 debian13 -dev, mirrored at
# ghcr.io/byronwilliamscpa/dhi-python. The digest is what is pulled; the tag
# only names the variant for review and for Renovate.
# Digests refreshed 2026-10-21 (3.12-debian13-dev / 3.12-debian13) for fixed
# HIGH CVEs in sqlite, openssl and util-linux.
# -----------------------------------------------------------------------------
FROM ghcr.io/byronwilliamscpa/dhi-python:3.12-debian13-dev@sha256:a023ddf6381a557057fc1a003ebd3bc95463cedee8955bd2744db1add26a7f0c AS builder

WORKDIR /build
COPY --from=ghcr.io/astral-sh/uv:0.8.17@sha256:e4644cb5bd56fdc2c5ea3ee0525d9d21eed1603bccd6a21f887a938be7e85be1 /uv /usr/local/bin/uv

COPY pyproject.toml uv.lock README.md ./

# Locked runtime dependencies only (no dev extras), hash-checked, installed into
# a plain target directory: the distroless runtime has no venv activation and
# its interpreter path need not match the builder's.
RUN uv export --frozen --no-dev --no-emit-project --output-file /tmp/requirements.txt && \
    uv pip install --no-cache --require-hashes --target /opt/app -r /tmp/requirements.txt

# The project is copied, not built: building a wheel would fetch the build
# backend from PyPI without a pin or hash. ENTRYPOINT runs `python -m`, and
# __version__ is static, so no installed metadata is needed. Bytecode caches
# from a local checkout are dropped so the image matches a clean build.
COPY src/family_logistics/ /opt/app/family_logistics/
RUN find /opt/app/family_logistics -name __pycache__ -type d -prune -exec rm -rf {} +

# -----------------------------------------------------------------------------
# Stage 2: runtime. Distroless, no shell, nonroot.
# Source: dhi.io/python 3.12 debian13, mirrored at
# ghcr.io/byronwilliamscpa/dhi-python. Tag and digest, as above.
# -----------------------------------------------------------------------------
FROM ghcr.io/byronwilliamscpa/dhi-python:3.12-debian13@sha256:5484dba6d3ba3feebcecd30804b3c795446d63643b6062bc7e8e2d54836f8aef

LABEL org.opencontainers.image.title="family-logistics"
LABEL org.opencontainers.image.description="Deterministic family transportation assurance"
LABEL org.opencontainers.image.source="https://github.com/ByronWilliamsCPA/family-logistics"
LABEL org.opencontainers.image.licenses="MIT"

COPY --from=builder /opt/app /opt/app
WORKDIR /app

ENV PYTHONPATH=/opt/app \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    FL_DATABASE_PATH=/data/family_logistics.db \
    FL_BIND_HOST=0.0.0.0 \
    FL_PORT=8080 \
    FL_JSON_LOGS=true

# Bare numeric UID: no /etc/passwd entry needed on distroless. The host state
# directory /data/family-logistics is created owned by 65532 before the first
# deploy.
USER 65532:65532

EXPOSE 8080

# Exec form is required on distroless (no shell). The probe calls GET /healthz
# on loopback and exits non-zero when the API or database is down.
HEALTHCHECK --interval=30s --timeout=5s --retries=3 --start-period=30s \
    CMD ["python", "-m", "family_logistics", "healthcheck"]

ENTRYPOINT ["python", "-m", "family_logistics"]
# The app is the database's only writer and owns its schema: it upgrades to the
# packaged head at start, atomically and under the database's write lock, and
# refuses a schema newer than this image. Downgrades are an explicit operator
# action (`migrate --downgrade --revision <rev>`, with confirmation).
CMD ["serve", "--upgrade-db"]
