"""Stub for future Alpaca paper-trading mirror.

Idea: every signal the live bot emits is also "executed" against an Alpaca
paper account. This gives a parallel honest P&L of the strategy regardless
of how/when the user actually fills in Robinhood — useful for verifying
the strategy without risking capital.

Activated by setting ALPACA_PAPER_API_KEY / ALPACA_PAPER_API_SECRET in .env.
Not wired into the scheduler yet — that comes after the live MVP is stable.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

import structlog

from .config import AppSettings
from .strategy import SignalKind, SignalProposal

log = structlog.get_logger(__name__)


@dataclass
class PaperOrderResult:
    accepted: bool
    order_id: str | None
    filled_price: float | None
    error: str | None = None


class PaperBroker(Protocol):
    def submit(self, p: SignalProposal) -> PaperOrderResult: ...
    def positions(self) -> dict[str, float]: ...
    def cash(self) -> float: ...


class NullPaperBroker:
    """No-op fallback used when Alpaca credentials are absent."""

    def submit(self, p: SignalProposal) -> PaperOrderResult:
        return PaperOrderResult(accepted=False, order_id=None, filled_price=None, error="disabled")

    def positions(self) -> dict[str, float]:
        return {}

    def cash(self) -> float:
        return 0.0


def make_broker(settings: AppSettings) -> PaperBroker:
    """Returns a real Alpaca client if creds are set, otherwise a Null broker.

    Real client deferred until the alpaca-py dependency is needed. For now
    we don't pin it in pyproject.toml to keep the live MVP install lean.
    """
    if not (settings.alpaca_paper_api_key and settings.alpaca_paper_api_secret):
        log.info("alpaca_paper_disabled")
        return NullPaperBroker()

    # Deferred import; user must `pip install alpaca-py` before enabling.
    try:
        from alpaca.trading.client import TradingClient  # type: ignore
        from alpaca.trading.enums import OrderSide, TimeInForce  # type: ignore
        from alpaca.trading.requests import MarketOrderRequest  # type: ignore
    except ImportError:
        log.warning("alpaca_py_not_installed", hint="pip install alpaca-py")
        return NullPaperBroker()

    client = TradingClient(
        settings.alpaca_paper_api_key,
        settings.alpaca_paper_api_secret,
        paper=True,
    )

    class _AlpacaPaperBroker:
        def submit(self, p: SignalProposal) -> PaperOrderResult:
            if p.kind == SignalKind.BUY and p.suggested_usd:
                req = MarketOrderRequest(
                    symbol=p.ticker,
                    notional=p.suggested_usd,
                    side=OrderSide.BUY,
                    time_in_force=TimeInForce.DAY,
                )
            elif p.kind in (SignalKind.SELL, SignalKind.STOP):
                # Caller must resolve qty from the corresponding lot before submit.
                return PaperOrderResult(
                    accepted=False, order_id=None, filled_price=None,
                    error="qty resolution not implemented in stub",
                )
            else:
                return PaperOrderResult(accepted=False, order_id=None, filled_price=None, error="ignored")
            order = client.submit_order(req)
            return PaperOrderResult(
                accepted=True,
                order_id=str(order.id),
                filled_price=float(order.filled_avg_price) if order.filled_avg_price else None,
            )

        def positions(self) -> dict[str, float]:
            return {p.symbol: float(p.qty) for p in client.get_all_positions()}

        def cash(self) -> float:
            return float(client.get_account().cash)

    return _AlpacaPaperBroker()
