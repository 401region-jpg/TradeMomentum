"""
strategy/base.py
Абстрактный интерфейс стратегии.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum

import pandas as pd


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

    @abstractmethod
    def name(self) -> str:
        ...

    @abstractmethod
    def generate_signals(self, df: pd.DataFrame, ticker: str) -> list[Signal]:
        ...

    @abstractmethod
    def add_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        ...
