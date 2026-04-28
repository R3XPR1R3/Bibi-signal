"""SQLAlchemy 2.0 models + a thin Repository layer.

Schema:
    cash       — single-row free cash balance (per environment: live or paper)
    lots       — open and closed buy lots (the ladder rungs)
    trades     — append-only audit log of every buy and sell
    signals    — emitted signals (idempotency: don't re-send the same one)
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Optional

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum as SAEnum,
    ForeignKey,
    Numeric,
    String,
    create_engine,
    select,
    text,
)
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    Session,
    mapped_column,
    relationship,
    sessionmaker,
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Environment(str, Enum):
    LIVE = "live"
    PAPER = "paper"


class LotStatus(str, Enum):
    OPEN = "open"
    CLOSED = "closed"


class TradeSide(str, Enum):
    BUY = "buy"
    SELL = "sell"


class SignalKind(str, Enum):
    BUY = "BUY"
    SELL = "SELL"
    STOP = "STOP"


class Cash(Base):
    __tablename__ = "cash"

    id: Mapped[int] = mapped_column(primary_key=True)
    environment: Mapped[Environment] = mapped_column(
        SAEnum(Environment), unique=True, nullable=False
    )
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False, default=Decimal("0"))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)


class Lot(Base):
    __tablename__ = "lots"

    id: Mapped[int] = mapped_column(primary_key=True)
    environment: Mapped[Environment] = mapped_column(SAEnum(Environment), nullable=False, index=True)
    ticker: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    buy_price: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(Numeric(18, 8), nullable=False)
    cost_basis: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    target_price: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False)
    stop_price: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False)
    status: Mapped[LotStatus] = mapped_column(SAEnum(LotStatus), default=LotStatus.OPEN, index=True)
    sell_price: Mapped[Optional[Decimal]] = mapped_column(Numeric(18, 6))
    realised_pnl: Mapped[Optional[Decimal]] = mapped_column(Numeric(18, 4))
    # Trailing take-profit state. peak_price = highest seen since entry;
    # trail_active = True once profit_percent crossed and trail engaged.
    # Both nullable for backward compat with pre-trailing DBs.
    peak_price: Mapped[Optional[Decimal]] = mapped_column(Numeric(18, 6))
    trail_active: Mapped[bool] = mapped_column(Boolean, default=False)
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    closed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    trades: Mapped[list["Trade"]] = relationship(back_populates="lot")


class Trade(Base):
    __tablename__ = "trades"

    id: Mapped[int] = mapped_column(primary_key=True)
    environment: Mapped[Environment] = mapped_column(SAEnum(Environment), nullable=False)
    lot_id: Mapped[Optional[int]] = mapped_column(ForeignKey("lots.id"))
    ticker: Mapped[str] = mapped_column(String(16), nullable=False)
    side: Mapped[TradeSide] = mapped_column(SAEnum(TradeSide), nullable=False)
    price: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(Numeric(18, 8), nullable=False)
    notional: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    executed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    lot: Mapped[Optional[Lot]] = relationship(back_populates="trades")


class Signal(Base):
    __tablename__ = "signals"

    id: Mapped[int] = mapped_column(primary_key=True)
    environment: Mapped[Environment] = mapped_column(SAEnum(Environment), nullable=False)
    ticker: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    kind: Mapped[SignalKind] = mapped_column(SAEnum(SignalKind), nullable=False)
    lot_id: Mapped[Optional[int]] = mapped_column(ForeignKey("lots.id"))
    price: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False)
    suggested_usd: Mapped[Optional[Decimal]] = mapped_column(Numeric(18, 4))
    fingerprint: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class DividendEvent(Base):
    __tablename__ = "dividend_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    environment: Mapped[Environment] = mapped_column(SAEnum(Environment), nullable=False, index=True)
    ticker: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    amount_usd: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    pay_date: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


def make_engine(database_url: str):
    return create_engine(database_url, echo=False, future=True)


def _ensure_lot_trail_columns(engine) -> None:
    """SQLite-only: idempotently add trail columns to existing DBs.

    `Base.metadata.create_all` only creates missing TABLES, not missing
    COLUMNS. For users upgrading from a pre-trailing DB, we ALTER TABLE
    in place. Safe to run on every startup — checks PRAGMA first.
    """
    if not engine.url.drivername.startswith("sqlite"):
        return  # other backends should use Alembic
    with engine.begin() as conn:
        cols = {row[1] for row in conn.execute(text("PRAGMA table_info(lots)"))}
        if "lots" not in {r[0] for r in conn.execute(text(
                "SELECT name FROM sqlite_master WHERE type='table'"))}:
            return  # fresh DB — create_all will handle it
        if "peak_price" not in cols:
            conn.execute(text("ALTER TABLE lots ADD COLUMN peak_price NUMERIC(18,6)"))
        if "trail_active" not in cols:
            conn.execute(text("ALTER TABLE lots ADD COLUMN trail_active BOOLEAN DEFAULT 0"))


def init_db(database_url: str) -> sessionmaker[Session]:
    """Create the schema if missing and return a session factory."""
    engine = make_engine(database_url)
    Base.metadata.create_all(engine)
    _ensure_lot_trail_columns(engine)
    return sessionmaker(engine, expire_on_commit=False, class_=Session)


# ---------- Repository helpers ----------

def get_cash(session: Session, env: Environment) -> Decimal:
    row = session.execute(select(Cash).where(Cash.environment == env)).scalar_one_or_none()
    return row.amount if row else Decimal("0")


def set_cash(session: Session, env: Environment, amount: Decimal) -> None:
    row = session.execute(select(Cash).where(Cash.environment == env)).scalar_one_or_none()
    if row is None:
        session.add(Cash(environment=env, amount=amount))
    else:
        row.amount = amount


def adjust_cash(session: Session, env: Environment, delta: Decimal) -> Decimal:
    current = get_cash(session, env)
    new_amount = current + delta
    set_cash(session, env, new_amount)
    return new_amount


def open_lots(session: Session, env: Environment, ticker: str | None = None) -> list[Lot]:
    stmt = select(Lot).where(Lot.environment == env, Lot.status == LotStatus.OPEN)
    if ticker is not None:
        stmt = stmt.where(Lot.ticker == ticker)
    return list(session.execute(stmt.order_by(Lot.opened_at)).scalars())


def closed_lots(session: Session, env: Environment, limit: int | None = None) -> list[Lot]:
    stmt = (
        select(Lot)
        .where(Lot.environment == env, Lot.status == LotStatus.CLOSED)
        .order_by(Lot.closed_at.desc())
    )
    if limit is not None:
        stmt = stmt.limit(limit)
    return list(session.execute(stmt).scalars())


def signal_already_sent(session: Session, fingerprint: str) -> bool:
    return session.execute(
        select(Signal.id).where(Signal.fingerprint == fingerprint)
    ).first() is not None
