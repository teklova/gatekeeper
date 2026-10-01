# Gatekeeper

[![License: commercial](https://img.shields.io/badge/license-commercial%20terms-lightgrey.svg)](#licensing)
[![Python: 3.10+](https://img.shields.io/badge/python-3.10%2B-3776AB.svg?logo=python&logoColor=white)](#prerequisites)
[![Build](https://github.com/teklova/gatekeeper/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/teklova/gatekeeper/actions/workflows/ci.yml)
[![Security: Gitleaks in CI](https://img.shields.io/badge/security-Gitleaks%20in%20CI-2E8B57.svg)](.github/workflows/ci.yml)

Gatekeeper is an edge-first vehicle access auditing platform that detects and reconciles vehicle movements while keeping records available through network interruptions. Its edge node securely checks in with Gatekeeper Cloud, where fleet telemetry, M-Pesa STK payment callbacks, and operator workflows can be managed together.

This repository contains the Gatekeeper Edge Node; the companion Cloud services are deployed separately. Cloud billing, payment callback processing, and the operator console require that companion deployment.

## Architecture

```text
Camera / RTSP stream
        |
        v
Edge Node: detection + tracking --> local SQLite audit store
        |                                  |
        | HTTPS check-in, signed telemetry, idempotent event sync
        +----------------------------------+----> Gatekeeper Cloud
                                                    |-- node and fleet telemetry
                                                    |-- M-Pesa STK callbacks and billing
                                                    |-- Material 3 operator console
                                                        dark/light modes; amber accents
```

The edge retains audit events locally until Cloud acknowledges them. It retries synchronization with backoff and moves into local standby when Cloud connectivity remains unavailable beyond the configured threshold. Reconnection restores Cloud contact and allows queued events to sync.

## Capabilities

- YOLO vehicle detection with ByteTrack tracking and configurable gate regions.
- Cloud-side hardware-token hash verification, HTTPS node check-in, signed requests, and operational telemetry.
- Local SQLite audit records, dwell-time tracking, anomaly summaries, and CSV/JSON daily reports.
- Idempotent audit-event synchronization with acknowledgment checks and retry/backoff behavior.
- Africa's Talking SMS alerts with duplicate suppression, retry handling, and a persistent dead-letter queue for critical alerts.
- Companion Cloud integration for automated M-Pesa STK callbacks, billing, and idempotency ledgers; payment processing is not implemented in this edge repository.
- Companion Material 3 operator console with dark/light modes and amber/obsidian accents (`#FF9F1C` / `#FFBF69`).
- Health and readiness endpoints for container orchestration.

## Prerequisites

- Python 3.11 or later is recommended for local development. The package metadata supports Python 3.10 and later.
- Docker Engine and Docker Compose v2 for container deployment.
- A provisioned Gatekeeper Cloud node token and a reachable RTSP camera stream for a live edge run.

## Local Development

```bash
git clone https://github.com/teklova/gatekeeper.git
cd gatekeeper
python -m venv .venv
```

Activate the environment and install the package plus development tools:

```powershell
# Windows PowerShell
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
```

```bash
# Linux / macOS
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
```

Copy `.env.example` to `.env`, set the provisioned `DEVICE_SECRET`, and configure the camera source and gate geometry in `config.json`. Then start the node and run the test suite:

```bash
python main.py
python -m unittest discover -s tests -v
```

The edge process requires a valid Cloud token before it starts. For enrollment and production hardening, see [DEPLOYMENT.md](DEPLOYMENT.md).

## Docker Deployment

Create `.env` from `.env.example`, set `DEVICE_SECRET`, and update the deployment-specific camera/configuration values. Then build and start the edge service:

```bash
docker-compose up --build -d
```

With Docker Compose v2, the equivalent command is `docker compose up --build -d`. The service stores its SQLite database under `./data` and exposes its health endpoint on `127.0.0.1:8080`. Production installation, systemd setup, token provisioning, backup, and update guidance are in [DEPLOYMENT.md](DEPLOYMENT.md).

## Configuration and Secrets

The edge node reads a local `.env` file and environment variables. Never commit real credentials or provisioned node tokens.

| Variable | Required | Purpose |
| --- | --- | --- |
| `DEVICE_SECRET` | Yes for Cloud check-in | Unique provisioned hardware token for this node. |
| `FLEET_URL` | No | Gatekeeper Cloud base URL; defaults to `https://gatekeeper.teklova.com`. Must use HTTPS. |
| `GATEKEEPER_DEVICE_ID` | Recommended | Stable node identity; `DEVICE_ID` is also accepted. |
| `DB_PATH` | No | SQLite database path; defaults to `./data/gatekeeper.db` locally and `/app/data/gatekeeper.db` in Docker. |
| `RTSP_STREAM_URL` | For live camera input | Camera stream URL; alternatively set `camera_source` in `config.json`. |
| `AFRICASTALKING_USERNAME` | If SMS is enabled | Africa's Talking account name; use `sandbox` for testing. |
| `AFRICASTALKING_API_KEY` | If SMS is enabled | Africa's Talking API key. Treat as a secret. |
| `AFRICASTALKING_RECIPIENT_PHONE` | If SMS is enabled | Alert destination in international format. |
| `SYNC_WARNING_THRESHOLD_SECONDS` | No | Time without Cloud contact before local standby; default `180`. |
| `CLOUD_CHECKIN_INTERVAL_SECONDS` | No | Cloud check-in refresh interval; default `60` seconds. |
| `HEALTH_CHECK_TOKEN` | No | Optional bearer token for non-local health probes. |

`ADMIN_API_KEY`, database URLs, and application encryption secrets are not Edge Node settings. Configure Cloud-side credentials in the separately deployed Cloud service using its own deployment documentation; do not invent or reuse edge credentials for those values.

## Operations

Check health and readiness locally:

```bash
curl http://127.0.0.1:8080/healthz
curl http://127.0.0.1:8080/readyz
```

Generate the daily CSV and JSON audit summaries:

```bash
python scripts/generate_report.py
```

The Cloud integration, subscription behavior, and node provisioning are described in [DEPLOYMENT.md](DEPLOYMENT.md). The repository CI workflow runs the unit tests and scans git history for secrets.

## Licensing

No `LICENSE` file or general open-source license is currently published in this repository. Access to the source does not by itself grant permission to use, modify, redistribute, or deploy it commercially. Buyers and commercial adopters should contact the repository owner, Teklova, to agree written licensing and support terms before use.
