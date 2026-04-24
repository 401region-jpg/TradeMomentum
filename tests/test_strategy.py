import numpy as np
import pandas as pd

from strategy.momentum import MomentumStrategy

MINIMAL_CFG = {
    "strategy": {
        "ema_fast": 5,
        "ema_slow": 20,
        "rsi_period": 14,
        "rsi_long_max": 80,
        "rsi_long_min": 20,
        "rsi_short_min": 20,
        "rsi_short_max": 80,
        "atr_period": 14,
        "atr_sl_multiplier": 1.0,
        "atr_tp_multiplier": 3.0,
        "min_atr_pct": 0.00001,
        "min_body_pct": 0.0,
        "volume_ma_period": 5,
        "volume_spike_mult": 1.0,
        "atr_expansion_mult": 1.0,
        "max_positions": 3,
        "trade_only_in_windows": False,
        "allowed_hours": {"default": []},
        "global_trend": {"enabled": False},
    },
    "risk": {"allow_shorts": True},
}


def make_df(n: int = 100, trend: str = "up") -> pd.DataFrame:
    np.random.seed(42)
    prices = [100.0]
    for _ in range(n - 1):
        change = np.random.normal(0.1 if trend == "up" else -0.1, 0.5)
        prices.append(max(prices[-1] + change, 0.01))
    prices = np.array(prices)
    high = prices + np.random.uniform(0.1, 0.5, n)
    low = prices - np.random.uniform(0.1, 0.5, n)
    return pd.DataFrame(
        {
            "time": pd.date_range("2024-01-01", periods=n, freq="15min", tz="UTC"),
            "open": prices,
            "high": high,
            "low": low,
            "close": prices,
            "volume": np.random.randint(100, 1000, n),
        }
    )


class TestMomentumStrategy:

    def setup_method(self):
        self.strategy = MomentumStrategy(MINIMAL_CFG)

    def test_name(self):
        assert "TrendFollower" in self.strategy.name()

    def test_add_indicators_columns(self):
        df = make_df(100)
        result = self.strategy.add_indicators(df)
        for col in ["ema_fast", "ema_slow", "rsi", "atr", "trend_up", "trend_down"]:
            assert col in result.columns, f"Колонка {col!r} отсутствует"

    def test_no_signals_on_short_data(self):
        df = make_df(10)
        signals = self.strategy.generate_signals(df, "TEST")
        assert signals == []

    def test_signals_have_required_fields_if_any(self):
        df = make_df(150, trend="up")
        signals = self.strategy.generate_signals(df, "TEST")
        for sig in signals:
            assert sig.ticker == "TEST"
            assert sig.price > 0
            assert sig.sl_price > 0
            assert sig.tp_price > 0

    def test_sl_tp_geometry(self):
        df = make_df(150, trend="up")
        signals = self.strategy.generate_signals(df, "TEST")
        for sig in signals:
            if sig.type.value == "long":
                assert sig.sl_price < sig.price
                assert sig.tp_price > sig.price
            elif sig.type.value == "short":
                assert sig.sl_price > sig.price
                assert sig.tp_price < sig.price

    def test_rr_ratio_matches_config(self):
        df = make_df(150, trend="up")
        signals = self.strategy.generate_signals(df, "TEST")
        for sig in signals:
            win = abs(sig.tp_price - sig.price)
            risk = abs(sig.sl_price - sig.price)
            if risk > 0:
                rr = float(win / risk)
                assert abs(rr - 3.0) < 0.05

    def test_no_nan_in_indicators(self):
        df = make_df(200)
        result = self.strategy.add_indicators(df)
        tail = result.iloc[30:]
        for col in ["ema_fast", "ema_slow", "rsi", "atr"]:
            assert tail[col].isna().sum() == 0

    def test_deterministic(self):
        df = make_df(150)
        s1 = self.strategy.generate_signals(df, "T")
        s2 = self.strategy.generate_signals(df, "T")
        assert len(s1) == len(s2)
        for a, b in zip(s1, s2, strict=False):
            assert a.type == b.type
            assert a.price == b.price
