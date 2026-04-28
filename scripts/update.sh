#!/usr/bin/env bash
# Pull latest code and refresh Python deps. Run after `git pull` adds new
# modules or changes pyproject.toml. Idempotent.
#
# Usage:
#   bash scripts/update.sh                # git pull + reinstall (dev only)
#   EXTRAS=dev,alpaca bash scripts/update.sh    # also install extras

set -euo pipefail
cd "$(dirname "$0")/.."
REPO="$(pwd)"

EXTRAS="${EXTRAS:-dev}"

echo "═══ Bibi-Signal update ═══"
echo "Repo:    $REPO"
echo "Extras:  $EXTRAS"
echo

if [[ ! -d .venv ]]; then
    echo "✗ no .venv found. Run scripts/setup-rpi.sh first."
    exit 1
fi

echo "[1/3] git pull --ff-only"
git pull --ff-only

# shellcheck disable=SC1091
source .venv/bin/activate

echo "[2/3] pip install --upgrade -e \".[$EXTRAS]\""
pip install --upgrade --quiet -e ".[$EXTRAS]"

echo "[3/3] pytest -q"
python -m pytest -q || {
    echo "✗ tests failed after update — investigate before running live."
    exit 1
}

echo
echo "✓ update complete. Launch with:  bash scripts/run.sh"
