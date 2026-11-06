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

# Optional extras, ported from upstream (PR #1522 added INSTALL_SURPRISAL alongside the
# existing INSTALL_LANCEDB):
#   docker build --build-arg INSTALL_LANCEDB=true --build-arg INSTALL_SURPRISAL=true .
ARG INSTALL_LANCEDB=false
ARG INSTALL_SURPRISAL=false

# Dependencies only. The project itself is installed in the second sync below, after
# its source is present -- installing it before the source exists produces a venv entry
# that points at an empty tree.
#
# `uv sync` is EXACT by default, so the second sync would uninstall whatever extras this
# one added unless it is given the same "--extra" arguments. The resolved argument list
# is written to /app/.uv-extras here and replayed below rather than duplicating the two
# case blocks, which would drift.
RUN set --; \
    case "$INSTALL_LANCEDB" in \
        true) set -- "$@" --extra lancedb ;; \
        false) ;; \
        *) echo "INSTALL_LANCEDB must be 'true' or 'false'" >&2; exit 2 ;; \
    esac; \
    case "$INSTALL_SURPRISAL" in \
        true) set -- "$@" --extra surprisal ;; \
        false) ;; \
        *) echo "INSTALL_SURPRISAL must be 'true' or 'false'" >&2; exit 2 ;; \
    esac; \
    printf '%s ' "$@" > /app/.uv-extras; \
    uv sync --frozen --no-install-project --no-group dev "$@" \
    && rm -rf /tmp/uv-cache

# The project's own source, needed for the second sync
COPY src/ /app/src/
COPY migrations/ /app/migrations/
COPY alembic.ini /app/alembic.ini

RUN set -- $(cat /app/.uv-extras); \
    uv sync --frozen --no-group dev "$@" \
    && rm -rf /tmp/uv-cache

FROM python:3.13-slim-bookworm AS runtime

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# Create the runtime user BEFORE copying the virtualenv, so --chown lands it with its
# final owner. A recursive chown in a later layer would copy the whole venv and nearly
# double the image size -- the exact trap this change exists to avoid.
# The `mkdir /app/lancedb_data` pre-creates the LanceDB dir so a named volume mounted
# there inherits app ownership instead of defaulting to root (upstream parity). The
# comment sits ABOVE the RUN rather than inside its line continuation, as upstream has
# it: a comment inside a continuation relies on the builder stripping it, and this
# fork must stay buildable with DOCKER_BUILDKIT=0 and under Railway's Railpack.
RUN addgroup --system app \
    && adduser --system --group app \
    && chown app:app /app \
    && mkdir /app/lancedb_data \
    && chown app:app /app/lancedb_data

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
