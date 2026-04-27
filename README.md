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

## Quick start

```bash
# 1. Clone and install
git clone <this-repo>
cd Bibi-signal
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev,backtest]"

# 2. Configure
cp .env.example .env
# Edit .env: add TELEGRAM_BOT_TOKEN and TELEGRAM_ALLOWED_CHAT_IDS

# Optional: tune config.yaml (per-ticker dip/profit, stop-loss, RSI threshold)

# 3. Backtest before running live
bibi-backtest --ticker QQQ --years 5
bibi-backtest --ticker XLE --years 5

# 4. Run the signal bot
bibi-signal
```

## Telegram commands

| Command | Args | What it does |
|---|---|---|
| `/buy` | `<ticker> <usd> <price>` | Record a buy you just made in Robinhood |
| `/sell` | `<ticker> <lot_id> <price>` | Record a sell of a specific lot |
| `/cash` | `<usd>` | Set free cash balance |
| `/status` | — | Portfolio, open lots, free cash, unrealised P&L |
| `/rules` | `[ticker]` | Show current strategy parameters |
| `/set` | `<ticker> <param> <value>` | Tune a parameter (dip/profit/stop) |
| `/history` | `[n]` | Last n closed trades + ROI |
| `/help` | — | List commands |

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

A future module (`alpaca_paper.py`) will mirror every signal into an
Alpaca paper-trading account so you can verify the strategy on a live
broker API without risking capital.

## Architecture

```
src/bibi_signal/
├── config.py          # Pydantic settings + YAML loader
├── database.py        # SQLAlchemy models, session factory, CRUD helpers
├── price_fetcher.py   # yfinance wrapper with retry/cache
├── indicators.py      # SMA, RSI, ATR
├── strategy.py        # ladder logic — pure function, fully testable
├── backtest.py        # vectorised backtest over historical data
├── telegram_bot.py    # PTB v21 application + command handlers
├── scheduler.py       # APScheduler async, polls + dispatches signals
├── alpaca_paper.py    # stub for future paper-trading mirror
└── main.py            # entry point
```

## Risks

- The strategy is rule-based, not predictive. In a sustained bear market
  (think 2022 QQQ −33%) the ladder will exhaust cash and stop-losses will
  trigger. The trend filter (`require_uptrend: true`) mitigates but does
  not eliminate this.
- Frequent trades create taxable events in the US. Track lots carefully.
- yfinance is unofficial Yahoo scraping. It breaks occasionally. Monitor
  logs.
