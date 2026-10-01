# Gatekeeper Edge Appliance - Field Deployment Guide

This guide details the setup process for provisioning Gatekeeper on edge Mini PCs (e.g., GMKtec NucBox G3 or equivalent Ubuntu/Debian x86_64 nodes).

---

## 🚀 Quick Start Provisioning

1. Clone or copy the repository onto the edge node:

   ```bash
   git clone https://github.com/your-org/gatekeeper.git /tmp/gatekeeper
   cd /tmp/gatekeeper
   ```

2. Execute the automated provisioning installer as root:

   ```bash
   chmod +x deploy/install.sh
   sudo ./deploy/install.sh
   ```

3. Configure device parameters by editing `/opt/gatekeeper/.env` with local site values:

   ```env
   GATEKEEPER_DEVICE_ID=site-gate-01
   FLEET_MANAGEMENT_URL=https://fleet.tektally.com
   ```

4. Restart the service to apply parameters:

   ```bash
   sudo systemctl restart gatekeeper.service
   ```

---

## 🩺 System Inspection & Operations

### Check Service Health

```bash
sudo systemctl status gatekeeper.service
```

### Tail Live Application Logs

```bash
sudo journalctl -u gatekeeper.service -f
```

### Probe Application Health Endpoints

```bash
# Liveness check
curl -i http://localhost:8080/healthz

# Readiness check (storage, frame heartbeat, alert queue)
curl -i http://localhost:8080/readyz
```

---

## OTA Rollback Verification

Use the installer to validate the health-gated rollout logic and automatic rollback path.

1. Create a rollback candidate by confirming a healthy baseline:

   ```bash
   curl -f http://localhost:8080/readyz
   docker images | grep gatekeeper
   ```

2. Trigger an update rollout:

   ```bash
   sudo ./deploy/install.sh
   ```

3. Verify health-gated success path:

   ```bash
   curl -i http://localhost:8080/readyz
   sudo journalctl -u gatekeeper.service -n 100
   ```

4. Verify rollback behavior by forcing `/readyz` failure in a test build (for example, a temporary bad config or intentionally failing startup command), then rerun installer:

   ```bash
   sudo ./deploy/install.sh
   ```

5. Confirm rollback markers in logs:

   ```bash
   sudo journalctl -u gatekeeper.service -n 200 | grep -E "Deployment unhealthy|Rolling back|Rollback completed"
   docker image inspect gatekeeper:backup >/dev/null && echo "backup image exists"
   ```

## Systemd Sandboxing Verification

After installing the updated unit file, verify the service sandbox directives are applied:

```bash
sudo systemctl daemon-reload
sudo systemctl restart gatekeeper.service
sudo systemctl show gatekeeper.service \
  -p ProtectSystem \
  -p PrivateTmp \
  -p NoNewPrivileges \
  -p CapabilityBoundingSet
```

Expected highlights:

- `ProtectSystem=full`
- `PrivateTmp=yes`
- `NoNewPrivileges=yes`
- `CapabilityBoundingSet` includes `CAP_NET_BIND_SERVICE`

### Manually Prune Audit Storage (if needed)

```bash
./deploy/compose.sh exec gatekeeper python scripts/prune_storage.py
```

---

## 🎉 Project Milestones Complete

Across all three sprints, the Gatekeeper edge platform has achieved full production readiness:

- Sprint 1 (Runtime & Storage Hygiene):
  - Async non-blocking notification queue
  - Atomic frame heartbeats & liveness/readiness probes (`/healthz`, `/readyz`)
  - Storage pruning against `gatekeeper.db` with commit-level `VACUUM` handling
- Sprint 2 (Fleet Management & Remote Config):
  - System telemetry and heartbeat generation
  - Dynamic, thread-safe config hot-reloading (`VehicleTracker.apply_config_update`)
  - `GatekeeperRunner` lifecycle management and signal handling
- Sprint 3 (Packaging & Deployment):
  - Multi-stage Docker build & log rotation
  - `systemd` edge service and automated provisioning
  - GitHub Actions CI/CD workflow
