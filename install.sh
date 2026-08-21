#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd -P)"
PYTHON="${PYTHON:-python3}"

"$PYTHON" -c 'import sys; sys.exit(0 if (3, 11) <= sys.version_info < (3, 14) else 1)' || {
    echo "install: Python 3.11, 3.12, or 3.13 required, got $($PYTHON --version)." \
         "IMAPClient 3.1.0 (the only release on PyPI) cannot open a real IMAP" \
         "connection under 3.14: Python 3.14's imaplib made IMAP4.file a" \
         "read-only property, and imapclient's IMAP4WithTimeout.open() still" \
         "assigns self.file directly, so every connection fails with" \
         "AttributeError before login is ever attempted." >&2
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
