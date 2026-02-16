from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import time

from dotenv import load_dotenv


@dataclass(frozen=True)
class Settings:
    alpaca_api_key: str
    alpaca_secret_key: str
    alpaca_base_url: str
    webhook_secret: str
    flask_host: str = "0.0.0.0"
    flask_port: int = 8000
    log_level: str = "INFO"

    # Risk controls for small cash accounts
    notional_per_trade: float = 25.0
    max_positions: int = 3
    max_trades_per_day: int = 10
    daily_loss_limit: float = 20.0
    daily_buying_power_budget: float = 250.0

    timezone: str = "America/Chicago"
    market_open_ct: time = time(8, 30)
    trade_start_ct: time = time(8, 33)
    trade_end_ct: time = time(9, 30)
    flatten_time_ct: time = time(9, 35)


def _required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise ValueError(f"Missing required env var: {name}")
    return value


def load_settings() -> Settings:
    load_dotenv()
    notional = float(os.getenv("NOTIONAL_PER_TRADE", "25"))
    return Settings(
        alpaca_api_key=_required_env("ALPACA_API_KEY"),
        alpaca_secret_key=_required_env("ALPACA_SECRET_KEY"),
        alpaca_base_url=os.getenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets").strip(),
        webhook_secret=_required_env("WEBHOOK_SECRET"),
        flask_host=os.getenv("FLASK_HOST", "0.0.0.0").strip(),
        flask_port=int(os.getenv("FLASK_PORT", "8000")),
        log_level=os.getenv("LOG_LEVEL", "INFO").upper().strip(),
        notional_per_trade=10.0 if notional <= 10 else notional,
        max_positions=int(os.getenv("MAX_POSITIONS", "3")),
        max_trades_per_day=int(os.getenv("MAX_TRADES_PER_DAY", "10")),
        daily_loss_limit=float(os.getenv("DAILY_LOSS_LIMIT", "20")),
        daily_buying_power_budget=float(os.getenv("DAILY_BUYING_POWER_BUDGET", "250")),
    )
