"""Provision an edge node from a cloud hardware-token response."""

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import requests

DEFAULT_CLOUD_URL = "https://gatekeeper.teklova.com"
DEFAULT_ENV_FILE = Path(__file__).resolve().parents[1] / ".env"
MANAGED_ENV_KEYS = ("DEVICE_SECRET", "FLEET_URL", "GATEKEEPER_DEVICE_ID", "DEVICE_ID")
_ENV_KEY_PATTERN = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=")


def _read_bootstrap_file(path: str | Path | None) -> dict[str, Any]:
    if path is None:
        return {}
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("Unable to read the bootstrap JSON file") from exc
    if not isinstance(payload, dict):
        raise TypeError("Bootstrap file must contain a JSON object")
    nested = payload.get("bootstrap")
    return nested if isinstance(nested, dict) else payload


def _valid_cloud_url(value: str) -> str:
    url = value.strip().rstrip("/")
    parsed = urlsplit(url)
    if (
        parsed.scheme.lower() != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("FLEET_URL must be a valid HTTPS origin or base path")
    return url


def _check_in(hardware_token: str, fleet_url: str, post: Any) -> None:
    response = post(
        f"{fleet_url}/api/v1/nodes/check-in",
        headers={"X-Hardware-Token": hardware_token},
        timeout=10,
        allow_redirects=False,
    )
    response.raise_for_status()
    if not 200 <= response.status_code < 300:
        raise requests.HTTPError(
            f"Cloud check-in returned unexpected status {response.status_code}"
        )
    try:
        payload = response.json()
        access_token = payload["access_token"]
        expires_at = datetime.fromisoformat(
            str(payload["expires_at"]).replace("Z", "+00:00")
        )
        subscription_state = payload["subscription_state"]
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("Cloud check-in response is incomplete") from exc
    if not isinstance(access_token, str) or not access_token:
        raise ValueError("Cloud check-in did not return an access token")
    if expires_at.tzinfo is None or expires_at <= datetime.now(UTC):
        raise ValueError("Cloud check-in returned an expired or timezone-naive token")
    if subscription_state not in {"active", "grace_period"}:
        raise PermissionError("Cloud subscription is not authorized for this node")


def _restrict_windows_permissions(path: Path) -> None:
    identity = subprocess.run(
        ["whoami"], check=True, capture_output=True, text=True
    ).stdout.strip()
    subprocess.run(
        [
            "icacls",
            str(path),
            "/inheritance:r",
            "/grant:r",
            f"{identity}:(F)",
            "*S-1-5-18:(F)",
            "*S-1-5-32-544:(F)",
        ],
        check=True,
        capture_output=True,
        text=True,
    )


def _write_env_securely(env_file: str | Path, values: dict[str, str]) -> None:
    target = Path(env_file)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_symlink() or (target.exists() and not target.is_file()):
        raise ValueError("Refusing to replace a non-regular .env path")

    original = target.read_text(encoding="utf-8") if target.exists() else ""
    output: list[str] = []
    written: set[str] = set()
    for line in original.splitlines():
        match = _ENV_KEY_PATTERN.match(line)
        key = match.group(1) if match else None
        if key not in values:
            output.append(line)
        elif key not in written:
            output.append(f"{key}={json.dumps(values[key])}")
            written.add(key)

    for key, value in values.items():
        if key not in written:
            output.append(f"{key}={json.dumps(value)}")

    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=target.parent
    )
    temporary_path = Path(temporary_name)
    try:
        if os.name != "nt":
            os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write("\n".join(output) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        if os.name == "nt":
            _restrict_windows_permissions(temporary_path)
        os.replace(temporary_path, target)
        if os.name != "nt":
            os.chmod(target, 0o600)
    except Exception:
        temporary_path.unlink(missing_ok=True)
        raise


def bootstrap_node(
    *,
    bootstrap_file: str | Path | None = None,
    env_file: str | Path = DEFAULT_ENV_FILE,
    device_secret: str | None = None,
    fleet_url: str | None = None,
    post: Any | None = None,
) -> dict[str, str]:
    """Validate a provisioned token with Cloud before storing it locally."""
    payload = _read_bootstrap_file(bootstrap_file)
    token = str(
        device_secret
        or os.environ.get("DEVICE_SECRET")
        or os.environ.get("GATEKEEPER_HARDWARE_TOKEN")
        or payload.get("token")
        or payload.get("hardware_token")
        or payload.get("device_secret")
        or ""
    ).strip()
    if not token or "\n" in token or "\r" in token:
        raise ValueError("Provide the provisioned token using DEVICE_SECRET or a bootstrap file")

    url = _valid_cloud_url(
        fleet_url
        or os.environ.get("FLEET_URL")
        or os.environ.get("FLEET_MANAGEMENT_URL")
        or payload.get("fleet_url")
        or payload.get("cloud_url")
        or DEFAULT_CLOUD_URL
    )
    node_id = str(
        payload.get("node_id")
        or os.environ.get("GATEKEEPER_DEVICE_ID")
        or os.environ.get("DEVICE_ID")
        or ""
    ).strip()
    if "\n" in node_id or "\r" in node_id:
        raise ValueError("Bootstrap node_id contains an invalid newline")

    _check_in(token, url, post or requests.post)
    values = {"DEVICE_SECRET": token, "FLEET_URL": url}
    if node_id:
        values.update(GATEKEEPER_DEVICE_ID=node_id, DEVICE_ID=node_id)
    _write_env_securely(env_file, values)
    return {"fleet_url": url, "node_id": node_id}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Safely bootstrap a Gatekeeper edge node")
    parser.add_argument("--bootstrap-file", help="Cloud provisioning response JSON file")
    parser.add_argument("--env-file", default=str(DEFAULT_ENV_FILE))
    parser.add_argument("--cloud-url", help="Override FLEET_URL; HTTPS is required")
    args = parser.parse_args(argv)

    try:
        result = bootstrap_node(
            bootstrap_file=args.bootstrap_file,
            env_file=args.env_file,
            fleet_url=args.cloud_url,
        )
    except (OSError, PermissionError, TypeError, ValueError, requests.RequestException) as exc:
        print(f"Bootstrap failed: {exc}", file=sys.stderr)
        return 1

    node = f" for node {result['node_id']}" if result["node_id"] else ""
    print(f"Gatekeeper Cloud handshake verified{node}; secure .env updated.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())