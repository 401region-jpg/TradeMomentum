"""
strategy/multi_signal.py

MultiSignalStrategy: EMA Crossover + RSI-фильтр + ATR-based SL/TP.

Логика:
  LONG:
    - EMA(fast) пересекла EMA(slow) ВВЕРХ на предыдущей свече
    - RSI(14) находится в зоне [rsi_long_min, rsi_long_max] (не перекуплен)
    - ATR(14) > min_atr_pct * close (не во флэте)

  SHORT:
    - EMA(fast) пересекла EMA(slow) ВНИЗ на предыдущей свече
    - RSI(14) находится в зоне [rsi_short_min, rsi_short_max] (не перепродан)
    - ATR(14) > min_atr_pct * close

  Стоп-лосс:  вход ± atr_sl_multiplier * ATR
  Тейк-профит: вход ± atr_tp_multiplier * ATR  (R:R = tp_mult/sl_mult)

Полностью детерминирован и тестируем — нет I/O, нет API-вызовов.
"""
from __future__ import annotations

import logging
from decimal import Decimal

import numpy as np
import pandas as pd

from strategy.base import Signal, SignalType, Strategy

logger = logging.getLogger(__name__)


# ── Чистые функции-индикаторы (numpy, без внешних зависимостей) ───────────────

def _ema(series: pd.Series, span: int) -> pd.Series:
    return series.ewm(span=span, adjust=False).mean()


def _rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def _atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    high, low, prev_close = df["high"], df["low"], df["close"].shift(1)
    tr = pd.concat(
        [high - low, (high - prev_close).abs(), (low - prev_close).abs()],
        axis=1,
    ).max(axis=1)
    return tr.ewm(alpha=1 / period, adjust=False).mean()


# ── Основная стратегия ────────────────────────────────────────────────────────

class MultiSignalStrategy(Strategy):
    """
    EMA-кросс + RSI-фильтр + ATR-стопы.
    Параметры берутся из config/params.yaml через словарь cfg.
    """

    def __init__(self, cfg: dict):
        s = cfg["strategy"]
        self._ema_fast: int = s["ema_fast"]
        self._ema_slow: int = s["ema_slow"]
        self._rsi_period: int = s["rsi_period"]
        self._rsi_long_max: float = s["rsi_long_max"]
        self._rsi_long_min: float = s["rsi_long_min"]
        self._rsi_short_min: float = s["rsi_short_min"]
        self._rsi_short_max: float = s["rsi_short_max"]
        self._atr_period: int = s["atr_period"]
        self._atr_sl: float = s["atr_sl_multiplier"]
        self._atr_tp: float = s["atr_tp_multiplier"]
        self._min_atr_pct: float = s["min_atr_pct"]
        self._allow_shorts: bool = cfg["risk"]["allow_shorts"]

    def name(self) -> str:
        return f"MultiSignal(EMA{self._ema_fast}/{self._ema_slow}+RSI{self._rsi_period}+ATR)"

    def add_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        """Добавляет все нужные колонки индикаторов. Возвращает новый DataFrame."""
        df = df.copy()
        df["ema_fast"] = _ema(df["close"], self._ema_fast)
        df["ema_slow"] = _ema(df["close"], self._ema_slow)
        df["rsi"] = _rsi(df["close"], self._rsi_period)
        df["atr"] = _atr(df, self._atr_period)
        # Пересечения EMA
        df["ema_cross_up"] = (
            (df["ema_fast"] > df["ema_slow"]) &
            (df["ema_fast"].shift(1) <= df["ema_slow"].shift(1))
        )
        df["ema_cross_down"] = (
            (df["ema_fast"] < df["ema_slow"]) &
            (df["ema_fast"].shift(1) >= df["ema_slow"].shift(1))
        )
        return df

    def generate_signals(self, df: pd.DataFrame, ticker: str) -> list[Signal]:
        """
        Генерирует сигналы по последним N строкам DataFrame.
        Смотрим на предпоследнюю строку (-2) для сигнала,
        последняя строка (-1) — текущая незакрытая свеча.
        """
        if len(df) < self._ema_slow + 5:
            return []  # мало данных для расчёта индикаторов

        df = self.add_indicators(df)

        # Берём предпоследнюю завершённую свечу для сигнала
        row = df.iloc[-2]
        current_price = Decimal(str(df.iloc[-1]["close"]))
        atr = Decimal(str(row["atr"]))
        rsi = row["rsi"]
        close = Decimal(str(row["close"]))

        signals: list[Signal] = []

        # ── Фильтр флэта ──────────────────────────────────────────────────────
        if float(atr) < float(close) * self._min_atr_pct:
            logger.debug("[%s] Флэт (ATR=%.4f < %.4f%% цены) — нет сигнала", ticker, float(atr), self._min_atr_pct*100)
            return []

        # ── Проверка NaN ──────────────────────────────────────────────────────
        import math
        if any(math.isnan(x) for x in [rsi, row["ema_fast"], row["ema_slow"]]):
            return []

        # ── LONG сигнал ───────────────────────────────────────────────────────
        if (
            row["ema_cross_up"] and
            self._rsi_long_min <= rsi <= self._rsi_long_max
        ):
            sl = current_price - atr * Decimal(str(self._atr_sl))
            tp = current_price + atr * Decimal(str(self._atr_tp))
            signals.append(Signal(
                type=SignalType.LONG,
                ticker=ticker,
                price=current_price,
                sl_price=sl,
                tp_price=tp,
                atr=atr,
                reason=f"EMA{self._ema_fast} cross↑ EMA{self._ema_slow} | RSI={rsi:.1f}",
            ))
            logger.info("[%s] LONG сигнал | price=%.4f sl=%.4f tp=%.4f | %s",
                        ticker, float(current_price), float(sl), float(tp), signals[-1].reason)

        # ── SHORT сигнал ──────────────────────────────────────────────────────
        elif (
            row["ema_cross_down"] and
            self._allow_shorts and
            self._rsi_short_min <= rsi <= self._rsi_short_max
        ):
            sl = current_price + atr * Decimal(str(self._atr_sl))
            tp = current_price - atr * Decimal(str(self._atr_tp))
            signals.append(Signal(
                type=SignalType.SHORT,
                ticker=ticker,
                price=current_price,
                sl_price=sl,
                tp_price=tp,
                atr=atr,
                reason=f"EMA{self._ema_fast} cross↓ EMA{self._ema_slow} | RSI={rsi:.1f}",
            ))
            logger.info("[%s] SHORT сигнал | price=%.4f sl=%.4f tp=%.4f | %s",
                        ticker, float(current_price), float(sl), float(tp), signals[-1].reason)

        return signals
