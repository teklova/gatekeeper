import sys
import csv
import json
import sqlite3
from collections import defaultdict
from contextlib import closing
from pathlib import Path

# Ensure repo root is on sys.path so `gatekeeper` imports work when run from anywhere
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gatekeeper.config_loader import get_config
from gatekeeper.db import init_db


def generate_daily_report(db_path="gatekeeper.db", today_only: bool = False):
    """Aggregate daily vehicle volume and average dwell time from the audit DB.

    Returns a dict keyed by ISO date, with:
    {
        "YYYY-MM-DD": {
            "total_vehicles": int,
            "avg_dwell_seconds": float,
            "by_vehicle": {
                "CAR": {"count": int, "avg_dwell_seconds": float},
                ...
            }
        }
    }
    """
    with closing(sqlite3.connect(db_path)) as conn:
        cursor = conn.cursor()
        if today_only:
            # Focus strictly on today's entries (UTC/local as SQLite configured)
            query = """
                SELECT
                    vehicle_type,
                    COUNT(*) AS total_vehicles,
                    ROUND(AVG(dwell_seconds), 2) AS avg_dwell_seconds
                FROM carwash_audit
                WHERE entry_time >= date('now', 'start of day')
                GROUP BY vehicle_type
                ORDER BY total_vehicles DESC
            """
            try:
                cursor.execute(query)
                rows = cursor.fetchall()
            except sqlite3.OperationalError as exc:
                raise RuntimeError(f"Database query failed: {exc}") from exc

            if not rows:
                return {}

            # Build a single-date summary keyed by today's ISO date
            today_key = cursor.execute("SELECT DATE('now')").fetchone()[0]
            daily_summary = {
                today_key: {"total_vehicles": 0, "avg_dwell_seconds": 0.0, "by_vehicle": {}}
            }
            total_seconds = 0.0
            total_records = 0
            for vehicle_type, count, avg_dwell in rows:
                vt = (vehicle_type or "UNKNOWN").upper()
                daily_summary[today_key]["by_vehicle"][vt] = {
                    "count": count,
                    "avg_dwell_seconds": float(avg_dwell or 0.0),
                }
                daily_summary[today_key]["total_vehicles"] += count
                total_seconds += float(avg_dwell or 0.0) * count
                total_records += count

            daily_summary[today_key]["avg_dwell_seconds"] = round(total_seconds / total_records, 2) if total_records else 0.0

            # Include estimated revenue using pricing from config
            config = get_config()
            pricing = config.get("pricing", config.get("pricing_tiers", {}))
            revenue = 0.0
            for vt, metrics in daily_summary[today_key]["by_vehicle"].items():
                price = pricing.get(vt, pricing.get("DEFAULT", 0))
                revenue += metrics["count"] * float(price)
            daily_summary[today_key]["estimated_revenue"] = round(revenue, 2)

            return daily_summary

        # legacy / full-date scan behavior
        query = """
            SELECT
                DATE(entry_time) AS audit_date,
                vehicle_type,
                COUNT(*) AS total_vehicles,
                ROUND(AVG(dwell_seconds), 2) AS avg_dwell_seconds
            FROM carwash_audit
            WHERE entry_time IS NOT NULL
            GROUP BY DATE(entry_time), vehicle_type
            ORDER BY audit_date DESC, total_vehicles DESC
        """

        try:
            cursor.execute(query)
            rows = cursor.fetchall()
        except sqlite3.OperationalError as exc:
            raise RuntimeError(f"Database query failed: {exc}") from exc

        if not rows:
            return {}

        daily_summary = defaultdict(
            lambda: {"total_vehicles": 0, "avg_dwell_seconds": 0.0, "by_vehicle": {}}
        )

        for date_str, vehicle_type, count, avg_dwell in rows:
            vehicle_type = (vehicle_type or "UNKNOWN").upper()
            day = daily_summary[date_str]
            day["total_vehicles"] += count
            day["by_vehicle"][vehicle_type] = {
                "count": count,
                "avg_dwell_seconds": float(avg_dwell or 0.0),
            }

        for data in daily_summary.values():
            total_seconds = 0.0
            total_records = 0

            for metrics in data["by_vehicle"].values():
                total_seconds += metrics["avg_dwell_seconds"] * metrics["count"]
                total_records += metrics["count"]

            data["avg_dwell_seconds"] = round(total_seconds / total_records, 2) if total_records else 0.0

        return dict(daily_summary)


def export_summary_report(summary, output_dir, format="json"):
    """Export the generated daily summary as JSON or CSV."""
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)

    if format.lower() == "json":
        file_path = output_path / "daily_summary.json"
        with file_path.open("w", encoding="utf-8") as fp:
            json.dump(summary, fp, indent=2, sort_keys=True)
        return str(file_path)

    if format.lower() == "csv":
        file_path = output_path / "daily_summary.csv"
        fieldnames = ["date", "vehicle_type", "count", "avg_dwell_seconds"]
        with file_path.open("w", newline="", encoding="utf-8") as fp:
            writer = csv.DictWriter(fp, fieldnames=fieldnames)
            writer.writeheader()
            for date_str, data in sorted(summary.items(), reverse=True):
                for vehicle_type, metrics in sorted(data["by_vehicle"].items()):
                    writer.writerow(
                        {
                            "date": date_str,
                            "vehicle_type": vehicle_type,
                            "count": metrics["count"],
                            "avg_dwell_seconds": metrics["avg_dwell_seconds"],
                        }
                    )
        return str(file_path)

    raise ValueError("format must be 'json' or 'csv'")


def run_daily_report(output_dir="reports", db_path="gatekeeper.db"):
    # CLI-focused run should show today's metrics and revenue
    summary = generate_daily_report(db_path, today_only=True)
    json_path = export_summary_report(summary, output_dir, format="json")
    csv_path = export_summary_report(summary, output_dir, format="csv")
    return {"json": json_path, "csv": csv_path, "summary": summary}


if __name__ == "__main__":
    # Produce a clean minimal ASCII summary for clients, and write sanitized JSON for developers
    # Ensure DB exists and migrations applied before running report
    init_db()
    report = generate_daily_report(today_only=True)
    if not report:
        print("No audit records found for today.")
        raise SystemExit(0)

    # we'll print a compact ASCII block for today's summary
    for date_str, data in sorted(report.items(), reverse=True):
        header = f" gatekeeper Summary â€” {date_str} "
        sep = "=" * len(header)
        print(sep)
        print(header)
        print(sep)
        print(f"Total vehicles: {data.get('total_vehicles', 0)}")
        print(f"Average dwell (s): {data.get('avg_dwell_seconds', 0.0):.2f}")
        if data.get("estimated_revenue") is not None:
            print(f"Estimated revenue (KES): {data.get('estimated_revenue')}")
        print("Vehicle breakdown:")
        for vehicle_type, metrics in sorted(data["by_vehicle"].items()):
            print(f"  - {vehicle_type}: count={metrics['count']}, avg_dwell_s={metrics['avg_dwell_seconds']:.2f}")
        print(sep)

    # write sanitized JSON to reports dir for developers
    out = Path("reports")
    out.mkdir(parents=True, exist_ok=True)
    sanitized_path = out / "daily_summary.json"
    with sanitized_path.open("w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2, sort_keys=True)

    print(f"\nWrote sanitized JSON to: {sanitized_path}")

