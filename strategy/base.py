# strategy/base.py
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import List, Optional
from enum import Enum


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
    bids: List[tuple]  # [(price, qty), ...]
    asks: List[tuple]
    timestamp: float


@dataclass
class PositionState:
    net_qty: int          # + лонг, - шорт, 0 = нет
    avg_entry: float
    unrealized_pnl: float
    active_buy_orders: int
    active_sell_orders: int


class Strategy(ABC):
    """Старый интерфейс — свечной (momentum)."""

    @abstractmethod
    def generate_signals(self, candles) -> dict:
        raise NotImplementedError

    @abstractmethod
    def add_indicators(self, df):
        raise NotImplementedError


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
