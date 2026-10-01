#!/bin/sh
set -eu
cd "$(dirname "$0")"

PYTHON_BIN=${PYTHON:-}
if [ -z "$PYTHON_BIN" ]; then
    for candidate in python3.14 python3.13 python3.12 python3.11 python3.10 python3; do
        if command -v "$candidate" >/dev/null 2>&1 &&
            "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' >/dev/null 2>&1; then
            PYTHON_BIN=$candidate
            break
        fi
    done
fi

if [ -z "$PYTHON_BIN" ] || ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
    echo "Python 3.10 or later is required; set PYTHON to its executable path." >&2
    exit 1
fi

"$PYTHON_BIN" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' || {
    echo "Python 3.10 or later is required." >&2
    exit 1
}
"$PYTHON_BIN" -m venv --upgrade .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt
echo "Setup complete. Activate with: . .venv/bin/activate"