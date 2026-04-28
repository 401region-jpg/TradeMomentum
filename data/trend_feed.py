# data/trend_feed.py
"""
Фоновый модуль для получения глобального тренда BTC
через публичный Binance REST API (без ключей).
Обновляется каждые N минут в отдельном потоке.
"""
import logging
import threading
import time
from typing import Optional

import requests
import pandas as pd

logger = logging.getLogger(__name__)

BINANCE_KLINES = "https://api.binance.com/api/v3/klines"


def _fetch_klines(symbol: str = "BTCUSDT", interval: str = "1h", limit: int = 100) -> pd.DataFrame:
    resp = requests.get(
        BINANCE_KLINES,
        params={"symbol": symbol, "interval": interval, "limit": limit},
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()
    df = pd.DataFrame(data, columns=[
        "open_time", "open", "high", "low", "close", "volume",
        "close_time", "qav", "num_trades", "tbbav", "tbqav", "ignore"
    ])
    df["close"] = df["close"].astype(float)
    return df


def _calc_trend(df: pd.DataFrame, fast: int = 20, slow: int = 50) -> int:
    """
    Возвращает:  +1 = uptrend, -1 = downtrend, 0 = flat
    """
    ema_fast = df["close"].ewm(span=fast, adjust=False).mean()
    ema_slow = df["close"].ewm(span=slow, adjust=False).mean()
    last_fast = ema_fast.iloc[-1]
    last_slow = ema_slow.iloc[-1]
    prev_fast = ema_fast.iloc[-2]
    prev_slow = ema_slow.iloc[-2]

    # Тренд: EMA fast выше slow → long; ниже → short
    margin = 0.0005  # 0.05% — зона "flat"
    diff_pct = (last_fast - last_slow) / last_slow

    if diff_pct > margin:
        return +1
    elif diff_pct < -margin:
        return -1
    return 0


class TrendFeed:
    """
    Потокобезопасный провайдер глобального тренда.

    Использование:
        feed = TrendFeed(update_interval_minutes=5)
        feed.start()
        bias = feed.trend_bias   # +1 / 0 / -1
    """

    def __init__(
        self,
        symbol: str = "BTCUSDT",
        interval: str = "1h",
        ema_fast: int = 20,
        ema_slow: int = 50,
        update_interval_minutes: int = 5,
    ):
        self.symbol = symbol
        self.interval = interval
        self.ema_fast = ema_fast
        self.ema_slow = ema_slow
        self.update_interval = update_interval_minutes * 60

        self._bias: int = 0
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

    @property
    def trend_bias(self) -> int:
        with self._lock:
            return self._bias

    def start(self):
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="TrendFeed")
        self._thread.start()
        logger.info("TrendFeed started (symbol=%s, interval=%s)", self.symbol, self.interval)

    def stop(self):
        self._stop_event.set()

    def _loop(self):
        while not self._stop_event.is_set():
            try:
                df = _fetch_klines(self.symbol, self.interval, limit=100)
                bias = _calc_trend(df, self.ema_fast, self.ema_slow)
                with self._lock:
                    self._bias = bias
                logger.debug("TrendFeed updated: bias=%d", bias)
            except Exception as exc:
                logger.warning("TrendFeed error: %s", exc)

            self._stop_event.wait(timeout=self.update_interval)