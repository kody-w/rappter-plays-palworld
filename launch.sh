#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$ROOT/.venv"

if [[ ! -x "$VENV/bin/python" ]]; then
  printf 'error: run ./bootstrap.sh --setup-only first\n' >&2
  exit 1
fi

exec "$VENV/bin/rappter-plays-palworld" "$@"
