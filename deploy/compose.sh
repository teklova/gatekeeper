#!/usr/bin/env bash
set -euo pipefail

if docker compose version >/dev/null 2>&1; then
    exec docker compose "$@"
fi

if command -v docker-compose >/dev/null 2>&1; then
    exec docker-compose "$@"
fi

echo "Error: Docker Compose is not installed. Install the Docker Compose plugin or docker-compose v1." >&2
exit 127