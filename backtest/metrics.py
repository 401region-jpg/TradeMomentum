"""
backtest/metrics.py
Чистые функции для расчёта метрик. Нет I/O, нет API.
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def sharpe_ratio(returns: pd.Series, annual_factor: float = 252) -> float:
    if returns.std() == 0:
        return 0.0
    return float(returns.mean() / returns.std() * np.sqrt(annual_factor))


def sortino_ratio(returns: pd.Series, annual_factor: float = 252) -> float:
    downside = returns[returns < 0]
    if len(downside) == 0 or downside.std() == 0:
        return 0.0
    return float(returns.mean() / downside.std() * np.sqrt(annual_factor))


def max_drawdown(equity: pd.Series) -> tuple[float, float]:
    rolling_max = equity.cummax()
    drawdown = equity - rolling_max
    dd_pct = drawdown / rolling_max
    return float(drawdown.min()), float(dd_pct.min())


def profit_factor(trades: pd.DataFrame) -> float:
    wins = trades[trades["pnl"] > 0]["pnl"].sum()
    losses = trades[trades["pnl"] < 0]["pnl"].sum()
    if losses == 0:
        return float("inf")
    return float(wins / abs(losses))


def hit_rate(trades: pd.DataFrame) -> float:
    if len(trades) == 0:
        return 0.0
    return float((trades["pnl"] > 0).sum() / len(trades))


def avg_win_loss_ratio(trades: pd.DataFrame) -> float:
    wins = trades[trades["pnl"] > 0]["pnl"]
    losses = trades[trades["pnl"] < 0]["pnl"]
    if len(wins) == 0 or len(losses) == 0:
        return 0.0
    return float(wins.mean() / abs(losses.mean()))


def total_return_pct(equity: pd.Series) -> float:
    if equity.iloc[0] == 0:
        return 0.0
    return float((equity.iloc[-1] / equity.iloc[0] - 1) * 100)


def annualized_return_pct(equity: pd.Series, trading_days: int = 252) -> float:
    n = len(equity)
    if n < 2 or equity.iloc[0] == 0:
        return 0.0
    total = equity.iloc[-1] / equity.iloc[0]
    years = n / trading_days
    return float((total ** (1 / years) - 1) * 100) if years > 0 else 0.0


def build_summary(
    trades: pd.DataFrame,
    equity: pd.Series,
    initial_capital: float,
) -> dict:
    if len(trades) == 0:
        return {"error": "Нет сделок в периоде"}

    daily_returns = equity.pct_change().dropna()
    dd_abs, dd_pct = max_drawdown(equity)

    return {
        "total_trades":          len(trades),
        "win_trades":            int((trades["pnl"] > 0).sum()),
        "loss_trades":           int((trades["pnl"] < 0).sum()),
        "hit_rate_pct":          round(hit_rate(trades) * 100, 1),
        "total_pnl":             round(trades["pnl"].sum(), 2),
        "total_return_pct":      round(total_return_pct(equity), 2),
        "annualized_return_pct": round(annualized_return_pct(equity), 2),
        "max_drawdown_abs":      round(dd_abs, 2),
        "max_drawdown_pct":      round(dd_pct * 100, 2),
        "sharpe_ratio":          round(sharpe_ratio(daily_returns), 3),
        "sortino_ratio":         round(sortino_ratio(daily_returns), 3),
        "profit_factor":         round(profit_factor(trades), 3),
        "avg_win_loss_ratio":    round(avg_win_loss_ratio(trades), 3),
        "avg_pnl_per_trade":     round(trades["pnl"].mean(), 2),
        "best_trade":            round(trades["pnl"].max(), 2),
        "worst_trade":           round(trades["pnl"].min(), 2),
        "initial_capital":       initial_capital,
        "final_capital":         round(equity.iloc[-1], 2),
    }
