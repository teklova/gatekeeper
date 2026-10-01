"""CLI to test Africa's Talking SMS integration using gatekeeper config."""
from __future__ import annotations

import logging
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from gatekeeper.config_loader import get_config
from gatekeeper.notifier import SMSNotifier


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logger = logging.getLogger("test_sms")

    config = get_config()
    af = config.get("africastalking", {})
    sms_enabled = af.get("enabled", config.get("sms_enabled", False))

    username = af.get("username", "sandbox") or "sandbox"
    api_key = af.get("api_key", "")
    recipient = af.get("recipient_phone", "")
    sender_id = af.get("sender_id", "") or ("19191" if username == "sandbox" else "")

    if not sms_enabled:
        logger.error("SMS is not enabled (SMS_ENABLED/AFRICASTALKING_ENABLED not set). Aborting.")
        return 2

    if not (username and api_key and recipient):
        logger.error("Missing SMS credentials. Please set AFRICASTALKING_USERNAME, AFRICASTALKING_API_KEY, and AFRICASTALKING_RECIPIENT_PHONE in .env")
        return 2

    notifier = SMSNotifier(
        username=username,
        api_key=api_key,
        recipient=recipient,
        enabled=sms_enabled,
        sandbox_mode=(username == "sandbox"),
        config=config,
    )

    message = "gatekeeper Sandbox Test SMS"
    success, info = notifier.send_alert_with_response(message)

    if success:
        logger.info("Test SMS sent successfully: %s", info)
        return 0
    else:
        logger.error("Test SMS failed: %s", info)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())


