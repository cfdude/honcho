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
# Bind IPv6 (dual-stack) so the app is reachable over Railway's private network,
# which is IPv6 (fd12::/16). A 0.0.0.0 (IPv4-only) bind is unreachable there.
exec /app/.venv/bin/fastapi run --host :: src/main.py
