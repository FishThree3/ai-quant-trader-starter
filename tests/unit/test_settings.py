import pytest
from pydantic import ValidationError

from config.settings import Settings


def test_live_mode_requires_explicit_enable():
    with pytest.raises(ValidationError):
        Settings(trading_mode="LIVE", allow_live_trading=False, _env_file=None)


def test_paper_mode_is_allowed_by_default():
    s = Settings(trading_mode="PAPER", allow_live_trading=False, _env_file=None)
    assert s.trading_mode == "PAPER"
