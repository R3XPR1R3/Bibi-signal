# Bibi-Signal — Ladder Swing Bot

Signal-only trading assistant for **QQQ** and **XLE** on Robinhood.

The bot does **not** place orders. It analyses prices, applies a ladder swing
strategy, and sends BUY / SELL / HOLD signals to Telegram. You confirm each
trade manually in the Robinhood app.

## What it does

- Polls market data every 5–15 minutes (configurable).
- Tracks each open lot in SQLite with its individual buy price and target.
- Sends a BUY signal when price drops `dip_percent` from the last buy,
  with optional confirmations: 200-SMA uptrend filter, RSI < 35.
- Sends a SELL signal when an individual lot gains `profit_percent`.
- Sends a STOP-LOSS signal when a lot drops `stop_loss_percent` below entry.
- Tracks free cash, realised P&L, win rate.
- Backtests the same strategy on historical data so you can see what it
  would have done before risking real money.

## Install (Raspberry Pi or any Debian/Ubuntu)

One-shot setup script — installs system packages, creates a venv, installs
Python deps (dev + alpaca by default), and runs the test suite:

```bash
git clone <this-repo>
cd Bibi-signal
bash scripts/setup-rpi.sh
```

After that, just one command to launch:

```bash
bash scripts/run.sh                                    # interactive TUI
bash scripts/run.sh backtest --ticker QQQ --years 5    # historical replay
bash scripts/run.sh optimize --ticker QQQ              # parameter sweep
bash scripts/run.sh paper                              # paper trading
bash scripts/run.sh live                               # live signals
```

The launcher activates the venv automatically. If `.venv` is missing, it
runs `setup-rpi.sh` first. To pull latest code and refresh deps:

```bash
bash scripts/update.sh                                 # default: dev only
EXTRAS=dev,alpaca,robinhood bash scripts/update.sh     # also extras
```

`scripts/setup-rpi.sh` and `scripts/update.sh` honor `EXTRAS=...` to pick
which optional dependency groups install (defaults to `dev,alpaca`).
Available extras: `dev`, `alpaca`, `robinhood`, `backtest`.

## Console launcher (`bibi-tui`)

Plain stdlib TUI — no extra deps, ideal for Raspberry Pi. Lets you:

- enter Telegram and Robinhood Crypto API keys without touching `.env` by hand
- see current LIVE / PAPER cash and which bots are running
- start/stop modes (paper, live) **in the background** so closing the
  terminal doesn't kill them
- run one-shot modes (backtest, optimize) interactively
- tail logs and check process status

```
╔══ Bibi-Signal Launcher ═══════════════════════════════╗
  cwd:     /home/pi/Bibi-signal
  config:  tickers: QQQ, XLE · interval: 10m · crypto: off · dividends: on
  cash:    LIVE $100.00   PAPER $1000.00
  running: ● paper pid 4112 since 14:22
╚═══════════════════════════════════════════════════════╝

  Main menu
   [1] Configure       — API keys, strategy YAML
   [2] Backtest        — historical replay
   [3] Optimize        — parallel grid search
   [4] Paper           — virtual money on live prices
   [5] Live (all-in-1) — Telegram signals + paper mirror + crypto + dividends
   [6] Status          — running processes
   [7] Logs            — tail recent
   [0] Quit
```

Background processes survive the TUI exit (PID files in `data/run/`,
logs in `logs/`). Restart the Pi and re-run `bibi-tui` — the launcher
shows whatever was left running.

## Run as a system service (24/7 on a Pi)

For unattended operation, install the bot as a systemd unit:

```bash
sudo cp scripts/bibi-signal.service.example /etc/systemd/system/bibi-signal.service
# Edit User=, WorkingDirectory= for your account
sudo systemctl daemon-reload
sudo systemctl enable --now bibi-signal
journalctl -u bibi-signal -f          # follow logs
```

## Sanity-check the install

```bash
pytest                                        # 51 tests, all should pass
bibi-signal --mode paper --once --no-market-hours   # one tick, prints portfolio
```

## Three modes

The bot has three modes that map to natural stages of trusting it:

```
backtest  ──>  paper  ──>  live
   |             |           |
historical    live prices   live prices
data,         virtual $     real $ (via Telegram signals)
no risk       no risk       your risk
```

### 0. Optimize — find robust parameters before backtesting (optional)

Grid-searches strategy parameters across years of historical data with a
**train/test split** to detect overfitting. Train (default 70%) is used
to find candidate combos in parallel across CPU cores; the top 30 are
then evaluated on the held-out test set (default 30%). The grid sweeps
dip / profit / stop-loss / RSI threshold / trend filter / **trailing
take-profit + trail percent** — 3000 combos total. Output is a table
sorted by test-set score.

```bash
bibi-signal --mode optimize --ticker QQQ --years 5 --top 10
bibi-signal --mode optimize --ticker XLE --years 5 --workers 4
```

Pick a row where:
- TEST return is positive,
- TEST drawdown isn't worse than you can stomach,
- the ⚠️ overfit flag is **not** set (train ≫ test = the combo got lucky on
  history but probably won't generalize).

**Auto-apply**: when stdin is a terminal, optimize asks at the end which
rank to apply to `config.yaml`. Or pass `--apply N` to skip the prompt:

```bash
bibi-signal --mode optimize --ticker QQQ --years 5 --apply 1   # apply best combo
```

The 8 strategy fields for that ticker (dip, profit, stop, RSI on/off,
RSI threshold, uptrend on/off, trailing on/off, trail percent) are
rewritten in place; comments and unrelated keys are preserved, the
result is validated as a valid `StrategyConfig` before saving (with a
backup-and-restore on validation failure).

After apply, run a quick `--mode backtest` to confirm the numbers match
what optimize promised, then move on to paper trading.

**Optimize is not "AI prediction"** — it's a deterministic search over
parameter space with overfitting protection. Markets in the future
won't be the same as the past; consider the result a starting point,
not a guarantee.

### 1. Backtest — historical replay (one-shot, no setup)

Replays the strategy on years of past QQQ/XLE bars. Tells you if the
strategy was historically profitable, what the worst drawdown was, and
how it compares to buy-and-hold.

```bash
bibi-signal --mode backtest --ticker QQQ --years 5
bibi-signal --mode backtest --ticker XLE --years 5 --starting-cash 100
```

### 2. Paper — live prices, virtual money (no Telegram needed)

Runs the strategy on the **current live market** but trades only virtual
money in the PAPER environment of your DB. Console output, no broker, no
Telegram token required. This is the recommended next step after backtest:
let it run for 2–4 weeks and watch what happens.

```bash
# Continuous, normal cadence (every check_frequency_minutes)
bibi-signal --mode paper

# One tick and exit — handy for smoke tests
bibi-signal --mode paper --once

# Test on a weekend (skip the "market closed" guard)
bibi-signal --mode paper --once --no-market-hours
```

After each tick the console prints the paper portfolio summary:
cash, open lots, target/stop prices, realised + unrealised P&L, win rate.

### 3. Live — real signals via Telegram (your real account)

Full bot: stocks scheduler + crypto scheduler + dividend cron, all
broadcasting to Telegram. Every LIVE signal is also mirrored into PAPER
so you can compare. Requires `TELEGRAM_BOT_TOKEN` and
`TELEGRAM_ALLOWED_CHAT_IDS` in `.env`.

```bash
bibi-signal --mode live   # or just `bibi-signal`
```

## Price source for stocks (yfinance / alpaca / robinhood)

Three options. Crypto always uses the official Robinhood Crypto API.

| Source | Real-time? | Legal? | Cost | Setup |
|---|---|---|---|---|
| **yfinance** (default) | ~30s lag | yes | free | nothing |
| **alpaca** ✓ recommended | yes | yes (official) | free | sign up + 2 keys |
| **robinhood** | yes | ToS gray area | free | login + risk |

If you want real-time prices, **use Alpaca**. Alpaca is the only
recommended path for production:

```bash
pip install -e '.[alpaca]'

# 1. https://alpaca.markets/ -> Sign Up (no SSN needed for paper account)
# 2. Dashboard -> Paper Trading -> Generate API keys
# 3. Put them in .env:
#      ALPACA_PAPER_API_KEY=PK...
#      ALPACA_PAPER_API_SECRET=...

# 4. Switch source — either via TUI:
bibi-tui                   # → [1] Configure → [s] Switch price source → 2

#    or by hand in config.yaml:
#      price_source: alpaca
```

You only need an Alpaca **paper** account — no live trading account
required. Bibi-Signal does **not** use Alpaca's paper-trading orders;
our internal `paper_engine.py` simulates trades against whichever data
source you configure.

### Robinhood as a price source (advanced, not recommended)

If you specifically want the same numbers shown in the Robinhood mobile
app, you can route prices through `robin-stocks`. **This violates
Robinhood's ToS** and aggressive polling can lock your account. We
mitigate with a 30-second response cache and a 30-requests/minute local
guard, but the risk is non-zero.

```bash
pip install -e '.[robinhood]'
# .env:
#   ROBINHOOD_USERNAME=...
#   ROBINHOOD_PASSWORD=...
#   ROBINHOOD_MFA_SECRET=...   # optional, base32 TOTP seed for unattended
bibi-rh-login                  # one-time login; session cached
# config.yaml:
#   price_source: robinhood
```

### Auto-fallback

Whichever source you pick, if it fails (Alpaca rate limit, expired
Robinhood session, network), the bot **automatically falls back to
yfinance** so it doesn't crash. A warning is logged and the next tick
will retry the configured source.

## Persistence

Everything is in `data/bibi.db` (SQLite). Stop and restart the bot any
time — cash, open lots, trade history, fingerprints of already-sent
signals all survive. The only thing that doesn't persist is in-memory
`/set` overrides; edit `config.yaml` to make tweaks permanent.

## Telegram commands

**Trading (manual records of Robinhood fills)**

| Command | Args | What it does |
|---|---|---|
| `/buy` | `<ticker> <usd> <price>` | Record a buy you just made in Robinhood |
| `/sell` | `<ticker> <lot_id> <price>` | Record a sell of a specific lot |
| `/cash` | `<usd>` | Set free cash balance |
| `/status` | — | Portfolio, open lots, free cash |
| `/history` | `[n]` | Last n closed trades + ROI |

**Income**

| Command | Args | What it does |
|---|---|---|
| `/dividends` | — | Yields & upcoming ex-dates for SCHD/JEPI/JEPQ |
| `/divreceived` | `<ticker> <usd> [date]` | Record a dividend you got (adds to cash) |

**Strategy**

| Command | Args | What it does |
|---|---|---|
| `/rules` | `[ticker]` | Show current strategy parameters |
| `/set` | `<ticker> <param> <value>` | Tune a parameter in-memory |

**Simulation & crypto**

| Command | Args | What it does |
|---|---|---|
| `/paper` | — | Shadow paper portfolio (compare vs LIVE) |
| `/crypto` | — | Crypto holdings & quotes via Robinhood Crypto API |
| `/help` | — | List all commands |

## A note on $100 capital

With $100 the absolute returns are tiny (4% on a $20 lot = $0.80). That is
fine — at this stage the bot is a **discipline tracker**, not an income
source. Use the first 2–3 months to:

1. Run the backtest, look at Sharpe / max drawdown / win-rate.
2. Run the bot live but execute trades manually.
3. Compare your real fills vs. what the backtest expected.
4. Only scale capital after the strategy has proven itself on your real
   timing (slippage and your reaction time matter).

## Why signal-only on Robinhood

Robinhood does not expose a public API for stock/ETF orders. Unofficial
libraries (`robin-stocks`) exist but violate the ToS and can get your
account locked. This bot stays on the safe side: it tells you what to do,
you tap the buttons in the app.

## Multi-asset rotation (Dual Momentum)

A different kind of strategy from the single-asset ladder. Bot manages a
**basket of ETFs** (QQQ, XLE, SCHD, IWM, GLD by default) and rotates capital
to whichever has the strongest recent return — but only if that asset is
above its long SMA. If nothing qualifies, sits in cash.

This handles **sector rotation automatically**. When tech crashes (2022
QQQ −33%), capital flows into energy (2022 XLE +73%); when both crash,
defensive cash mode kicks in.

```bash
bash scripts/run.sh multi-asset --years 10
```

Configure the universe and parameters in `config.yaml`:

```yaml
multi_asset:
  enabled: false              # backtest works without enabling
  universe:
    - QQQ                     # tech
    - XLE                     # energy
    - SCHD                    # dividend
    - IWM                     # small-caps
    - GLD                     # gold (defensive)
  lookback_days: 90           # 3-month return window
  sma_long_period: 200        # absolute momentum filter
  rebalance_frequency_days: 7 # weekly
  top_n: 1                    # hold the single best (raise to 2-3 with bigger capital)
```

### Honest expectation

Backtest on the 10-year QQQ/XLE/SCHD/IWM/GLD universe (recent run):

| Strategy | Return | Max drawdown |
|---|---|---|
| QQQ buy & hold | +568% | ~33% |
| Dual Momentum (this) | +346% | ~33% |

Dual Momentum **made money** (+346%) and rotated through every regime
(34% QQQ, 23% XLE, 23% GLD, 8% cash, etc.) — but **underperformed pure
QQQ buy & hold** in this specific window because QQQ was the dominant
asset for most of the decade. The drawdown also wasn't materially
better, since QQQ's 2022 crash hit while we were partly in QQQ.

Where this strategy actually shines: **multi-decade backtests** that
include 1973-style stagflation or 2000 dot-com. In those regimes
single-ticker buy & hold gets crushed and rotation handles it. In our
10-year window, regime variety just wasn't enough to make rotation pay.

Lesson: **no single strategy beats every era**. Use Dual Momentum if
you want a robust default that survives any regime; use buy & hold or
DCA if you're confident in a particular asset's secular trend.

## Trailing take-profit (let winners run)

Each ticker has two exit modes:

**Classic** (default, `trailing_take_profit: false`):
the bot sells the moment a lot reaches `+profit_percent`. Predictable but
caps gains — if the price keeps running up, you've already exited.

**Trailing** (`trailing_take_profit: true`):
when a lot reaches `+profit_percent`, instead of selling the bot **arms a
trailing stop**. It keeps tracking the peak price; the lot is sold only
when price retraces `trail_percent` from that peak. Lets big winners run.

```yaml
QQQ:
  profit_percent: 0.04          # arm the trail at +4%
  trailing_take_profit: true
  trail_percent: 0.02           # exit when price drops 2% from the peak
```

How it plays out:

| Tick | Price | Profit | Peak | Action |
|---|---|---|---|---|
| Buy | $100 | 0% | — | enter long |
| 1   | $103 | +3% | — | hold (trail not armed yet) |
| 2   | $104 | +4% | $104 | **arm trail**, peak=$104 |
| 3   | $108 | +8% | $108 | hold, peak now $108 |
| 4   | $112 | +12% | $112 | hold, peak now $112 |
| 5   | $109.7 | +9.7% | $112 | **SELL** — retraced ≥2% from peak |

Without the trail you would have exited tick 2 at +4% ($4 on $100). With
the trail you exit tick 5 at +9.7% ($9.7).

The downside: the trail also gives back gains during the retrace. Use the
optimize mode to find a `trail_percent` that suits your tickers — a tight
trail (1-2%) protects gains but exits early; a loose trail (5-10%) lets
trends ride but gives back more on reversals.

```bash
bibi-signal --mode backtest --ticker QQQ --years 5    # compare classic vs trailing
```

(Tip: stop-loss still wins over trailing — even if armed, a price below
`stop_price` triggers a STOP signal, not a trail-exit.)

## Internal paper-trading

Every LIVE stock signal is *also* executed virtually in PAPER state inside
the same SQLite DB at the live price. After a few weeks compare:

- `/paper` — what the strategy alone would have earned (no human delay)
- `/status` + `/history` — what you actually earned (with your timing)

Big gap = your manual execution is hurting returns. PAPER negative = the
strategy itself is bad, fix it before scaling. No external broker needed
for this — pure internal simulation on yfinance prices.

## Robinhood Crypto (the only fully-automatable part)

Robinhood **does** publish an official Crypto Trading API
(https://docs.robinhood.com/crypto/trading/). It uses Ed25519 request
signing and supports market orders on BTC, ETH, SOL, etc. This bot ships
with a client (`robinhood_crypto.py`) and a strategy wrapper
(`crypto_engine.py`).

Setup:

1. In the Robinhood mobile app: *Account → Investing → Crypto Trading API*
2. Generate an API key. Robinhood shows you the API key string; you keep
   the corresponding Ed25519 private key (32-byte seed).
3. Put the api key in `ROBINHOOD_CRYPTO_API_KEY` and the base64-encoded
   seed in `ROBINHOOD_CRYPTO_PRIVATE_KEY_B64` in your `.env`.
4. In `config.yaml`, flip `crypto.enabled: true`. Start with
   `crypto.auto_execute: false` (signal-only). After a few weeks of clean
   signals, flip `auto_execute: true` for full automation.

`alpaca_paper.py` is kept as a stub for users who'd rather verify strategy
on Alpaca's separate paper environment, but is not required.

## Architecture

```
src/bibi_signal/
├── config.py            # Pydantic settings + YAML loader
├── database.py          # SQLAlchemy models, session factory, CRUD helpers
├── price_fetcher.py     # yfinance wrapper with retry/cache
├── indicators.py        # SMA, RSI, ATR
├── strategy.py          # ladder logic — pure function, fully testable
├── backtest.py          # vectorised backtest over historical data
├── paper_engine.py      # internal shadow simulation (PAPER environment)
├── dividend.py          # ex-date tracking + buy-the-dip-pre-divvy signals
├── robinhood_crypto.py  # official Robinhood Crypto API client (Ed25519)
├── crypto_engine.py     # crypto strategy wrapper, optional auto-execute
├── alpaca_paper.py      # alternative paper broker (optional, stub)
├── telegram_bot.py      # PTB v21 application + all command handlers
├── scheduler.py         # APScheduler: stocks tick + crypto tick + daily div
└── main.py              # entry point
```

## Risks

- The strategy is rule-based, not predictive. In a sustained bear market
  (think 2022 QQQ −33%) the ladder will exhaust cash and stop-losses will
  trigger. The trend filter (`require_uptrend: true`) mitigates but does
  not eliminate this.
- Frequent trades create taxable events in the US. Track lots carefully.
- yfinance is unofficial Yahoo scraping. It breaks occasionally. Monitor
  logs.
