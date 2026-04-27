"""Configuration: env vars via Pydantic Settings + YAML strategy config."""
from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class TickerConfig(BaseModel):
    enabled: bool = True
    dip_percent: float = Field(gt=0, lt=0.5)
    profit_percent: float = Field(gt=0, lt=0.5)
    stop_loss_percent: float = Field(gt=0, lt=0.5)
    min_trade_usd: float = Field(gt=0)
    max_trade_usd: float = Field(gt=0)
    max_open_lots: int = Field(ge=1, le=20)
    use_atr_sizing: bool = False
    atr_k: float = Field(default=1.5, gt=0)
    require_rsi_oversold: bool = True
    rsi_threshold: float = Field(default=35, ge=0, le=100)
    require_uptrend: bool = True
    sma_long_period: int = Field(default=200, ge=20, le=500)

    @field_validator("max_trade_usd")
    @classmethod
    def max_gte_min(cls, v: float, info) -> float:
        if "min_trade_usd" in info.data and v < info.data["min_trade_usd"]:
            raise ValueError("max_trade_usd must be >= min_trade_usd")
        return v


class ReinvestConfig(BaseModel):
    mode: Literal["all_to_pool", "split"] = "all_to_pool"
    trading_share: float = Field(default=0.8, ge=0, le=1)
    dividend_share: float = Field(default=0.2, ge=0, le=1)
    dividend_ticker: str = "SCHD"


class NotificationConfig(BaseModel):
    telegram: bool = True
    console: bool = True


class StrategyConfig(BaseModel):
    starting_cash: float = Field(gt=0)
    check_frequency_minutes: int = Field(ge=1, le=240)
    respect_market_hours: bool = True
    tickers: dict[str, TickerConfig]
    reinvest: ReinvestConfig = ReinvestConfig()
    notifications: NotificationConfig = NotificationConfig()

    @classmethod
    def from_yaml(cls, path: Path | str) -> "StrategyConfig":
        with open(path, "r") as f:
            return cls.model_validate(yaml.safe_load(f))


class AppSettings(BaseSettings):
    """Process-level settings from environment / .env."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    telegram_bot_token: str = ""
    telegram_allowed_chat_ids: str = ""  # comma-separated
    database_url: str = "sqlite:///data/bibi.db"
    config_path: str = "config.yaml"
    log_level: str = "INFO"

    alpaca_paper_api_key: str = ""
    alpaca_paper_api_secret: str = ""
    alpaca_paper_base_url: str = "https://paper-api.alpaca.markets"

    @property
    def allowed_chat_ids(self) -> set[int]:
        if not self.telegram_allowed_chat_ids.strip():
            return set()
        return {int(x.strip()) for x in self.telegram_allowed_chat_ids.split(",") if x.strip()}


def load_all(env_path: Path | str | None = None) -> tuple[AppSettings, StrategyConfig]:
    """Load environment + YAML strategy config."""
    settings = AppSettings(_env_file=env_path) if env_path else AppSettings()
    strategy = StrategyConfig.from_yaml(settings.config_path)
    return settings, strategy
