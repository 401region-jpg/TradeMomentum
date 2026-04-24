"""
broker/base.py
Абстрактный интерфейс брокера.
"""
from __future__ import annotations

import datetime
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from typing import Optional


class OrderDirection(Enum):
    BUY = "buy"
    SELL = "sell"

    def opposite(self) -> "OrderDirection":
        return OrderDirection.SELL if self == OrderDirection.BUY else OrderDirection.BUY


class OrderStatus(Enum):
    PENDING = "pending"
    FILLED = "filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"


@dataclass
class Position:
    instrument_uid: str
    ticker: str
    quantity: int
    avg_price: Decimal
    current_price: Decimal
    opened_at: datetime.datetime = field(default_factory=datetime.datetime.utcnow)
    sl_price: Optional[Decimal] = None
    tp_price: Optional[Decimal] = None

    @property
    def is_long(self) -> bool:
        return self.quantity > 0

    @property
    def is_short(self) -> bool:
        return self.quantity < 0

    @property
    def unrealized_pnl(self) -> Decimal:
        return (self.current_price - self.avg_price) * self.quantity

    @property
    def direction(self) -> OrderDirection:
        return OrderDirection.BUY if self.quantity > 0 else OrderDirection.SELL


@dataclass
class Order:
    uid: str
    instrument_uid: str
    ticker: str
    direction: OrderDirection
    quantity: int
    price: Optional[Decimal]
    status: OrderStatus
    filled_price: Optional[Decimal] = None
    created_at: datetime.datetime = field(default_factory=datetime.datetime.utcnow)
    filled_at: Optional[datetime.datetime] = None
    error_message: Optional[str] = None


class BrokerClient(ABC):

    @abstractmethod
    async def get_balance(self) -> Decimal:
        ...

    @abstractmethod
    async def get_positions(self) -> list[Position]:
        ...

    @abstractmethod
    async def place_market_order(
        self,
        instrument_uid: str,
        ticker: str,
        direction: OrderDirection,
        quantity: int,
    ) -> Order:
        ...

    @abstractmethod
    async def cancel_order(self, order_uid: str) -> bool:
        ...

    @abstractmethod
    async def sync_positions(self) -> None:
        ...
