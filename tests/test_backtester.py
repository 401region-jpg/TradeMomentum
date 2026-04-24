"""
tests/test_backtester.py
Тесты движка бэктеста. Без API — только синтетические данные.
"""

import numpy as np
import pandas as pd
import pytest

from backtest.backtester import Backtester, BacktestResult
from risk.risk_manager import RiskManager
from strategy.momentum import MomentumStrategy

CFG = {
    "strategy": {
        "ema_fast": 5,
        "ema_slow": 20,
        "rsi_period": 14,
        "rsi_long_max": 70,
        "rsi_long_min": 30,
        "rsi_short_min": 30,
        "rsi_short_max": 70,
        "atr_period": 14,
        "atr_sl_multiplier": 1.0,
        "atr_tp_multiplier": 3.0,
        "min_atr_pct": 0.0001,
        "min_body_pct": 0.0,
        "volume_ma_period": 5,
        "volume_spike_mult": 1.0,
        "atr_expansion_mult": 1.0,
        "max_positions": 3,
        "trade_only_in_windows": False,
        "allowed_hours": {"default": []},
        "global_trend": {"enabled": False},
    },
    "risk": {
        "capital_rub": 50_000.0,
        "max_position_pct": 0.10,
        "max_leverage": 2,
        "daily_loss_limit_pct": 0.05,
        "weekly_loss_limit_pct": 0.10,
        "max_trades_per_day": 20,
        "allow_shorts": True,
    },
}


def make_df(n: int = 500, seed: int = 42) -> pd.DataFrame:
    np.random.seed(seed)
    close = np.cumsum(np.random.normal(0, 1, n)) + 100
    close = np.abs(close) + 10
    noise = np.random.uniform(0.1, 0.5, n)
    return pd.DataFrame(
        {
            "time": pd.date_range("2024-01-01", periods=n, freq="15min", tz="UTC"),
            "open": close + np.random.uniform(-0.2, 0.2, n),
            "high": close + noise,
            "low": close - noise,
            "close": close,
            "volume": np.random.randint(100, 10000, n),
        }
    )


class TestBacktester:

    def setup_method(self):
        self.strategy = MomentumStrategy(CFG)
        self.risk = RiskManager(CFG)
        self.backtester = Backtester(self.strategy, self.risk)

    def test_returns_backtest_result(self):
        df = make_df(300)
        result = self.backtester.run(df, "TEST", lot_size=1, initial_capital=50_000)
        assert isinstance(result, BacktestResult)

    def test_equity_starts_at_initial_capital(self):
        df = make_df(300)
        result = self.backtester.run(df, "TEST", lot_size=1, initial_capital=50_000)
        assert result.equity.iloc[0] == pytest.approx(50_000, abs=1)

    def test_all_trades_have_exit(self):
        df = make_df(300)
        result = self.backtester.run(df, "TEST", lot_size=1, initial_capital=50_000)
        for trade in result.trades:
            assert trade.exit_price is not None
            assert trade.exit_reason in ("sl", "tp", "eod", "signal")

    def test_summary_keys_present(self):
        df = make_df(300)
        result = self.backtester.run(df, "TEST", lot_size=1, initial_capital=50_000)
        for key in [
            "total_trades",
            "hit_rate_pct",
            "total_pnl",
            "max_drawdown_pct",
            "sharpe_ratio",
            "profit_factor",
        ]:
            assert key in result.summary

    def test_no_trades_on_insufficient_data(self):
        df = make_df(10)
        with pytest.raises(ValueError):
            self.backtester.run(df, "TEST", lot_size=1, initial_capital=50_000)

    def test_tp_gives_positive_pnl_long(self):
        df = make_df(500)
        result = self.backtester.run(df, "TEST", lot_size=1, initial_capital=50_000)
        for t in result.trades:
            if t.exit_reason == "tp" and t.direction == "long":
                assert t.pnl > 0

    def test_sl_gives_negative_pnl_long(self):
        df = make_df(500)
        result = self.backtester.run(df, "TEST", lot_size=1, initial_capital=50_000)
        for t in result.trades:
            if t.exit_reason == "sl" and t.direction == "long":
                assert t.pnl < 0
