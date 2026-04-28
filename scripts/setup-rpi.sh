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
echo "[3/5] Installing Python dependencies (this can take 5-15 min on a Pi)…"
pip install -e ".[dev]"

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

Next steps:
  source .venv/bin/activate
  bibi-tui                 # interactive launcher (configure + run modes)

Or directly:
  bibi-signal --mode paper --once --no-market-hours    # smoke test
  bibi-signal --mode optimize --ticker QQQ --years 5   # find good params

To run the bot 24/7 on this Pi as a system service, see:
  scripts/bibi-signal.service.example
EOF
