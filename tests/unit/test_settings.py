import pytest
from pydantic import ValidationError

from config.settings import Settings


def test_live_mode_requires_explicit_enable():
    with pytest.raises(ValidationError):
        Settings(trading_mode="LIVE", allow_live_trading=False, _env_file=None)


def test_paper_mode_is_allowed_by_default():
    s = Settings(trading_mode="PAPER", allow_live_trading=False, _env_file=None)
    assert s.trading_mode == "PAPER"
    assert s.ibkr_read_only is True


@pytest.mark.parametrize("mode,enabled", [("LIVE", True), ("PAPER", True)])
def test_no_live_escape_hatch(mode, enabled):
    with pytest.raises(ValidationError):
        Settings(trading_mode=mode, allow_live_trading=enabled, _env_file=None)


@pytest.mark.parametrize(
    "field,value",
    [
        ("ibkr_port", 0),
        ("ibkr_client_id", 0),
        ("ibkr_connect_timeout_seconds", 0),
        ("ibkr_stale_data_seconds", -1),
        ("max_trade_risk_pct", float("nan")),
        ("max_daily_loss_pct", float("inf")),
        ("ibkr_account", "U123"),
    ],
)
def test_invalid_safety_settings_rejected(field, value):
    with pytest.raises(ValidationError):
        Settings(**{field: value}, _env_file=None)


def test_watchlist_is_normalized_and_deduplicated():
    s = Settings(market_data_symbols=" aapl,MSFT,AAPL,, ", _env_file=None)
    assert s.watched_symbols == ["AAPL", "MSFT"]
