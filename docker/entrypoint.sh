#!/bin/sh
set -e

# Select the process by env var, because some platforms (e.g. Railway with the
# Railpack builder) ignore a per-service custom start command for an image that
# defines its own CMD. Env vars are always honored, so HONCHO_ROLE picks the
# process: "deriver" runs the background worker; anything else (the default)
# runs migrations then the API server.
if [ "$HONCHO_ROLE" = "deriver" ]; then
    echo "Starting deriver worker..."
    exec /app/.venv/bin/python -m src.deriver
fi

echo "Running database migrations..."
/app/.venv/bin/python scripts/provision_db.py

echo "Starting API server..."
# FORK: bind IPv6 (dual-stack) so the app is reachable over Railway's private
# network, which is IPv6 (fd12::/16). A 0.0.0.0 (IPv4-only) bind is unreachable
# there, so upstream's host must not be taken. --workers IS upstream's and is
# kept: it defaults to 1, which is today's behavior.
exec /app/.venv/bin/fastapi run --host :: --workers "${API_WORKERS:-1}" src/main.py
