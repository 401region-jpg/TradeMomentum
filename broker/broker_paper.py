"""
broker/broker_paper.py
Paper-trading брокер: симулирует исполнение ордеров на реальных котировках
без отправки каких-либо реальных заявок.

Используется в режиме MODE=paper.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, UTC
from decimal import Decimal
from typing import Optional

from broker.base import BrokerClient, Order, OrderDirection, OrderStatus, Position

logger = logging.getLogger(__name__)


class PaperBrokerClient(BrokerClient):
    """
    Симулирует брокера в памяти.
    - Исполняет ордера мгновенно по переданной цене (рыночное заполнение).
    - Хранит позиции и баланс внутри объекта.
    - Не делает НИ ОДНОГО запроса к T-Invest API для отправки ордеров.
    """

    def __init__(self, initial_balance: Decimal):
        self._balance: Decimal = initial_balance
        self._positions: dict[str, Position] = {}  # ticker → Position
        self._orders: dict[str, Order] = {}
        self._trade_log: list[dict] = []  # для CSV-экспорта

    # ── Интерфейс BrokerClient ────────────────────────────────────────────────

    async def get_balance(self) -> Decimal:
        return self._balance

    async def get_positions(self) -> list[Position]:
        return list(self._positions.values())

    async def place_market_order(
        self,
        instrument_uid: str,
        ticker: str,
        direction: OrderDirection,
        quantity: int,
        execution_price: Optional[Decimal] = None,  # подаётся из paper-runner
    ) -> Order:
        if execution_price is None:
            raise ValueError("PaperBroker требует execution_price для симуляции")
        if quantity <= 0:
            raise ValueError("quantity должен быть > 0")

        order_uid = str(uuid.uuid4())
        now = datetime.now(UTC)
        signed_qty = quantity if direction == OrderDirection.BUY else -quantity

        if ticker in self._positions:
            pos = self._positions[ticker]
            old_qty = pos.quantity
            new_qty = old_qty + signed_qty

            # Увеличиваем существующую позицию в ту же сторону
            if old_qty * signed_qty > 0:
                total_abs_qty = abs(old_qty) + abs(signed_qty)
                weighted_avg = (
                    pos.avg_price * abs(old_qty) + execution_price * abs(signed_qty)
                ) / Decimal(total_abs_qty)
                self._apply_cashflow(direction, execution_price, quantity)
                pos.quantity = new_qty
                pos.avg_price = weighted_avg
                pos.current_price = execution_price

            # Полное закрытие позиции
            elif new_qty == 0:
                close_direction = OrderDirection.SELL if old_qty > 0 else OrderDirection.BUY
                self._apply_cashflow(close_direction, execution_price, abs(old_qty))
                pnl = (execution_price - pos.avg_price) * Decimal(old_qty)
                self._log_trade(
                    event="close",
                    ticker=ticker,
                    pos=pos,
                    exec_price=execution_price,
                    pnl=pnl,
                    ts=now,
                    trade_qty=abs(old_qty),  # Закрыли весь старый объём
                )
                del self._positions[ticker]
                logger.info(
                    "[PAPER] CLOSE %s | qty=%d | entry=%.4f exit=%.4f | PnL=%.2f",
                    ticker,
                    old_qty,
                    float(pos.avg_price),
                    float(execution_price),
                    float(pnl),
                )

            # Частичное закрытие позиции
            else:
                closing_qty = min(abs(old_qty), abs(signed_qty))
                close_direction = OrderDirection.SELL if old_qty > 0 else OrderDirection.BUY
                self._apply_cashflow(close_direction, execution_price, closing_qty)
                pnl = (execution_price - pos.avg_price) * Decimal(
                    closing_qty if old_qty > 0 else -closing_qty
                )
                pos.quantity = new_qty
                pos.current_price = execution_price
                self._log_trade(
                    event="partial_close",
                    ticker=ticker,
                    pos=pos,
                    exec_price=execution_price,
                    pnl=pnl,
                    ts=now,
                    trade_qty=closing_qty,  # В журнал — именно закрытая часть
                )
                logger.info(
                    "[PAPER] PARTIAL CLOSE %s | old=%d new=%d | exit=%.4f | PnL=%.2f",
                    ticker,
                    old_qty,
                    new_qty,
                    float(execution_price),
                    float(pnl),
                )

        # Открытие новой позиции
        else:
            self._apply_cashflow(direction, execution_price, quantity)
            pos = Position(
                instrument_uid=instrument_uid,
                ticker=ticker,
                quantity=signed_qty,
                avg_price=execution_price,
                current_price=execution_price,
                opened_at=now,
            )
            self._positions[ticker] = pos
            self._log_trade(
                event="open",
                ticker=ticker,
                pos=pos,
                exec_price=execution_price,
                pnl=Decimal("0"),
                ts=now,
                trade_qty=quantity,  # Открыли quantity
            )
            logger.info(
                "[PAPER] OPEN %s %s | qty=%d | price=%.4f | balance=%.2f",
                direction.value.upper(),
                ticker,
                quantity,
                float(execution_price),
                float(self._balance),
            )

        order = Order(
            uid=order_uid,
            instrument_uid=instrument_uid,
            ticker=ticker,
            direction=direction,
            quantity=quantity,
            price=None,
            status=OrderStatus.FILLED,
            filled_price=execution_price,
            created_at=now,
            filled_at=now,
        )
        self._orders[order_uid] = order
        return order

    def _apply_cashflow(self, direction: OrderDirection, price: Decimal, quantity: int) -> None:
        amount = price * Decimal(quantity)
        if direction == OrderDirection.BUY:
            self._balance -= amount
        else:
            self._balance += amount

    async def cancel_order(self, order_uid: str) -> bool:
        if order_uid in self._orders:
            self._orders[order_uid].status = OrderStatus.CANCELLED
            return True
        return False

    async def sync_positions(self) -> None:
        # В paper-режиме позиции уже в памяти — просто логируем
        logger.info(
            "[PAPER] Sync: %d открытых позиций | баланс=%.2f",
            len(self._positions),
            float(self._balance),
        )

    # ── Paper-специфичные методы ──────────────────────────────────────────────

    def update_prices(self, prices: dict[str, Decimal]) -> None:
        """Обновляет текущие цены позиций (вызывается из paper-runner на каждой свече)."""
        for ticker, price in prices.items():
            if ticker in self._positions:
                self._positions[ticker].current_price = price

    def get_total_equity(self) -> Decimal:
        """Баланс + нереализованный PnL по всем позициям."""
        unrealized = sum(p.unrealized_pnl for p in self._positions.values())
        return self._balance + unrealized

    def get_trade_log(self) -> list[dict]:
        return self._trade_log

    def _log_trade(
        self,
        event: str,
        ticker: str,
        pos: Position,
        exec_price: Decimal,
        pnl: Decimal,
        ts: datetime,
        trade_qty: int,
    ) -> None:
        self._trade_log.append(
            {
                "event": event,
                "ticker": ticker,
                "direction": pos.direction.value,
                # qty = ОБЪЁМ СДЕЛКИ, а не текущий остаток позиции
                "qty": trade_qty,
                "entry_price": float(pos.avg_price),
                "exec_price": float(exec_price),
                "pnl": float(pnl),
                "opened_at": pos.opened_at.isoformat(),
                "timestamp": ts.isoformat(),
                "duration_min": (ts - pos.opened_at).seconds // 60,
            }
        )