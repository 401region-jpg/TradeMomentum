"""
backtest/backtester.py
Движок бэктеста, адаптированный под интрадей (1 день, ~61 свеча 15m).

Изменения:
  - MIN_CANDLES = 20 (было 60 — не работало на 1 дне)
  - Логирует статистику по часам МСК (видно какие часы работают)
  - SL/TP проверяются по high/low внутри свечи
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal

import pandas as pd

from backtest.metrics import build_summary
from risk.risk_manager import RiskManager
from strategy.base import SignalType, Strategy

logger = logging.getLogger(__name__)

MIN_CANDLES = 20  # минимум для 1 торгового дня на 15m


@dataclass
class BacktestTrade:
    ticker: str
    direction: str
    entry_idx: int
    entry_time: pd.Timestamp
    entry_price: float
    sl: float
    tp: float
    qty: int
    lot_size: int
    exit_idx: int | None = None
    exit_time: pd.Timestamp | None = None
    exit_price: float | None = None
    exit_reason: str | None = None  # "sl" | "tp" | "eod"
    pnl: float = 0.0
    commission: float = 0.0


@dataclass
class BacktestResult:
    trades: list[BacktestTrade]
    equity: pd.Series
    summary: dict
    hourly_stats: dict = field(default_factory=dict)

    def print_summary(self) -> None:
        print("\n" + "=" * 55)
        print("  BACKTEST RESULTS")
        print("=" * 55)
        for k, v in self.summary.items():
            print(f"  {k:<30}: {v}")
        print("=" * 55)
        if self.hourly_stats:
            print("\n  Статистика по часам МСК:")
            for h in sorted(self.hourly_stats):
                s = self.hourly_stats[h]
                n = s["w"] + s["l"]
                wr = s["w"] / n * 100 if n > 0 else 0
                print(f"  {h:02d}:xx → {wr:.0f}% winrate ({n} сделок)")
        print("=" * 55)


class Backtester:

    def __init__(
        self,
        strategy: Strategy,
        risk_manager: RiskManager,
        commission_pct: float = 0.0004,
    ):
        self._strategy = strategy
        self._risk = risk_manager
        self._commission = commission_pct

    def run(
        self,
        df: pd.DataFrame,
        ticker: str,
        lot_size: int = 1,
        initial_capital: float = 1000.0,
    ) -> BacktestResult:

        if len(df) < MIN_CANDLES:
            raise ValueError(f"Мало данных: {len(df)} свечей (нужно ≥ {MIN_CANDLES})")

        df = self._strategy.add_indicators(df.copy())

        capital = float(initial_capital)
        equity_curve = [capital]
        trades: list[BacktestTrade] = []
        open_trade: BacktestTrade | None = None
        hourly: dict = defaultdict(lambda: {"w": 0, "l": 0})

        # Минимальная начальная история для расчёта индикаторов
        warmup = max(
            getattr(self._strategy, "_ema_slow", 50),
            getattr(self._strategy, "_atr_period", 14) * 2,
            getattr(self._strategy, "_vol_period", 20),
        )
        start_i = min(warmup, len(df) - 2)

        for i in range(start_i, len(df)):
            row = df.iloc[i]

            # ── Проверяем SL/TP для открытой позиции ──────────────────────
            if open_trade is not None:
                closed = self._check_sl_tp(open_trade, row, i)
                if closed:
                    capital = self._close_trade(
                        open_trade, closed["price"], closed["reason"], capital
                    )
                    # Статистика по часам МСК
                    try:
                        msk_h = (open_trade.entry_time.hour + 3) % 24
                        if closed["reason"] == "tp":
                            hourly[msk_h]["w"] += 1
                        else:
                            hourly[msk_h]["l"] += 1
                    except Exception:
                        pass
                    trades.append(open_trade)
                    open_trade = None

            # ── Генерируем сигналы ─────────────────────────────────────────
            if open_trade is None:
                signals = self._strategy.generate_signals(df.iloc[: i + 1], ticker)
                for sig in signals:
                    if sig.type not in (SignalType.LONG, SignalType.SHORT):
                        continue
                    sl_dist = abs(sig.price - sig.sl_price)
                    qty = self._risk.calculate_quantity(
                        price=sig.price,
                        lot_size=lot_size,
                        sl_distance=sl_dist,
                    )
                    if qty <= 0:
                        continue

                    open_trade = BacktestTrade(
                        ticker=ticker,
                        direction="long" if sig.type == SignalType.LONG else "short",
                        entry_idx=i,
                        entry_time=row["time"],
                        entry_price=float(sig.price),
                        sl=float(sig.sl_price),
                        tp=float(sig.tp_price),
                        qty=qty,
                        lot_size=lot_size,
                    )
                    logger.debug(
                        "[BT] OPEN %s %s @ %.4f sl=%.4f tp=%.4f qty=%d",
                        ticker,
                        open_trade.direction,
                        open_trade.entry_price,
                        open_trade.sl,
                        open_trade.tp,
                        qty,
                    )
                    break  # 1 позиция за раз

            equity_curve.append(capital)

        # Закрываем незакрытую позицию в конце периода
        if open_trade is not None:
            last_close = float(df.iloc[-1]["close"])
            capital = self._close_trade(open_trade, last_close, "eod", capital)
            trades.append(open_trade)

        equity = pd.Series(equity_curve, name="equity")

        trades_df = (
            pd.DataFrame(
                [
                    {
                        "pnl": t.pnl,
                        "direction": t.direction,
                        "ticker": t.ticker,
                        "entry_price": t.entry_price,
                        "exit_price": t.exit_price,
                        "exit_reason": t.exit_reason,
                    }
                    for t in trades
                ]
            )
            if trades
            else pd.DataFrame(
                columns=["pnl", "direction", "ticker", "entry_price", "exit_price", "exit_reason"]
            )
        )

        summary = build_summary(trades_df, equity, initial_capital)

        return BacktestResult(
            trades=trades,
            equity=equity,
            summary=summary,
            hourly_stats=dict(hourly),
        )

    # ── Вспомогательные ───────────────────────────────────────────────────────
    def _check_sl_tp(
        self,
        trade: BacktestTrade,
        row: pd.Series,
        idx: int,
    ) -> dict | None:
        if trade.direction == "long":
            if row["low"] <= trade.sl:
                return {"price": trade.sl, "reason": "sl"}
            if row["high"] >= trade.tp:
                return {"price": trade.tp, "reason": "tp"}
        else:
            if row["high"] >= trade.sl:
                return {"price": trade.sl, "reason": "sl"}
            if row["low"] <= trade.tp:
                return {"price": trade.tp, "reason": "tp"}
        return None

    def _close_trade(
        self,
        trade: BacktestTrade,
        exit_price: float,
        reason: str,
        capital: float,
    ) -> float:
        trade.exit_price = exit_price
        trade.exit_reason = reason

        if trade.direction == "long":
            raw_pnl = (exit_price - trade.entry_price) * trade.qty * trade.lot_size
        else:
            raw_pnl = (trade.entry_price - exit_price) * trade.qty * trade.lot_size

        commission = (
            (trade.entry_price + exit_price) * trade.qty * trade.lot_size * self._commission
        )
        trade.pnl = raw_pnl - commission
        trade.commission = commission

        self._risk.record_pnl(Decimal(str(trade.pnl)))

        rr = abs(raw_pnl / ((trade.sl - trade.entry_price) * trade.qty * trade.lot_size + 0.0001))
        logger.info(
            "[BT] CLOSE %s %s @ %.4f | pnl=%.2f | reason=%s | R=%.1f",
            trade.ticker,
            trade.direction,
            exit_price,
            trade.pnl,
            reason,
            rr,
        )
        return capital + trade.pnl
