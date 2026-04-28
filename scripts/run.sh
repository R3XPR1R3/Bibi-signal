#!/usr/bin/env bash
# Quick launcher: activate venv and start the TUI.
#
# Usage:
#   bash scripts/run.sh            # opens bibi-tui
#   bash scripts/run.sh paper      # forwards "--mode paper" to bibi-signal
#   bash scripts/run.sh backtest --ticker QQQ --years 5
#   bash scripts/run.sh optimize --ticker QQQ
#
# If .venv is missing, runs setup-rpi.sh first.

set -euo pipefail
cd "$(dirname "$0")/.."
REPO="$(pwd)"

if [[ ! -d .venv ]]; then
    echo "[run] no .venv found — running setup-rpi.sh first…"
    bash scripts/setup-rpi.sh
fi

# shellcheck disable=SC1091
source .venv/bin/activate

if [[ $# -eq 0 ]]; then
    exec bibi-tui
fi

case "$1" in
    backtest|optimize|paper|live)
        mode="$1"; shift
        exec bibi-signal --mode "$mode" "$@"
        ;;
    tui)
        exec bibi-tui
        ;;
    rh-login)
        exec bibi-rh-login
        ;;
    *)
        echo "Unknown command: $1"
        echo "Usage: bash scripts/run.sh [tui|backtest|optimize|paper|live|rh-login] [args...]"
        exit 2
        ;;
esac
