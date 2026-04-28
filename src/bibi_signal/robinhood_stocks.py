"""Robinhood STOCKS price source via the unofficial robin-stocks library.

⚠️  IMPORTANT: This uses Robinhood's INTERNAL endpoints, not an official API.
    Robinhood's ToS forbids automated access. Aggressive polling can get
    your account locked. We mitigate that with three guardrails:

      1. Aggressive caching (default 30s TTL).
      2. Hard rate limit: max N requests per minute (default 30).
      3. Session caching so we don't re-login every run.

    Use at your own risk. For PRICE DATA ONLY — never put trading
    automation on top of this. For trading use the Robinhood mobile app
    manually (signal-only flow).

Configuration in .env:
    PRICE_SOURCE=robinhood
    ROBINHOOD_USERNAME=you@example.com
    ROBINHOOD_PASSWORD=...
    ROBINHOOD_MFA_SECRET=...     # base32 TOTP seed (optional, for unattended)

First-time setup: run `bibi-rh-login` once to authenticate interactively.
The session is then cached in ~/.tokens/robinhood.pickle and reused.
"""
from __future__ import annotations

import os
import sys
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from getpass import getpass
from pathlib import Path
from typing import Optional

import structlog

log = structlog.get_logger(__name__)


# ---------- guardrails ----------

DEFAULT_CACHE_TTL_SECONDS = 30
DEFAULT_RATE_LIMIT_PER_MIN = 30


class RobinhoodNotConfigured(RuntimeError):
    pass


class RobinhoodNotInstalled(RuntimeError):
    pass


class RobinhoodRateLimited(RuntimeError):
    """Raised when our local guard refuses to make another request."""


def _import_rh():
    try:
        import robin_stocks.robinhood as rh
        return rh
    except ImportError as e:
        raise RobinhoodNotInstalled(
            "robin-stocks is not installed. Install with:\n"
            "  pip install -e '.[robinhood]'"
        ) from e


@dataclass
class _RateLimitGuard:
    """Sliding-window limiter. Refuses if more than `max_per_minute` calls
    occurred in the last 60 seconds. Avoids bursts that look like a bot."""
    max_per_minute: int = DEFAULT_RATE_LIMIT_PER_MIN
    _calls: deque = None

    def __post_init__(self):
        self._calls = deque()

    def check(self) -> None:
        now = time.time()
        cutoff = now - 60
        while self._calls and self._calls[0] < cutoff:
            self._calls.popleft()
        if len(self._calls) >= self.max_per_minute:
            wait = 60 - (now - self._calls[0])
            raise RobinhoodRateLimited(
                f"local guard: {len(self._calls)} calls in last 60s, "
                f"max {self.max_per_minute}; wait {wait:.0f}s"
            )
        self._calls.append(now)


# ---------- price source ----------

@dataclass(frozen=True)
class RHQuote:
    symbol: str
    price: float
    fetched_at: datetime


class RobinhoodPriceSource:
    """Cached, rate-limited price reader.

    Constructed once and shared across the app. Not thread-safe — the
    scheduler is single-threaded async so this is fine.
    """

    def __init__(
        self,
        cache_ttl_seconds: int = DEFAULT_CACHE_TTL_SECONDS,
        rate_limit_per_min: int = DEFAULT_RATE_LIMIT_PER_MIN,
    ):
        self._rh = _import_rh()
        if not self._is_logged_in():
            raise RobinhoodNotConfigured(
                "no cached Robinhood session. Run `bibi-rh-login` once first."
            )
        self._cache_ttl = cache_ttl_seconds
        self._guard = _RateLimitGuard(rate_limit_per_min)
        self._cache: dict[str, tuple[float, float]] = {}  # symbol -> (price, fetched_at)

    def _is_logged_in(self) -> bool:
        # robin-stocks stores session at ~/.tokens/robinhood.pickle by default.
        token_path = Path.home() / ".tokens" / "robinhood.pickle"
        return token_path.exists()

    def get_price(self, symbol: str) -> RHQuote:
        now = time.time()
        cached = self._cache.get(symbol)
        if cached and now - cached[1] < self._cache_ttl:
            return RHQuote(symbol, cached[0], datetime.fromtimestamp(cached[1], tz=timezone.utc))

        self._guard.check()
        try:
            raw = self._rh.stocks.get_latest_price(symbol)
        except Exception as e:
            raise RuntimeError(f"robinhood get_latest_price({symbol}) failed: {e}") from e
        if not raw or raw[0] is None:
            raise RuntimeError(f"robinhood returned no price for {symbol}")
        price = float(raw[0])
        self._cache[symbol] = (price, now)
        return RHQuote(symbol, price, datetime.fromtimestamp(now, tz=timezone.utc))

    def get_prices(self, symbols: list[str]) -> dict[str, RHQuote]:
        """Batch fetch — one HTTP call for all symbols. Always preferred over
        loop of get_price() to keep the request count low."""
        now = time.time()
        # Honor cache for any symbol that's still fresh.
        result: dict[str, RHQuote] = {}
        to_fetch: list[str] = []
        for sym in symbols:
            cached = self._cache.get(sym)
            if cached and now - cached[1] < self._cache_ttl:
                result[sym] = RHQuote(
                    sym, cached[0], datetime.fromtimestamp(cached[1], tz=timezone.utc)
                )
            else:
                to_fetch.append(sym)
        if not to_fetch:
            return result

        self._guard.check()
        try:
            raw = self._rh.stocks.get_latest_price(to_fetch)
        except Exception as e:
            raise RuntimeError(f"robinhood get_latest_price({to_fetch}) failed: {e}") from e
        for sym, val in zip(to_fetch, raw or []):
            if val is None:
                continue
            price = float(val)
            self._cache[sym] = (price, now)
            result[sym] = RHQuote(sym, price, datetime.fromtimestamp(now, tz=timezone.utc))
        return result

    def clear_cache(self) -> None:
        self._cache.clear()


# ---------- login CLI ----------

def login_cli() -> None:
    """Interactive login. Run once; session is cached for subsequent use.

    Usage:  bibi-rh-login
    """
    print("════════════════════════════════════════════════════════════════")
    print("  Robinhood STOCKS login (unofficial — for price data only)")
    print("  Risks were already explained. Press Ctrl+C to cancel.")
    print("════════════════════════════════════════════════════════════════")
    print()

    try:
        rh = _import_rh()
    except RobinhoodNotInstalled as e:
        print(str(e), file=sys.stderr)
        sys.exit(2)

    username = (
        os.environ.get("ROBINHOOD_USERNAME")
        or input("  Robinhood email: ").strip()
    )
    password = (
        os.environ.get("ROBINHOOD_PASSWORD")
        or getpass("  Robinhood password (hidden): ")
    )
    mfa_secret = os.environ.get("ROBINHOOD_MFA_SECRET", "").strip()

    mfa_code: Optional[str] = None
    if mfa_secret:
        try:
            import pyotp
            mfa_code = pyotp.TOTP(mfa_secret).now()
            print(f"  Generated TOTP from MFA secret: {mfa_code}")
        except Exception as e:
            print(f"  ⚠ pyotp failed: {e}; falling back to manual entry")
            mfa_secret = ""
    if not mfa_secret:
        manual = input("  MFA code (leave empty for app push approval): ").strip()
        if manual:
            mfa_code = manual

    print("\n  Logging in…")
    try:
        rh.login(username=username, password=password, mfa_code=mfa_code, store_session=True)
    except Exception as e:
        print(f"  ✗ login failed: {e}", file=sys.stderr)
        sys.exit(1)

    # Sanity-check by fetching a known ticker.
    try:
        test = rh.stocks.get_latest_price("QQQ")
        if test and test[0]:
            print(f"  ✓ login OK, sanity check QQQ = ${float(test[0]):.2f}")
        else:
            print("  ⚠ login looked OK but sanity-check returned empty")
    except Exception as e:
        print(f"  ⚠ sanity-check failed: {e}", file=sys.stderr)

    token_path = Path.home() / ".tokens" / "robinhood.pickle"
    if token_path.exists():
        print(f"\n  Session cached at {token_path}")
        print("  Subsequent bot runs will reuse this session — no need to re-login")
        print("  unless Robinhood expires it (typically every few weeks).")
    print()
