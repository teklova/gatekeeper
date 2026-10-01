#!/usr/bin/env bash
set -euo pipefail

INSTALL_DIR="/opt/gatekeeper"
SERVICE_NAME="gatekeeper.service"
READYZ_URL="http://localhost:8080/readyz"
READY_TIMEOUT_SECONDS=60
COMPOSE_CMD=""

echo "========================================="
echo " Gatekeeper Edge Node Provisioning Tool "
echo "========================================="

log() {
    echo "[$(date +"%Y-%m-%d %H:%M:%S")] $*"
}

backup_current_image() {
    local image_id=""
    if docker container inspect gatekeeper-edge >/dev/null 2>&1; then
        image_id="$(docker inspect --format='{{.Image}}' gatekeeper-edge 2>/dev/null || true)"
    fi

    if [ -z "$image_id" ]; then
        image_id="$(docker image inspect gatekeeper:latest --format='{{.Id}}' 2>/dev/null || true)"
    fi

    if [ -n "$image_id" ]; then
        docker image tag "$image_id" gatekeeper:backup
        log "Tagged current image as gatekeeper:backup"
    else
        log "No running/current gatekeeper image found; skipping backup tag"
    fi
}

build_or_pull_new_image() {
    log "Pulling latest image metadata (if available)..."
    "$COMPOSE_CMD" pull --ignore-build-errors gatekeeper || true

    log "Building/updating gatekeeper image..."
    "$COMPOSE_CMD" build --pull gatekeeper
}

wait_for_readyz() {
    local elapsed=0
    while [ "$elapsed" -lt "$READY_TIMEOUT_SECONDS" ]; do
        if curl -fsS "$READYZ_URL" >/dev/null 2>&1; then
            return 0
        fi
        sleep 1
        elapsed=$((elapsed + 1))
    done
    return 1
}

rollback_to_backup() {
    log "Rolling back to gatekeeper:backup..."
    if ! docker image inspect gatekeeper:backup >/dev/null 2>&1; then
        log "Rollback failed: gatekeeper:backup not found"
        return 1
    fi

    "$COMPOSE_CMD" down || true
    docker image tag gatekeeper:backup gatekeeper:latest
    systemctl restart "$SERVICE_NAME"

    if wait_for_readyz; then
        log "Rollback completed successfully; /readyz is healthy"
        return 0
    fi

    log "Rollback attempted but /readyz is still unhealthy"
    return 1
}

# Ensure running as root
if [ "$EUID" -ne 0 ]; then
  echo "Error: Please run as root (e.g., sudo ./install.sh)"
  exit 1
fi

# 1. Install Docker & Compose if missing
if ! command -v docker >/dev/null 2>&1; then
    log "[1/4] Docker not found. Installing Docker Engine..."
    curl -fsSL https://get.docker.com | sh
    systemctl enable --now docker
else
    log "[1/4] Docker is already installed."
fi

# 2. Setup Application Directory
log "[2/4] Setting up installation directory at $INSTALL_DIR..."
mkdir -p "$INSTALL_DIR/data"

# Copy deployment assets into /opt/gatekeeper
cp -r . "$INSTALL_DIR/"

COMPOSE_CMD="$INSTALL_DIR/deploy/compose.sh"
chmod +x "$COMPOSE_CMD"

# Populate default .env if absent
if [ ! -f "$INSTALL_DIR/.env" ]; then
        log "Creating default .env from .env.example..."
    cp "$INSTALL_DIR/.env.example" "$INSTALL_DIR/.env"
fi

# 3. Register Systemd Service
log "[3/4] Installing systemd service..."
cp "$INSTALL_DIR/deploy/$SERVICE_NAME" "/etc/systemd/system/$SERVICE_NAME"
systemctl daemon-reload
systemctl enable "$SERVICE_NAME"

# 4. Health-Gated Update and Restart
log "[4/4] Executing health-gated update rollout..."
cd "$INSTALL_DIR"

backup_current_image
build_or_pull_new_image

log "Restarting Gatekeeper service with new image..."
systemctl restart "$SERVICE_NAME"

if wait_for_readyz; then
    log "Deployment healthy: /readyz succeeded within ${READY_TIMEOUT_SECONDS}s"
else
    log "Deployment unhealthy: /readyz failed within ${READY_TIMEOUT_SECONDS}s"
    "$COMPOSE_CMD" logs --tail=100 gatekeeper || true
    if ! rollback_to_backup; then
        log "ALERT: rollback did not restore health. Inspect logs immediately: journalctl -u $SERVICE_NAME -n 200"
        exit 1
    fi
fi

echo "========================================="
echo " Provisioning complete!"
echo " Check status with: systemctl status $SERVICE_NAME"
echo " View logs with:    journalctl -u $SERVICE_NAME -f"
echo "========================================="
