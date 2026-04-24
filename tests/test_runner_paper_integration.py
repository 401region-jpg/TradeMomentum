import asyncio
import sys
import types
from dataclasses import dataclass
from decimal import Decimal

import pandas as pd
import pytest

import runner
from broker.base import OrderDirection
from strategy.base import Signal, SignalType


@dataclass
class _FakePosition:
    ticker: str
    quantity: int


class _FakePaperBroker:
    def __init__(self, _initial_balance: Decimal):
        self.positions: dict[str, _FakePosition] = {}
        self.trade_log: list[dict] = []

    async def get_positions(self) -> list[_FakePosition]:
        return list(self.positions.values())

    async def place_market_order(
        self,
        _figi: str,
        ticker: str,
        direction: OrderDirection,
        quantity: int,
        execution_price: Decimal | None = None,
    ):
        if direction == OrderDirection.BUY:
            self.positions[ticker] = _FakePosition(ticker=ticker, quantity=quantity)
        else:
            self.positions.pop(ticker, None)
        self.trade_log.append(
            {
                "ticker": ticker,
                "direction": direction.value,
                "qty": quantity,
                "price": float(execution_price or Decimal("0")),
            }
        )

    def update_prices(self, _prices: dict[str, Decimal]) -> None:
        return

    def get_total_equity(self) -> Decimal:
        return Decimal("1000")

    def get_trade_log(self) -> list[dict]:
        return self.trade_log


class _FakeRiskManager:
    def __init__(self, _cfg: dict):
        self.recorded_pnls: list[Decimal] = []

    def check_entry_allowed(self, _pos_count: int, _now_utc):
        return

    def calculate_quantity(self, _price: Decimal, _lot: int, _sl_dist: Decimal, _now_utc):
        return 1

    def record_pnl(self, pnl: Decimal) -> None:
        self.recorded_pnls.append(pnl)

    def update_capital(self, _c: Decimal) -> None:
        return


class _FakeNotifier:
    opened: list[dict] = []
    closed: list[dict] = []

    def __init__(self, *_args, **_kwargs):
        _FakeNotifier.opened = []
        _FakeNotifier.closed = []

    async def notify_bot_started(self, *_args, **_kwargs):
        return

    async def notify_bot_stopped(self, *_args, **_kwargs):
        return

    async def notify_error(self, *_args, **_kwargs):
        return

    async def notify_trade_open(
        self, ticker, direction, price, quantity, sl, tp, reason, mode="paper"
    ):
        self.opened.append(
            {
                "ticker": ticker,
                "direction": direction,
                "price": price,
                "quantity": quantity,
                "sl": sl,
                "tp": tp,
                "reason": reason,
                "mode": mode,
            }
        )

    async def notify_trade_close(
        self,
        ticker,
        direction,
        entry_price,
        exit_price,
        quantity,
        pnl,
        reason,
        mode="paper",
    ):
        self.closed.append(
            {
                "ticker": ticker,
                "direction": direction,
                "entry_price": entry_price,
                "exit_price": exit_price,
                "quantity": quantity,
                "pnl": pnl,
                "reason": reason,
                "mode": mode,
            }
        )


class _FakeStrategy:
    def __init__(self, _cfg: dict):
        self._sent_entry = False

    def generate_signals(self, _df: pd.DataFrame, ticker: str) -> list[Signal]:
        if self._sent_entry:
            return []
        self._sent_entry = True
        return [
            Signal(
                type=SignalType.LONG,
                ticker=ticker,
                price=Decimal("100"),
                sl_price=Decimal("95"),
                tp_price=Decimal("110"),
                atr=Decimal("2"),
                reason="test-entry",
            )
        ]


class _FakeDataFeed:
    scenario = "tp"

    def __init__(self, *_args, **_kwargs):
        pass

    async def get_candles(self, *_args, **_kwargs):
        now = pd.Timestamp.now("UTC")
        return pd.DataFrame(
            [
                {"time": now, "open": 100, "high": 101, "low": 99, "close": 100, "volume": 1000}
                for _ in range(25)
            ]
        )

    async def stream_candles(self, _figis, _tf_main):
        yield {
            "figi": "FIGI1",
            "time": "2026-01-01T10:00:00Z",
            "open": 100,
            "high": 101,
            "low": 99,
            "close": 100,
            "volume": 1000,
            "is_complete": True,
        }
        second_high = 111 if self.scenario == "tp" else 101
        second_low = 99 if self.scenario == "tp" else 94
        yield {
            "figi": "FIGI1",
            "time": "2026-01-01T10:15:00Z",
            "open": 100,
            "high": second_high,
            "low": second_low,
            "close": 109,
            "volume": 1000,
            "is_complete": True,
        }
        raise asyncio.CancelledError()


def _install_fake_modules(monkeypatch):
    cfg_mod = types.ModuleType("config.settings")
    cfg_mod.TINKOFF_API_TOKEN = "token"
    cfg_mod.TINKOFF_SANDBOX = True
    cfg_mod.TELEGRAM_BOT_TOKEN = "tg"
    cfg_mod.TELEGRAM_CHAT_ID = "chat"
    monkeypatch.setitem(sys.modules, "config.settings", cfg_mod)

    data_mod = types.ModuleType("data.data_feed")
    data_mod.DataFeed = _FakeDataFeed
    monkeypatch.setitem(sys.modules, "data.data_feed", data_mod)

    momentum_mod = types.ModuleType("strategy.momentum")
    momentum_mod.MomentumStrategy = _FakeStrategy
    momentum_mod.compute_global_trend = lambda *_args, **_kwargs: None
    monkeypatch.setitem(sys.modules, "strategy.momentum", momentum_mod)

    risk_mod = types.ModuleType("risk.risk_manager")
    risk_mod.RiskManager = _FakeRiskManager
    risk_mod.RiskViolation = Exception
    monkeypatch.setitem(sys.modules, "risk.risk_manager", risk_mod)

    broker_mod = types.ModuleType("broker.broker_paper")
    broker_mod.PaperBrokerClient = _FakePaperBroker
    monkeypatch.setitem(sys.modules, "broker.broker_paper", broker_mod)

    notif_mod = types.ModuleType("notifications.notifier_telegram")
    notif_mod.TelegramNotifier = _FakeNotifier
    monkeypatch.setitem(sys.modules, "notifications.notifier_telegram", notif_mod)


@pytest.mark.asyncio
async def test_run_paper_closes_trade_on_tp(monkeypatch):
    _install_fake_modules(monkeypatch)
    _FakeDataFeed.scenario = "tp"

    saved_logs = []
    monkeypatch.setattr(runner, "_save_paper_trades", lambda log: saved_logs.append(log.copy()))

    cfg = {
        "timeframe": "15m",
        "strategy": {"global_trend": {"enabled": False}},
        "risk": {"capital_rub": 1000},
        "instruments": [{"ticker": "S1M6", "figi": "FIGI1", "lot": 1, "enabled": True}],
    }

    await runner.run_paper(cfg)

    assert _FakeNotifier.opened, "Ожидался вход в позицию"
    assert _FakeNotifier.closed, "Ожидалось закрытие позиции"
    assert _FakeNotifier.closed[0]["reason"] == "tp"
    assert _FakeNotifier.closed[0]["mode"] == "paper"
    assert saved_logs and len(saved_logs[0]) >= 2


@pytest.mark.asyncio
async def test_run_paper_closes_trade_on_sl(monkeypatch):
    _install_fake_modules(monkeypatch)
    _FakeDataFeed.scenario = "sl"

    saved_logs = []
    monkeypatch.setattr(runner, "_save_paper_trades", lambda log: saved_logs.append(log.copy()))

    cfg = {
        "timeframe": "15m",
        "strategy": {"global_trend": {"enabled": False}},
        "risk": {"capital_rub": 1000},
        "instruments": [{"ticker": "S1M6", "figi": "FIGI1", "lot": 1, "enabled": True}],
    }

    await runner.run_paper(cfg)

    assert _FakeNotifier.opened, "Ожидался вход в позицию"
    assert _FakeNotifier.closed, "Ожидалось закрытие позиции"
    assert _FakeNotifier.closed[0]["reason"] == "sl"
    assert _FakeNotifier.closed[0]["mode"] == "paper"
    assert saved_logs and len(saved_logs[0]) >= 2
