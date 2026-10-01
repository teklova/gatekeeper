#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."
python -m venv .venv >/dev/null 2>&1 || true
. .venv/Scripts/activate 2>/dev/null || . .venv/bin/activate 2>/dev/null || true
python scripts/generate_report.py
