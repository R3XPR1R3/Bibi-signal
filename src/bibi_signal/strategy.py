"""Ladder Swing strategy — pure functions, fully testable.

Decision flow per ticker on each tick:

    1. For every OPEN lot:
        - if price >= lot.target_price          -> emit SELL(lot)
        - elif price <= lot.stop_price           -> emit STOP(lot)
    2. If no SELL/STOP triggered AND we have room (open_lots < max_open_lots):
        - compute reference price (last lot's buy_price OR latest close)
        - if price <= reference * (1 - dip_percent)
          AND (not require_uptrend OR in_uptrend)
          AND (not require_rsi_oversold OR rsi <= rsi_threshold)
          AND free_cash >= min_trade_usd:
            -> emit BUY with suggested size
    3. Else HOLD.

The function is pure: it takes a snapshot, returns SignalProposals.
The caller persists results and dispatches notifications.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import Optional

from .config import TickerConfig


class SignalKind(str, Enum):
    BUY = "BUY"
    SELL = "SELL"
    STOP = "STOP"
    HOLD = "HOLD"


@dataclass(frozen=True)
class LotSnapshot:
    """Subset of a Lot row needed for decisions. Decimal-friendly.

    `peak_price` is the highest price observed since entry; `trail_active`
    means the trailing take-profit has been armed (price hit profit_percent
    at least once). Callers must update both before invoking evaluate().
    """
    id: int
    ticker: str
    buy_price: float
    quantity: float
    target_price: float
    stop_price: float
    peak_price: float | None = None
    trail_active: bool = False


@dataclass(frozen=True)
class MarketSnapshot:
    ticker: str
    price: float
    sma_long: float | None  # None when uptrend filter disabled / not enough data
    rsi: float | None
    atr: float | None
    in_uptrend: bool


@dataclass(frozen=True)
class SignalProposal:
    kind: SignalKind
    ticker: str
    price: float
    lot_id: Optional[int] = None
    suggested_usd: Optional[float] = None
    reason: str = ""

    def fingerprint(self) -> str:
        """Deterministic id so the same signal isn't re-emitted on the next tick."""
        bucket_price = round(self.price, 2)
        return f"{self.ticker}:{self.kind.value}:{self.lot_id or 0}:{bucket_price}"


def _suggested_buy_usd(free_cash: float, cfg: TickerConfig) -> float:
    """Size: 10–20% of free cash, clamped to [min_trade_usd, max_trade_usd]."""
    raw = free_cash * 0.15
    sized = max(cfg.min_trade_usd, min(cfg.max_trade_usd, raw))
    return round(sized, 2) if sized <= free_cash else round(free_cash, 2)


def _effective_dip(cfg: TickerConfig, market: MarketSnapshot) -> float:
    if cfg.use_atr_sizing and market.atr and market.price > 0:
        return min(0.20, max(0.005, cfg.atr_k * market.atr / market.price))
    return cfg.dip_percent


def evaluate(
    cfg: TickerConfig,
    market: MarketSnapshot,
    open_lots_for_ticker: list[LotSnapshot],
    free_cash: float,
) -> list[SignalProposal]:
    """Return a list of proposals (possibly multiple SELL/STOP plus a BUY).

    No I/O. Caller is responsible for persistence and dispatch.
    """
    if not cfg.enabled:
        return []

    proposals: list[SignalProposal] = []

    # 1. Exit decisions on every open lot — independent of buy gating.
    for lot in open_lots_for_ticker:
        # Stop-loss always wins (caps downside even when trailing armed).
        if market.price <= lot.stop_price:
            proposals.append(
                SignalProposal(
                    kind=SignalKind.STOP,
                    ticker=market.ticker,
                    price=market.price,
                    lot_id=lot.id,
                    reason=f"price {market.price:.2f} <= stop {lot.stop_price:.2f}",
                )
            )
            continue

        if cfg.trailing_take_profit:
            # Trailing mode. Two phases:
            #   not armed: hold until price hits target_price (= profit_percent above entry)
            #   armed:     keep updating peak; sell only on retrace
            if not lot.trail_active:
                # Caller will arm us next tick once price >= target. Until then, hold.
                continue
            peak = lot.peak_price if lot.peak_price is not None else lot.buy_price
            trail_stop = peak * (1 - cfg.trail_percent)
            if market.price <= trail_stop:
                proposals.append(
                    SignalProposal(
                        kind=SignalKind.SELL,
                        ticker=market.ticker,
                        price=market.price,
                        lot_id=lot.id,
                        reason=(
                            f"trail: price {market.price:.2f} <= "
                            f"peak {peak:.2f} - {cfg.trail_percent*100:.1f}% "
                            f"= {trail_stop:.2f}"
                        ),
                    )
                )
        else:
            # Classic fixed-target mode (original behaviour).
            if market.price >= lot.target_price:
                proposals.append(
                    SignalProposal(
                        kind=SignalKind.SELL,
                        ticker=market.ticker,
                        price=market.price,
                        lot_id=lot.id,
                        reason=f"price {market.price:.2f} >= target {lot.target_price:.2f}",
                    )
                )

    # 2. Entry decision (only if no exit fired this tick — keeps reasoning clean).
    if proposals:
        return proposals

    if len(open_lots_for_ticker) >= cfg.max_open_lots:
        return [
            SignalProposal(
                kind=SignalKind.HOLD,
                ticker=market.ticker,
                price=market.price,
                reason=f"ladder full ({len(open_lots_for_ticker)}/{cfg.max_open_lots})",
            )
        ]

    if free_cash < cfg.min_trade_usd:
        return [
            SignalProposal(
                kind=SignalKind.HOLD,
                ticker=market.ticker,
                price=market.price,
                reason=f"insufficient cash ${free_cash:.2f} < ${cfg.min_trade_usd:.2f}",
            )
        ]

    # Reference price: last buy if a ladder is in progress, otherwise current price
    # (so the next dip is measured from "now" — first rung).
    if open_lots_for_ticker:
        reference = open_lots_for_ticker[-1].buy_price
    else:
        reference = market.price

    dip = _effective_dip(cfg, market)
    threshold = reference * (1 - dip) if open_lots_for_ticker else market.price
    # First rung: buy immediately if filters pass. Subsequent rungs wait for the dip.

    if open_lots_for_ticker and market.price > threshold:
        return [
            SignalProposal(
                kind=SignalKind.HOLD,
                ticker=market.ticker,
                price=market.price,
                reason=(
                    f"price {market.price:.2f} > dip threshold {threshold:.2f} "
                    f"(ref {reference:.2f}, dip {dip*100:.2f}%)"
                ),
            )
        ]

    if cfg.require_uptrend and not market.in_uptrend:
        return [
            SignalProposal(
                kind=SignalKind.HOLD,
                ticker=market.ticker,
                price=market.price,
                reason="trend filter: price below long SMA",
            )
        ]

    if cfg.require_rsi_oversold:
        if market.rsi is None or market.rsi > cfg.rsi_threshold:
            return [
                SignalProposal(
                    kind=SignalKind.HOLD,
                    ticker=market.ticker,
                    price=market.price,
                    reason=(
                        f"RSI filter: {market.rsi:.1f} > {cfg.rsi_threshold}"
                        if market.rsi is not None
                        else "RSI unavailable"
                    ),
                )
            ]

    suggested = _suggested_buy_usd(free_cash, cfg)
    if suggested < cfg.min_trade_usd:
        return [
            SignalProposal(
                kind=SignalKind.HOLD,
                ticker=market.ticker,
                price=market.price,
                reason=f"sized buy ${suggested:.2f} < min ${cfg.min_trade_usd:.2f}",
            )
        ]

    rsi_str = f"{market.rsi:.1f}" if market.rsi is not None else "n/a"
    return [
        SignalProposal(
            kind=SignalKind.BUY,
            ticker=market.ticker,
            price=market.price,
            suggested_usd=suggested,
            reason=f"dip {dip*100:.2f}% from {reference:.2f}; rsi={rsi_str}",
        )
    ]


# ---------- helpers used when recording a buy/sell ----------

def compute_targets(buy_price: float, cfg: TickerConfig) -> tuple[float, float]:
    """Returns (target_price, stop_price) for a new lot."""
    target = buy_price * (1 + cfg.profit_percent)
    stop = buy_price * (1 - cfg.stop_loss_percent)
    return round(target, 4), round(stop, 4)


def realised_pnl(buy_price: Decimal, sell_price: Decimal, quantity: Decimal) -> Decimal:
    return (sell_price - buy_price) * quantity


@dataclass(frozen=True)
class TrailUpdate:
    """Mutation a caller should apply to a lot before evaluating SELL conditions.

    new_peak_price is None if the peak didn't change. arm is True only on
    the tick that the trail first activates.
    """
    lot_id: int
    new_peak_price: float | None
    arm: bool


def trail_updates(
    cfg: TickerConfig,
    open_lots_for_ticker: list[LotSnapshot],
    current_price: float,
) -> list[TrailUpdate]:
    """Compute peak/arm state changes for one tick. No side effects.

    Caller persists the returned updates (DB, in-memory) before calling
    evaluate(), so the SELL decision uses fresh state.
    """
    if not cfg.trailing_take_profit:
        return []
    out: list[TrailUpdate] = []
    for lot in open_lots_for_ticker:
        # Should we arm? (price reached the activation threshold for the first time)
        activation = lot.buy_price * (1 + cfg.profit_percent)
        arm = (not lot.trail_active) and current_price >= activation

        # Should we update the peak? Only meaningful once armed (or arming this tick).
        new_peak: float | None = None
        if lot.trail_active or arm:
            current_peak = lot.peak_price if lot.peak_price is not None else lot.buy_price
            if current_price > current_peak:
                new_peak = current_price

        if arm or new_peak is not None:
            out.append(TrailUpdate(lot_id=lot.id, new_peak_price=new_peak, arm=arm))
    return out
