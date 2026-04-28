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

# broker/base.py (дополнение к существующим методам)
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import List, Optional


@dataclass
class ActiveOrder:
    order_id: str
    side: str          # "BUY" / "SELL"
    price: float
    qty: int
    created_at: float  # timestamp


class BrokerClient(ABC):
    # --- существующие методы ---
    @abstractmethod
    def place_order(self, figi: str, side: str, qty: int, order_type: str) -> str:
        """Рыночный ордер. Возвращает order_id."""
        raise NotImplementedError

    @abstractmethod
    def get_positions(self) -> list:
        raise NotImplementedError

    # --- НОВЫЕ методы для MM ---
    @abstractmethod
    def place_limit_order(self, figi: str, side: str, qty: int, price: float) -> str:
        """
        Выставляет лимитный ордер.
        Возвращает order_id (строка).
        """
        raise NotImplementedError

    @abstractmethod
    def cancel_order(self, order_id: str) -> bool:
        """
        Отменяет ордер по ID.
        Возвращает True при успехе.
        """
        raise NotImplementedError

    @abstractmethod
    def get_active_orders(self, figi: str) -> List[ActiveOrder]:
        """
        Возвращает список активных ордеров по инструменту.
        """
        raise NotImplementedError

    @abstractmethod
    def get_order_book(self, figi: str, depth: int = 20) -> dict:
        """
        Возвращает стакан: {"bids": [(price, qty)], "asks": [(price, qty)]}
        """
        raise NotImplementedError

    @abstractmethod
    def get_position_qty(self, figi: str) -> int:
        """
        Возвращает текущую нетто-позицию (+ лонг, - шорт, 0 = нет).
        """
        raise NotImplementedError
