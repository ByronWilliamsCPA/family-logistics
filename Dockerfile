# =============================================================================
# family-logistics: transportation assurance API and scheduler
# =============================================================================
# Distroless runtime with digest-only DHI base-image pins, and a uv export with
# hashes from uv.lock. Published by CI as
# ghcr.io/byronwilliamscpa/family-logistics:sha-<7>.
#
# #ASSUME external resources: the build needs the GHCR mirror for both pinned
# DHI base images, ghcr.io/astral-sh for uv, and PyPI for locked wheels. An
# outage fails the build, never a running container.
# #VERIFY: `docker build .` exits 0 in CI; a wheel whose hash differs from
# uv.lock fails the build rather than installing silently.
# #VERIFY: both digests below match the approved base-image lock before the
# first release.

# -----------------------------------------------------------------------------
# Stage 1: builder. The -dev variant has a shell and package tooling.
# Source: dhi.io/python 3.12 debian13 -dev, mirrored at
# ghcr.io/byronwilliamscpa/dhi-python. Digest-only pin: the tag floats.
# -----------------------------------------------------------------------------
FROM ghcr.io/byronwilliamscpa/dhi-python@sha256:fe90645fdf287796e54df8b16360094e0001c607f7105ce3c15f026a6bcf2a3b AS builder

WORKDIR /build
COPY --from=ghcr.io/astral-sh/uv:0.8.17@sha256:e4644cb5bd56fdc2c5ea3ee0525d9d21eed1603bccd6a21f887a938be7e85be1 /uv /usr/local/bin/uv

COPY pyproject.toml uv.lock README.md ./
COPY src/ ./src/

# Locked runtime dependencies only (no dev extras), hash-checked, installed into
# a plain target directory: the distroless runtime has no venv activation and
# its interpreter path need not match the builder's.
RUN uv export --frozen --no-dev --no-emit-project --output-file /tmp/requirements.txt && \
    uv pip install --no-cache --require-hashes --target /opt/app -r /tmp/requirements.txt && \
    uv pip install --no-cache --no-deps --target /opt/app .

# -----------------------------------------------------------------------------
# Stage 2: runtime. Distroless, no shell, nonroot.
# Source: dhi.io/python 3.12 debian13, mirrored at
# ghcr.io/byronwilliamscpa/dhi-python. Digest-only pin.
# -----------------------------------------------------------------------------
FROM ghcr.io/byronwilliamscpa/dhi-python@sha256:a5425367554af74a6894e94ed3e98fee761973235307fc92438850163bf5f200

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
# packaged head at start. Downgrades are an explicit operator action
# (`migrate --downgrade --revision <rev>`).
CMD ["serve", "--upgrade-db"]
