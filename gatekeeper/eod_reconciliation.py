import json
from datetime import datetime

from gatekeeper.db import get_daily_summary
from gatekeeper.notifier import SMSNotifier


def run_eod_reconciliation():
    with open("config.json", "r", encoding="utf-8") as handle:
        config = json.load(handle)

    today_str = datetime.now().strftime("%Y-%m-%d")
    summary = get_daily_summary(today_str)

    total_washed = 0
    est_revenue = 0.0
    total_anomalies = 0
    pricing = config["pricing_tiers"]
    max_dwell_seconds = config.get("max_dwell_threshold_seconds", 14400)

    for v_type, count, avg_dwell, anomalies in summary:
        total_washed += count
        total_anomalies += anomalies
        rate = pricing.get(v_type, pricing.get("DEFAULT", 500.0))
        est_revenue += count * rate

    msg = (
        f"[Gatekeeper]\n"
        f"EOD Summary ({today_str})\n"
        f"Total Vehicles: {total_washed}\n"
        f"Estimated Revenue: KES {est_revenue:,.0f}\n"
        f"Alerts: {total_anomalies}\n"
        f"Max dwell threshold: {max_dwell_seconds // 3600}h"
    )

    at_conf = config.get("africastalking", {}) or {}
    notifier = SMSNotifier(
        username=at_conf.get("username", "sandbox"),
        api_key=at_conf.get("api_key", ""),
        recipient=at_conf.get("recipient_phone", ""),
        enabled=bool(config.get("sms_enabled", True)),
        sandbox_mode=bool(config.get("sandbox_mode", at_conf.get("sandbox_mode", True))),
        sandbox_api_key=str(config.get("sandbox_api_key", at_conf.get("sandbox_api_key", ""))).strip(),
        config=config,
    )
    notifier.send_alert(msg)


if __name__ == "__main__":
    run_eod_reconciliation()

