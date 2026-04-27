"""Robinhood Crypto Trading API client.

Reference: https://docs.robinhood.com/crypto/trading/

Auth model
----------
Each request is signed with Ed25519. Server verifies the signature using the
public key registered with your API key.

Headers required:
    x-api-key:    <your api key string>
    x-signature:  base64(ed25519_sign(message))
    x-timestamp:  unix epoch seconds (string)

Where `message = api_key + timestamp + path + body`
(`body` is an empty string for GET requests).

The "private key" you store is the 32-byte Ed25519 seed, base64-encoded.

This client supports the endpoints we need for the bot:
    - GET  /api/v1/crypto/marketdata/best_bid_ask/?symbol=BTC-USD
    - GET  /api/v1/crypto/trading/accounts/
    - GET  /api/v1/crypto/trading/holdings/
    - POST /api/v1/crypto/trading/orders/
    - GET  /api/v1/crypto/trading/orders/{id}/
    - DELETE /api/v1/crypto/trading/orders/{id}/cancel/

NOTE: Robinhood's API surface evolves. If a call fails with a 4xx, check
the docs link above and adjust the path/body shape.
"""
from __future__ import annotations

import base64
import json
import time
import uuid
from dataclasses import dataclass
from typing import Any, Optional

import httpx
import structlog
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from .config import AppSettings

log = structlog.get_logger(__name__)


class RobinhoodCryptoError(RuntimeError):
    pass


class RobinhoodCryptoNotConfigured(RobinhoodCryptoError):
    pass


@dataclass(frozen=True)
class CryptoQuote:
    symbol: str
    bid: float
    ask: float
    mid: float


@dataclass(frozen=True)
class CryptoHolding:
    symbol: str        # e.g. "BTC"
    quantity: float
    quantity_available: float


@dataclass(frozen=True)
class CryptoOrderResult:
    order_id: str
    state: str         # "open" | "filled" | "cancelled" | ...
    symbol: str
    side: str
    type: str
    raw: dict[str, Any]


def _load_private_key(b64_seed: str) -> Ed25519PrivateKey:
    seed = base64.b64decode(b64_seed)
    if len(seed) != 32:
        raise RobinhoodCryptoError(
            f"Ed25519 private key seed must be 32 bytes, got {len(seed)}"
        )
    return Ed25519PrivateKey.from_private_bytes(seed)


class RobinhoodCryptoClient:
    """Synchronous client. Wrap in run_in_executor if calling from async code."""

    def __init__(self, settings: AppSettings, timeout: float = 10.0) -> None:
        if not (settings.robinhood_crypto_api_key and settings.robinhood_crypto_private_key_b64):
            raise RobinhoodCryptoNotConfigured(
                "ROBINHOOD_CRYPTO_API_KEY and ROBINHOOD_CRYPTO_PRIVATE_KEY_B64 must be set"
            )
        self._api_key = settings.robinhood_crypto_api_key
        self._private_key = _load_private_key(settings.robinhood_crypto_private_key_b64)
        self._base_url = settings.robinhood_crypto_base_url.rstrip("/")
        self._client = httpx.Client(timeout=timeout)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "RobinhoodCryptoClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ---------- signing ----------

    def _sign(self, timestamp: str, method: str, path: str, body: str) -> str:
        # Robinhood's documented message format. If they change it, adjust here.
        message = f"{self._api_key}{timestamp}{path}{method.upper()}{body}".encode()
        signature = self._private_key.sign(message)
        return base64.b64encode(signature).decode()

    def _headers(self, method: str, path: str, body: str) -> dict[str, str]:
        timestamp = str(int(time.time()))
        return {
            "x-api-key": self._api_key,
            "x-signature": self._sign(timestamp, method, path, body),
            "x-timestamp": timestamp,
            "Content-Type": "application/json",
        }

    def _request(self, method: str, path: str, *, params: dict | None = None,
                 json_body: dict | None = None) -> dict:
        body = json.dumps(json_body) if json_body is not None else ""
        full_path = path
        if params:
            qs = "&".join(f"{k}={v}" for k, v in params.items())
            full_path = f"{path}?{qs}"
        url = f"{self._base_url}{full_path}"
        headers = self._headers(method, full_path, body)
        try:
            resp = self._client.request(method, url, headers=headers, content=body or None)
        except httpx.HTTPError as e:
            raise RobinhoodCryptoError(f"network error: {e}") from e
        if resp.status_code >= 400:
            log.error("robinhood_crypto_http_error",
                      status=resp.status_code, path=path, body=resp.text[:500])
            raise RobinhoodCryptoError(f"{method} {path} -> {resp.status_code}: {resp.text}")
        if not resp.content:
            return {}
        return resp.json()

    # ---------- market data ----------

    def best_bid_ask(self, symbol: str) -> CryptoQuote:
        data = self._request("GET", "/api/v1/crypto/marketdata/best_bid_ask/",
                             params={"symbol": symbol})
        results = data.get("results") or []
        if not results:
            raise RobinhoodCryptoError(f"no quote for {symbol}")
        row = results[0]
        bid = float(row.get("bid_price") or row.get("bid") or 0)
        ask = float(row.get("ask_price") or row.get("ask") or 0)
        mid = (bid + ask) / 2 if bid and ask else (bid or ask)
        return CryptoQuote(symbol=symbol, bid=bid, ask=ask, mid=mid)

    # ---------- account ----------

    def get_account(self) -> dict:
        return self._request("GET", "/api/v1/crypto/trading/accounts/")

    def get_holdings(self) -> list[CryptoHolding]:
        data = self._request("GET", "/api/v1/crypto/trading/holdings/")
        out: list[CryptoHolding] = []
        for row in data.get("results", []):
            out.append(
                CryptoHolding(
                    symbol=str(row.get("asset_code") or row.get("symbol") or ""),
                    quantity=float(row.get("total_quantity") or row.get("quantity") or 0),
                    quantity_available=float(row.get("quantity_available_for_trading") or 0),
                )
            )
        return out

    def buying_power_usd(self) -> float:
        acct = self.get_account()
        # Different fields can appear depending on account state; try a couple.
        for key in ("buying_power", "buying_power_usd", "available_funds"):
            if key in acct:
                try:
                    return float(acct[key])
                except (TypeError, ValueError):
                    pass
        return 0.0

    # ---------- orders ----------

    def submit_market_order(
        self,
        symbol: str,
        side: str,
        usd_amount: Optional[float] = None,
        quantity: Optional[float] = None,
    ) -> CryptoOrderResult:
        """Place a market order. Specify EITHER usd_amount (for buys) or quantity."""
        if side not in ("buy", "sell"):
            raise ValueError("side must be 'buy' or 'sell'")
        if usd_amount is None and quantity is None:
            raise ValueError("specify usd_amount or quantity")

        market_config: dict[str, Any] = {}
        if usd_amount is not None:
            market_config["asset_quantity"] = None
            market_config["quote_amount"] = str(round(usd_amount, 2))
        if quantity is not None:
            market_config["asset_quantity"] = str(quantity)

        body = {
            "client_order_id": str(uuid.uuid4()),
            "symbol": symbol,
            "side": side,
            "type": "market",
            "market_order_config": market_config,
        }
        data = self._request("POST", "/api/v1/crypto/trading/orders/", json_body=body)
        return CryptoOrderResult(
            order_id=str(data.get("id", "")),
            state=str(data.get("state", "unknown")),
            symbol=symbol,
            side=side,
            type="market",
            raw=data,
        )

    def get_order(self, order_id: str) -> dict:
        return self._request("GET", f"/api/v1/crypto/trading/orders/{order_id}/")

    def cancel_order(self, order_id: str) -> dict:
        return self._request("POST", f"/api/v1/crypto/trading/orders/{order_id}/cancel/")
