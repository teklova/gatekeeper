# Gatekeeper Edge Deployment

## Local Development Environment

From the repository root, run `setup.bat` on Windows or `sh ./setup.sh` on Linux/macOS. The script creates or upgrades `.venv`, updates pip, and installs the edge requirements. Activate with `.\.venv\Scripts\Activate.ps1` in PowerShell, `.venv\Scripts\activate.bat` in Command Prompt, or `. .venv/bin/activate` on Linux/macOS. The edge setup requires Python 3.10 or later.

## HP EliteDesk 800 G4 Mini

Use a supported 64-bit Ubuntu LTS installation. In firmware, enable power-on after AC loss, keep Secure Boot enabled where the installed OS and camera drivers support it, and set a BIOS administrator password. Give the node a reserved LAN address and verify the camera stream, DNS, NTP, and outbound HTTPS before installation.

## Secure Bootstrap

Provision the node in Gatekeeper Cloud and save the one-time JSON response (`node_id` and `token`) to a protected file readable only by the installer. From the edge repository root, run:

```bash
python scripts/bootstrap.py --bootstrap-file /secure/path/node-bootstrap.json
```

The script verifies the hardware token against the HTTPS `/api/v1/nodes/check-in` endpoint before changing `.env`, preserves unrelated settings, writes atomically, and restricts file permissions. It never stores the temporary JWT. For managed provisioning, set `DEVICE_SECRET` and `FLEET_URL` in the process environment instead; do not put the token directly in command-line arguments or shell history. A JSON bootstrap file can also include `cloud_url`; otherwise the default is `https://gatekeeper.teklova.com`.

The default cloud check-in refresh is once per 60 seconds so Cloud monitoring receives current check-ins. `CLOUD_CHECKIN_INTERVAL_SECONDS` may increase that interval; values below 60 are raised to the Cloud rate-limit-safe minimum. If Cloud contact is lost for `SYNC_WARNING_THRESHOLD_SECONDS` (default 180), the edge logs a warning and enters local standby. Successful cloud contact clears the offline state and resumes tracking according to the subscription state.

Install Docker Engine and Compose, then place the release under `/opt/gatekeeper`:

```bash
sudo install -d -m 0750 /opt/gatekeeper/data
sudo cp -R . /opt/gatekeeper/
cd /opt/gatekeeper
sudo cp .env.example .env
sudo chmod 0600 .env
```

Provision the node in Gatekeeper Cloud from a trusted operator session. The one-time hardware token is returned only at provisioning. Put that token in `DEVICE_SECRET` in `/opt/gatekeeper/.env`; do not reuse it on another node, commit it, or include it in support bundles. Set `GATEKEEPER_DEVICE_ID`, `FLEET_URL=https://gatekeeper.teklova.com`, and `DB_PATH=/app/data/gatekeeper.db`. Configure camera and site values in `config.json`. Replace every example value before deployment.

The Compose service requires a non-empty `DEVICE_SECRET`, persists SQLite in `/opt/gatekeeper/data`, and makes the health endpoint available on port 8080. Start and register the systemd unit:

```bash
cd /opt/gatekeeper
sudo ./deploy/compose.sh up --build -d
sudo install -m 0644 deploy/gatekeeper.service /etc/systemd/system/gatekeeper.service
sudo systemctl daemon-reload
sudo systemctl enable --now gatekeeper.service
sudo systemctl status gatekeeper.service
sudo journalctl -u gatekeeper.service -f
```

The unit starts the Compose stack after network-online and Docker, restarts it on failure, and directs logs to journald. Keep the Docker socket restricted to administrators. The service currently runs the Docker client as root; do not grant an untrusted account access to the Docker group. Back up `data/gatekeeper.db` with SQLite's online backup API or while the service is stopped, and test restores regularly.

## Secrets And Updates

Keep `.env` mode `0600`, limit SSH access, and deliver hardware tokens over an authenticated channel. Rotate by provisioning a replacement token in Cloud and replacing the local value during a maintenance window. Avoid `set -x`, shell history, and commands that print the token. Deploy a reviewed release, then verify:

```bash
sudo systemctl is-active gatekeeper.service
curl --fail http://127.0.0.1:8080/healthz
curl --fail http://127.0.0.1:8080/readyz
```

Check journal logs for successful check-in and sync, and confirm the cloud node's last check-in before closing the maintenance window. The edge queue retains records until the Cloud logs API acknowledges them; monitor disk free space and queue backlog during extended outages.

## Cloud Service

See the cloud repository's `DEPLOYMENT.md` for PostgreSQL, Redis, Alembic, Nginx/Certbot, M-Pesa Daraja, and Africa's Talking setup.