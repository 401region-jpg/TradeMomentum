# === существующий код оставляем как есть ===
class Strategy(ABC):

    @abstractmethod
    def name(self) -> str:
        ...

    @abstractmethod
    def generate_signals(self, df: pd.DataFrame, ticker: str) -> list[Signal]:
        ...

    @abstractmethod
    def add_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        ...


# === НОВЫЙ БЛОК ДЛЯ MARKET MAKER СТРАТЕГИЙ ===
from dataclasses import dataclass
from typing import List, Tuple, Optional
from enum import Enum as _Enum


class Side(str, _Enum):
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
    net_qty: int          # +лонг, -шорт, 0 = нет
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