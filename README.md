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

## Install

```bash
git clone <this-repo>
cd Bibi-signal
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

# Optional: copy .env.example to .env and fill in tokens (only needed for live mode).
cp .env.example .env
```

Sanity-check the install:

```bash
pytest                                        # 29 tests, all should pass
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
