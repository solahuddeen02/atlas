# Atlas image (Phase 3b, minimal). Phase 4 makes it multi-stage and multi-arch.
# Every base image is pinned by digest. Runs as a non-root user (uid/gid 10001),
# which is what `atlas-admin init --chown` sets on the secrets it writes (ADR-004 §3).

FROM ghcr.io/astral-sh/uv:0.13.6@sha256:af22a52f5eafef3d78e551599d405c02df61919d18028f0cea8eda93bc9986d0 AS uv

# python:3.11-slim-bookworm
FROM python:3.11-slim-bookworm@sha256:94a8fee35252d06ab75856f15f12c7bae67b10e8a6da93c7fded82b2610926cd

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/atlas/venv

# PostgreSQL 17 client from PGDG, so pg_dump/pg_restore match the server (ADR-005).
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl \
    && install -d /usr/share/postgresql-common/pgdg \
    && curl -fsSL -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc \
        https://www.postgresql.org/media/keys/ACCC4CF8.asc \
    && echo "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] https://apt.postgresql.org/pub/repos/apt bookworm-pgdg main" \
        > /etc/apt/sources.list.d/pgdg.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends postgresql-client-17 \
    && apt-get purge -y curl \
    && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*

COPY --from=uv /uv /usr/local/bin/uv

WORKDIR /opt/atlas/src
# Dependencies first (cached), then the project itself, installed non-editable.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project
COPY atlas ./atlas
RUN uv sync --frozen --no-dev --no-editable \
    && rm -rf /opt/atlas/src /usr/local/bin/uv /root/.cache
# Leave the deleted source dir before the next RUN: BuildKit recreates a missing
# WORKDIR (empty) for every later step.
WORKDIR /opt/atlas

RUN groupadd --system --gid 10001 atlas \
    && useradd --system --uid 10001 --gid 10001 --home-dir /nonexistent \
        --shell /usr/sbin/nologin atlas

ENV PATH=/opt/atlas/venv/bin:$PATH
USER 10001:10001
EXPOSE 8000
CMD ["python", "-m", "atlas.kernel.web"]
