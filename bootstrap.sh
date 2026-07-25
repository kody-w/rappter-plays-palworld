#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$ROOT/.venv"
OPENRAPPTER_REF="${OPENRAPPTER_REF:-444feeca3f5c05c9742646e1dfd35749d007f580}"
OPENRAPPTER_SOURCE="${OPENRAPPTER_SOURCE:-}"
SETUP_ONLY=0
LAUNCH_ARGS=()

fail() {
  printf 'error: %s\n' "$*" >&2
  exit 1
}

case "$(uname -s)" in
  Darwin | Linux) ;;
  *) fail "the control plane supports macOS and Linux; the game server itself runs on Windows" ;;
esac

while (($#)); do
  case "$1" in
    --openrappter-source)
      (($# >= 2)) || fail "--openrappter-source requires a checkout path"
      OPENRAPPTER_SOURCE="$2"
      shift 2
      ;;
    --setup-only)
      SETUP_ONLY=1
      shift
      ;;
    --)
      shift
      LAUNCH_ARGS+=("$@")
      break
      ;;
    *)
      LAUNCH_ARGS+=("$1")
      shift
      ;;
  esac
done

PYTHON=""
for candidate in python3.13 python3.12 python3.11 python3; do
  if command -v "$candidate" >/dev/null 2>&1 &&
    "$candidate" -c 'import sys; raise SystemExit(sys.version_info < (3, 11))'; then
    PYTHON="$(command -v "$candidate")"
    break
  fi
done
[[ -n "$PYTHON" ]] || fail "Python 3.11+ is required"
command -v git >/dev/null 2>&1 || fail "git is required"

if [[ ! -x "$VENV/bin/python" ]]; then
  "$PYTHON" -m venv "$VENV"
fi

"$VENV/bin/python" -m pip install --disable-pip-version-check --quiet \
  --upgrade "pip>=24,<27"

if [[ -n "$OPENRAPPTER_SOURCE" ]]; then
  OPENRAPPTER_SOURCE="$(cd "$OPENRAPPTER_SOURCE" && pwd)"
  if [[ -f "$OPENRAPPTER_SOURCE/python/pyproject.toml" ]]; then
    OPENRAPPTER_PACKAGE="$OPENRAPPTER_SOURCE/python"
  elif [[ -f "$OPENRAPPTER_SOURCE/pyproject.toml" ]]; then
    OPENRAPPTER_PACKAGE="$OPENRAPPTER_SOURCE"
  else
    fail "OpenRappter checkout has no Python pyproject.toml"
  fi
  "$VENV/bin/python" -m pip install --disable-pip-version-check --quiet \
    "$OPENRAPPTER_PACKAGE"
else
  "$VENV/bin/python" -m pip install --disable-pip-version-check --quiet \
    "git+https://github.com/kody-w/openrappter.git@${OPENRAPPTER_REF}#subdirectory=python"
fi

"$VENV/bin/python" -m pip install --disable-pip-version-check --quiet \
  -e "$ROOT[runtime]"
"$VENV/bin/python" -m rappter_plays_palworld.install_agent \
  --source "$ROOT/palworld_agent.py"
"$VENV/bin/python" -m copilot download-runtime
"$VENV/bin/python" -c \
  'from openrappter.agents.palworld_agent import PalworldAgent; assert PalworldAgent().name == "Palworld"'

if ((SETUP_ONLY)); then
  cat <<'EOF'
Setup complete.

Next:
  1. Provision the server on your Windows host:
       powershell -File server\provision-windows.ps1 -AdminPassword '<secret>'
  2. Point this machine at it:
       export PALWORLD_HOST=<lan-ip>
       export PALWORLD_ADMIN_PASSWORD='<secret>'
  3. Verify the connection:
       ./launch.sh doctor
  4. Bring the warden online:
       ./launch.sh start --foreground
EOF
  exit 0
fi

exec "$ROOT/launch.sh" "${LAUNCH_ARGS[@]}"
