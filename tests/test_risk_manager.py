"""
tests/test_risk_manager.py
Unit-тесты для RiskManager. Без API.
"""

from decimal import Decimal

import pytest

from risk.risk_manager import (
    DailyLimitExceeded,
    MaxPositionsExceeded,
    MaxTradesPerDayExceeded,
    RiskManager,
    WeeklyLimitExceeded,
)

MINIMAL_CFG = {
    "risk": {
        "capital_rub": 100_000.0,
        "max_position_pct": 0.10,
        "max_leverage": 5,
        "daily_loss_limit_pct": 0.02,
        "weekly_loss_limit_pct": 0.05,
        "max_trades_per_day": 5,
        "allow_shorts": True,
    },
    "strategy": {
        "atr_sl_multiplier": 1.0,
        "atr_tp_multiplier": 3.0,
        "max_positions": 3,
    },
}


class TestRiskManager:

    def setup_method(self):
        self.rm = RiskManager(MINIMAL_CFG)

    def test_initial_capital(self):
        assert self.rm.capital == Decimal("100000")

    def test_check_entry_allowed_ok(self):
        self.rm.check_entry_allowed(current_positions_count=0)

    def test_max_positions_exceeded(self):
        with pytest.raises(MaxPositionsExceeded):
            self.rm.check_entry_allowed(current_positions_count=3)

    def test_max_trades_per_day(self):
        for _ in range(5):
            self.rm.record_pnl(Decimal("10"))
        with pytest.raises(MaxTradesPerDayExceeded):
            self.rm.check_entry_allowed(current_positions_count=0)

    def test_daily_loss_limit(self):
        self.rm.record_pnl(Decimal("-2001"))
        with pytest.raises(DailyLimitExceeded):
            self.rm.check_entry_allowed(current_positions_count=0)

    def test_weekly_loss_limit(self):
        rm = RiskManager(
            {
                "risk": {
                    "capital_rub": 100_000.0,
                    "max_position_pct": 0.10,
                    "max_leverage": 5,
                    "daily_loss_limit_pct": 0.99,
                    "weekly_loss_limit_pct": 0.05,
                    "max_trades_per_day": 100,
                    "allow_shorts": True,
                },
                "strategy": {"max_positions": 3},
            }
        )
        rm.record_pnl(Decimal("-5001"))
        with pytest.raises(WeeklyLimitExceeded):
            rm.check_entry_allowed(current_positions_count=0)

    def test_daily_reset(self):
        self.rm.record_pnl(Decimal("-2001"))
        self.rm.reset_daily()
        self.rm.check_entry_allowed(current_positions_count=0)

    def test_calculate_quantity_basic(self):
        qty = self.rm.calculate_quantity(
            price=Decimal("100"),
            lot_size=1,
            sl_distance=Decimal("2"),
        )
        assert qty > 0

    def test_calculate_quantity_zero_when_no_capital(self):
        rm = RiskManager(
            {
                "risk": {
                    "capital_rub": 100.0,
                    "max_position_pct": 0.10,
                    "max_leverage": 1,
                    "daily_loss_limit_pct": 0.05,
                    "weekly_loss_limit_pct": 0.10,
                    "max_trades_per_day": 10,
                    "allow_shorts": True,
                },
                "strategy": {"max_positions": 3},
            }
        )
        qty = rm.calculate_quantity(
            price=Decimal("10000"),
            lot_size=1000,
            sl_distance=Decimal("100"),
        )
        assert qty == 0

    def test_calculate_quantity_respects_leverage(self):
        rm_1x = RiskManager({**MINIMAL_CFG, "risk": {**MINIMAL_CFG["risk"], "max_leverage": 1}})
        rm_5x = RiskManager({**MINIMAL_CFG, "risk": {**MINIMAL_CFG["risk"], "max_leverage": 5}})
        qty_1x = rm_1x.calculate_quantity(Decimal("100"), 1, Decimal("2"))
        qty_5x = rm_5x.calculate_quantity(Decimal("100"), 1, Decimal("2"))
        assert qty_5x >= qty_1x

    def test_pnl_tracking(self):
        self.rm.record_pnl(Decimal("500"))
        self.rm.record_pnl(Decimal("-200"))
        assert self.rm.day_pnl == Decimal("300")
        assert self.rm.portfolio_trades == 2

    def test_sl_distance_zero_returns_zero(self):
        qty = self.rm.calculate_quantity(Decimal("100"), 1, Decimal("0"))
        assert qty == 0
