"""
strategy/momentum.py  v9 — Trend Follower 15m

КЛЮЧЕВЫЕ ВЫВОДЫ ИЗ ДАННЫХ:
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Январь ШОРТЫ: hit=31%, avg_win=71₽, avg_loss=-22₽ → R:R=3.24 → PF=1.48 ✅
Март  ЛОНГИ:  hit=27%, avg_win=31₽, avg_loss=-16₽ → R:R=1.94 → PF=0.73 ❌

Проблема марта — НЕ направление, а маленький TP:
  avg_win=31₽ при аналогичных позициях что давали 71₽ в январе
  = бот выходил из прибыльных сделок слишком рано

Решение v9:
  1. DIRECTION LOCK: определяем тренд недели (EMA20/50 на 4h),
     торгуем ТОЛЬКО в его направлении весь период
  2. TP = 3.0 ATR (было 2.25) → при 31% hit rate PF > 1
  3. SL = 0.9 ATR (без изменений)
  4. Реальный R:R = 3.33 → break-even всего 23% hit rate
  5. trailing_exit: если цена прошла 1.5 ATR — переносим SL в безубыток
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""

from __future__ import annotations

import logging
import math
from datetime import time
from decimal import Decimal
from enum import Enum
from typing import Optional

import numpy as np
import pandas as pd
import pytz

from strategy.base import Signal, SignalType, Strategy

logger = logging.getLogger(__name__)
MSK = pytz.timezone("Europe/Moscow")


class GlobalTrend(Enum):
    BULL = "BULL"
    BEAR = "BEAR"
    FLAT = "FLAT"


def _ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False).mean()


def _rsi(s: pd.Series, p: int = 14) -> pd.Series:
    d = s.diff()
    ag = d.clip(lower=0).ewm(alpha=1 / p, adjust=False).mean()
    al = (-d.clip(upper=0)).ewm(alpha=1 / p, adjust=False).mean()
    return 100 - 100 / (1 + ag / al.replace(0, np.nan))


def _atr(df: pd.DataFrame, p: int = 14) -> pd.Series:
    high, low, prev_close = df["high"], df["low"], df["close"].shift(1)
    tr = pd.concat(
        [
            (high - low).rename("hl"),
            (high - prev_close).abs().rename("hc"),
            (low - prev_close).abs().rename("lc"),
        ],
        axis=1,
    ).max(axis=1)
    return tr.ewm(alpha=1 / p, adjust=False).mean()


def _parse_t(s: str) -> time:
    h, m = s.split(":")
    return time(int(h), int(m))


def compute_global_trend(
    df: pd.DataFrame,
    ema_fast: int = 20,
    ema_slow: int = 50,
    flat_gap_pct: float = 0.003,
) -> GlobalTrend:
    if df is None or len(df) < ema_slow + 2:
        return GlobalTrend.FLAT
    fast = _ema(df["close"], ema_fast).iloc[-1]
    slow = _ema(df["close"], ema_slow).iloc[-1]
    if slow == 0 or math.isnan(fast) or math.isnan(slow):
        return GlobalTrend.FLAT
    if abs(fast - slow) / slow < flat_gap_pct:
        return GlobalTrend.FLAT
    return GlobalTrend.BULL if fast > slow else GlobalTrend.BEAR


class MomentumStrategy(Strategy):
    """
    v9: Trend Follower 15m
    - Торгует ТОЛЬКО в направлении старшего тренда (4h/1h)
    - TP = 3.0 ATR → R:R = 3.33 → break-even = 23% hit rate
    - Триггер: импульсная свеча в направлении 15m EMA20/50
    - Direction Lock: если глобальный BEAR → запрещены все лонги (и наоборот)
    """

    def __init__(self, cfg: dict):
        s = cfg["strategy"]
        r = cfg["risk"]
        ses = cfg.get("session", {})

        self._ema_fast = s["ema_fast"]
        self._ema_slow = s["ema_slow"]
        self._rsi_period = s["rsi_period"]
        self._rsi_long_min = s["rsi_long_min"]
        self._rsi_long_max = s["rsi_long_max"]
        self._rsi_short_min = s["rsi_short_min"]
        self._rsi_short_max = s["rsi_short_max"]
        self._atr_period = s["atr_period"]
        self._atr_sl = s["atr_sl_multiplier"]
        self._atr_tp = s["atr_tp_multiplier"]  # 3.0 → R:R=3.33
        self._min_body_pct = s.get("min_body_pct", 0.15)
        self._min_atr_pct = s.get("min_atr_pct", 0.0008)
        self._allow_shorts = r["allow_shorts"]
        self._vol_period = s.get("volume_ma_period", 20)
        self._vol_spike = s.get("volume_spike_mult", 1.0)
        self._atr_exp = s.get("atr_expansion_mult", 1.0)

        # Торговые окна
        self._trade_windows = s.get("trade_only_in_windows", False)
        self._main_open = _parse_t(ses.get("main_open", "09:00"))
        self._main_close = _parse_t(ses.get("main_close", "18:50"))
        self._eve_open = _parse_t(ses.get("evening_open", "19:05"))
        self._eve_close = _parse_t(ses.get("evening_close", "23:50"))
        self._use_evening = ses.get("use_evening_session", True)
        self._eve_boost = _parse_t(s.get("evening_boost_start", "16:00"))

        # Allowed hours
        ah = s.get("allowed_hours", {})
        default = ah.get("default", None)
        self._default_hours: Optional[set] = set(default) if default else None
        self._allowed_hours: dict[str, Optional[set]] = {
            k: set(v) for k, v in ah.items() if k != "default"
        }

        # Глобальный тренд (DIRECTION LOCK)
        gt = s.get("global_trend", {})
        self._gt_enabled = gt.get("enabled", False)
        self._gt_flat_gap = gt.get("flat_gap_pct", 0.003)
        self._gt_strict_lock = gt.get("strict_lock", True)  # True = жёсткая блокировка
        self._global_trend = GlobalTrend.FLAT

        # Trailing exit порог
        self._trailing_trigger = s.get("trailing_trigger_atr", 1.5)

    def set_global_trend(self, trend: GlobalTrend, ticker: str = "") -> None:
        if trend != self._global_trend:
            logger.info(
                "🌐 [%s] Тренд: %s → %s | direction_lock=%s",
                ticker,
                self._global_trend.value,
                trend.value,
                "ON" if self._gt_strict_lock else "OFF",
            )
        self._global_trend = trend

    def get_global_trend(self) -> GlobalTrend:
        return self._global_trend

    def name(self) -> str:
        rr = self._atr_tp / self._atr_sl
        be = 1 / (1 + rr)
        lock = "LOCK" if self._gt_strict_lock else "SOFT"
        return (
            f"TrendFollower_v9_15m("
            f"EMA{self._ema_fast}/{self._ema_slow}"
            f"|TP{self._atr_tp}×ATR"
            f"|RR{rr:.2f}"
            f"|BE{be*100:.0f}%"
            f"|{lock})"
        )

    def add_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()
        df["ema_fast"] = _ema(df["close"], self._ema_fast)
        df["ema_slow"] = _ema(df["close"], self._ema_slow)
        df["rsi"] = _rsi(df["close"], self._rsi_period)
        df["atr"] = _atr(df, self._atr_period)
        df["atr_ma"] = df["atr"].rolling(self._atr_period * 2).mean()
        df["vol_ma"] = df["volume"].rolling(self._vol_period).mean()
        df["body"] = (df["close"] - df["open"]).abs()
        df["is_bull"] = df["close"] > df["open"]
        df["is_bear"] = df["close"] < df["open"]
        df["trend_up"] = df["ema_fast"] > df["ema_slow"]
        df["trend_down"] = df["ema_fast"] < df["ema_slow"]
        return df

    def generate_signals(self, df: pd.DataFrame, ticker: str) -> list[Signal]:
        min_rows = max(self._ema_slow, self._atr_period * 2, self._vol_period) + 3
        if len(df) < min_rows:
            return []

        df = self.add_indicators(df)

        trading_ok, eve_boost, msk_hour = self._session_check(df)
        if not trading_ok:
            return []

        if msk_hour is not None:
            allowed = self._allowed_hours.get(ticker, self._default_hours)
            if allowed is not None and msk_hour not in allowed:
                return []

        row = df.iloc[-1]
        price = Decimal(str(row["close"]))
        atr = Decimal(str(row["atr"]))
        rsi = row["rsi"]

        for v in [rsi, row["ema_fast"], row["ema_slow"], row["atr"]]:
            if math.isnan(float(v)):
                return []

        if float(atr) / float(price) < self._min_atr_pct:
            return []

        if self._vol_spike > 1.0 and float(row["vol_ma"]) > 0:
            if float(row["volume"]) < float(row["vol_ma"]) * self._vol_spike:
                return []

        if self._atr_exp > 1.0 and float(row["atr_ma"]) > 0:
            if float(row["atr"]) < float(row["atr_ma"]) * self._atr_exp:
                return []

        body_ok = float(row["body"]) >= float(atr) * self._min_body_pct

        # ── DIRECTION LOCK: строгий тренд-фильтр ─────────────
        # Если глобальный BEAR → НИКАКИХ лонгов
        # Если глобальный BULL → НИКАКИХ шортов
        # Если FLAT → торгуем по локальному 15m тренду
        allow_long = True
        allow_short = self._allow_shorts

        if self._gt_enabled:
            gt = self._global_trend
            if gt == GlobalTrend.BEAR and self._gt_strict_lock:
                allow_long = False
                logger.debug("[%s] 🔒 BEAR LOCK: лонги запрещены", ticker)
            elif gt == GlobalTrend.BULL and self._gt_strict_lock:
                allow_short = False
                logger.debug("[%s] 🔒 BULL LOCK: шорты запрещены", ticker)
            elif gt == GlobalTrend.FLAT:
                logger.debug("[%s] FLAT: оба направления", ticker)

        signals: list[Signal] = []
        rr = self._atr_tp / self._atr_sl
        boost = " 🌙" if eve_boost else ""
        gt_tag = f"[{self._global_trend.value}]" if self._gt_enabled else ""
        h_tag = f"h{msk_hour:02d}" if msk_hour is not None else ""
        tp_pct = float(atr) * self._atr_tp / float(price) * 100

        # ── LONG ──────────────────────────────────────────────
        if (
            allow_long
            and row["trend_up"]
            and float(row["close"]) > float(row["ema_fast"])
            and row["is_bull"]
            and body_ok
            and self._rsi_long_min <= rsi <= self._rsi_long_max
        ):
            sl = price - atr * Decimal(str(self._atr_sl))
            tp = price + atr * Decimal(str(self._atr_tp))
            reason = (
                f"LONG↑{boost} {gt_tag}{h_tag}МСК"
                f" EMA{self._ema_fast}>{self._ema_slow}"
                f" body={float(row['body'])/float(atr)*100:.0f}%ATR"
                f" RSI={rsi:.0f} TP≈{tp_pct:.2f}% RR={rr:.2f}"
            )
            signals.append(
                Signal(
                    type=SignalType.LONG,
                    ticker=ticker,
                    price=price,
                    sl_price=sl,
                    tp_price=tp,
                    atr=atr,
                    reason=reason,
                )
            )
            logger.info(
                "[%s] ✅ %s | %.4f sl=%.4f tp=%.4f",
                ticker,
                reason,
                float(price),
                float(sl),
                float(tp),
            )

        # ── SHORT ─────────────────────────────────────────────
        elif (
            allow_short
            and row["trend_down"]
            and float(row["close"]) < float(row["ema_fast"])
            and row["is_bear"]
            and body_ok
            and self._rsi_short_min <= rsi <= self._rsi_short_max
        ):
            sl = price + atr * Decimal(str(self._atr_sl))
            tp = price - atr * Decimal(str(self._atr_tp))
            reason = (
                f"SHORT↓{boost} {gt_tag}{h_tag}МСК"
                f" EMA{self._ema_fast}<{self._ema_slow}"
                f" body={float(row['body'])/float(atr)*100:.0f}%ATR"
                f" RSI={rsi:.0f} TP≈{tp_pct:.2f}% RR={rr:.2f}"
            )
            signals.append(
                Signal(
                    type=SignalType.SHORT,
                    ticker=ticker,
                    price=price,
                    sl_price=sl,
                    tp_price=tp,
                    atr=atr,
                    reason=reason,
                )
            )
            logger.info(
                "[%s] ✅ %s | %.4f sl=%.4f tp=%.4f",
                ticker,
                reason,
                float(price),
                float(sl),
                float(tp),
            )

        return signals

    def _session_check(self, df: pd.DataFrame) -> tuple[bool, bool, Optional[int]]:
        if not self._trade_windows:
            return True, False, None
        try:
            t_raw = df.iloc[-1].get("time")
            if t_raw is None:
                return True, False, None
            ts = pd.Timestamp(t_raw)
            if ts.tzinfo is None:
                ts = ts.tz_localize("UTC")
            msk = ts.tz_convert(MSK)
            t = msk.time()
            msk_hour = msk.hour
        except Exception:
            return True, False, None

        in_main = self._main_open <= t <= self._main_close
        in_evening = self._use_evening and self._eve_open <= t <= self._eve_close
        boost = t >= self._eve_boost
        return (in_main or in_evening), boost, msk_hour
