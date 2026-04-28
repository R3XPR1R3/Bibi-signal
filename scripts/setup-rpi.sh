#!/usr/bin/env bash
# Raspberry Pi (or any Debian/Ubuntu) one-shot setup for Bibi-Signal.
#
# Usage:   bash scripts/setup-rpi.sh
# Idempotent: safe to re-run after a git pull.

set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_DIR"

echo "═══ Bibi-Signal Raspberry Pi setup ═══"
echo "Repo: $REPO_DIR"
echo

# ---------- 1. system deps ----------
if [[ "${SKIP_APT:-0}" != "1" ]] && command -v apt-get >/dev/null; then
    echo "[1/5] Installing system packages (apt)…"
    SUDO=""
    [[ "$EUID" -ne 0 ]] && SUDO="sudo"
    $SUDO apt-get update
    $SUDO apt-get install -y \
        python3 python3-venv python3-pip \
        build-essential libssl-dev libffi-dev \
        git tmux nano
else
    echo "[1/5] Skipping apt (set SKIP_APT=0 to enable; or run on a non-apt system)."
fi

# ---------- 2. venv ----------
if [[ ! -d .venv ]]; then
    echo "[2/5] Creating .venv…"
    python3 -m venv .venv
else
    echo "[2/5] .venv already exists, reusing."
fi
# shellcheck disable=SC1091
source .venv/bin/activate
python -m pip install --upgrade pip wheel setuptools

# ---------- 3. python deps ----------
# EXTRAS controls which optional dependency groups to install.
# Default: dev,alpaca (alpaca is recommended for real-time prices).
# Add 'robinhood' if you want unofficial Robinhood stocks via robin-stocks.
EXTRAS="${EXTRAS:-dev,alpaca}"
echo "[3/5] Installing Python dependencies with extras [$EXTRAS]"
echo "      (this can take 5-15 min on a Pi)…"
pip install -e ".[$EXTRAS]"

# ---------- 4. config + env scaffolding ----------
echo "[4/5] Setting up config files…"
mkdir -p data data/run logs

if [[ ! -f .env ]]; then
    cp .env.example .env
    echo "    created .env from .env.example — edit it via 'bibi-tui' menu [1] Configure"
else
    echo "    .env already exists, leaving it alone"
fi

# ---------- 5. quick smoke test ----------
echo "[5/5] Running tests to confirm install…"
python -m pytest -q || {
    echo
    echo "✗ tests failed; install may be incomplete."
    exit 1
}

cat <<EOF

✓ Setup complete.

Quick launchers:
  bash scripts/run.sh              # opens the TUI
  bash scripts/run.sh paper        # paper trading
  bash scripts/run.sh backtest --ticker QQQ --years 5
  bash scripts/run.sh optimize --ticker QQQ

After git pull, refresh deps with:
  bash scripts/update.sh

To run the bot 24/7 on this Pi as a system service, see:
  scripts/bibi-signal.service.example
EOF
