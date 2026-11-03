from enum import StrEnum
from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class TradingMode(StrEnum):
    PAPER = "PAPER"
    LIVE = "LIVE"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=False)

    app_env: str = "development"
    app_name: str = "ai-quant-trader"
    database_url: str = "postgresql+asyncpg://quant:quant@localhost:5432/quant"
    redis_url: str = "redis://localhost:6379/0"

    trading_mode: TradingMode = TradingMode.PAPER
    allow_live_trading: bool = False

    ibkr_host: str = "127.0.0.1"
    ibkr_port: int = 7947
    ibkr_client_id: int = 71
    ibkr_account: str | None = None
    ibkr_connect_timeout_seconds: int = 15
    ibkr_stale_data_seconds: int = 10

    max_daily_loss_pct: float = Field(default=1.0, gt=0)
    max_total_exposure_pct: float = Field(default=20.0, gt=0, le=100)
    max_open_positions: int = Field(default=3, ge=1)
    max_position_pct: float = Field(default=5.0, gt=0, le=100)
    max_trade_risk_pct: float = Field(default=0.25, gt=0, le=100)
    max_spread_pct: float = Field(default=0.30, ge=0)

    @model_validator(mode="after")
    def protect_live_trading(self):
        if self.trading_mode == TradingMode.LIVE and not self.allow_live_trading:
            raise ValueError("LIVE trading requested while ALLOW_LIVE_TRADING=false")
        return self


settings = Settings()
