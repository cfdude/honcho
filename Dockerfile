# https://pythonspeed.com/articles/base-image-python-docker-images/
# https://testdriven.io/blog/docker-best-practices/
#
# MULTI-STAGE, and deliberately WITHOUT BuildKit.
# Upstream's Dockerfile uses `--mount=type=cache` and a `# syntax=` directive, both of
# which need BuildKit. Railway's Railpack builder rejects type=bind and unscoped
# type=cache mounts, so this fork keeps plain RUN layers and must stay buildable with
# DOCKER_BUILDKIT=0. The multi-stage split is what shrinks the image; the cache mount
# only ever made rebuilds faster.
FROM python:3.13-slim-bookworm AS builder

COPY --from=ghcr.io/astral-sh/uv:0.13.5 /uv /bin/uv

WORKDIR /app

# Enable bytecode compilation
ENV UV_COMPILE_BYTECODE=1

# Copy from the cache instead of linking since it's a mounted volume
ENV UV_LINK_MODE=copy

# Python optimizations
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# No BuildKit cache mount is available, so uv's cache would otherwise land in the
# image layer it is written in. Point it at /tmp and delete it in the same RUN.
ENV UV_CACHE_DIR=/tmp/uv-cache

# Copy the lockfile + project metadata first so dependency resolution caches as a layer
COPY uv.lock pyproject.toml /app/

# Dependencies only. The project itself is installed in the second sync below, after
# its source is present -- installing it before the source exists produces a venv entry
# that points at an empty tree.
RUN uv sync --frozen --no-install-project --no-group dev && rm -rf /tmp/uv-cache

# The project's own source, needed for the second sync
COPY src/ /app/src/
COPY migrations/ /app/migrations/
COPY alembic.ini /app/alembic.ini

RUN uv sync --frozen --no-group dev && rm -rf /tmp/uv-cache

FROM python:3.13-slim-bookworm AS runtime

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# Create the runtime user BEFORE copying the virtualenv, so --chown lands it with its
# final owner. A recursive chown in a later layer would copy the whole venv and nearly
# double the image size -- the exact trap this change exists to avoid.
RUN addgroup --system app \
    && adduser --system --group app \
    && chown app:app /app

COPY --from=builder --chown=app:app /app/.venv /app/.venv

# Place executables in the environment at the front of the path
ENV PATH="/app/.venv/bin:$PATH"
ENV HOME=/app

COPY --chown=app:app src/ /app/src/
COPY --chown=app:app migrations/ /app/migrations/
COPY --chown=app:app scripts/ /app/scripts/
COPY --chown=app:app docker/ /app/docker/
COPY --chown=app:app alembic.ini /app/alembic.ini
# src/_version.py reads the service version from here at runtime, so this
# is a runtime input as well as a build input.
COPY --chown=app:app pyproject.toml /app/pyproject.toml
# Copy config files - this will copy config.toml if it exists, and config.toml.example
COPY --chown=app:app config.toml* /app/

# Switch to non-root user
USER app

EXPOSE 8000

# NOT upstream's `fastapi run`. docker/entrypoint.sh branches on HONCHO_ROLE to start
# either the API (which runs the migrations first) or the deriver, which is how one
# image serves both Railway services.
CMD ["sh", "docker/entrypoint.sh"]
