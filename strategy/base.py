# strategy/base.py
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import List, Tuple

import pandas as pd


# === СТАРЫЙ СВЕЧНОЙ ИНТЕРФЕЙС (momentum-стратегия) ===

class SignalType(Enum):
    LONG = "long"
    SHORT = "short"
    CLOSE_LONG = "close_long"
    CLOSE_SHORT = "close_short"
    NONE = "none"


@dataclass
class Signal:
    type: SignalType
    ticker: str
    price: Decimal
    sl_price: Decimal
    tp_price: Decimal
    atr: Decimal
    reason: str = ""

    @property
    def is_entry(self) -> bool:
        return self.type in (SignalType.LONG, SignalType.SHORT)

    @property
    def is_exit(self) -> bool:
        return self.type in (SignalType.CLOSE_LONG, SignalType.CLOSE_SHORT)


class Strategy(ABC):
    """Базовый интерфейс свечных стратегий."""

    @abstractmethod
    def name(self) -> str:
        ...

    @abstractmethod
    def add_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        ...

    @abstractmethod
    def generate_signals(self, df: pd.DataFrame, ticker: str) -> list[Signal]:
        ...


# === НОВЫЙ ИНТЕРФЕЙС ДЛЯ MARKET MAKER СТРАТЕГИЙ ===

class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


@dataclass
class LimitOrderRequest:
    side: Side
    price: float
    qty: int
    ttl_seconds: int = 30
    comment: str = ""


@dataclass
class OrderBookState:
    best_bid: float
    best_ask: float
    mid_price: float
    spread_pct: float
    bids: List[Tuple[float, float]]  # [(price, qty), ...]
    asks: List[Tuple[float, float]]
    timestamp: float


@dataclass
class PositionState:
    net_qty: int          # + лонг, - шорт, 0 = нет
    avg_entry: float
    unrealized_pnl: float
    active_buy_orders: int
    active_sell_orders: int


class MarketMakerStrategy(ABC):
    """Новый интерфейс — стаканный (market maker)."""

    @abstractmethod
    def generate_orders(
        self,
        ob: OrderBookState,
        pos: PositionState,
        trend_bias: int,       # +1 / 0 / -1
    ) -> List[LimitOrderRequest]:
        """Возвращает список желаемых лимитных ордеров."""
        raise NotImplementedError

    @abstractmethod
    def on_fill(self, side: Side, price: float, qty: int) -> None:
        """Callback при исполнении ордера (обновление статистики)."""
        raise NotImplementedError