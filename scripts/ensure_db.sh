#!/usr/bin/env bash
# Apply ACCOUNTS migrations (creates tables on empty MySQL). Idempotent.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
exec python manage.py ensure_db "$@"
