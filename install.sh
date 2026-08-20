#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd -P)"
PYTHON="${PYTHON:-python3}"

"$PYTHON" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' || {
    echo "install: Python 3.11 or newer required, got $($PYTHON --version)" >&2
    exit 1
}

"$PYTHON" -m venv "$ROOT/venv"
"$ROOT/venv/bin/pip" install --quiet --upgrade pip
"$ROOT/venv/bin/pip" install --quiet -e "$ROOT[dev]"

mkdir -p "$ROOT/log" "$ROOT/db"

if [ ! -f "$ROOT/etc/config.yaml" ]; then
    cp "$ROOT/etc/config.yaml.example" "$ROOT/etc/config.yaml"
    chmod 600 "$ROOT/etc/config.yaml"
    echo "install: seeded etc/config.yaml — edit it before running"
fi

echo "install: done. Next: $ROOT/bin/mailgonizer check"
