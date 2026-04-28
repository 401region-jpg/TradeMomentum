# broker/broker_tinkoff.py
"""
Адаптер T-Invest через REST API.
Не требует tinkoff-invest-api — использует httpx.
LIVE заблокирован без --confirm-live.

Добавлены методы для market-maker режима:
- place_limit_order
- get_active_orders
- get_order_book
- get_position_qty
"""

from __future__ import annotations

import logging
import uuid
import time
from decimal import Decimal
from typing import Optional, List

import httpx

from broker.base import (
    BrokerClient,
    Order,
    OrderDirection,
    OrderStatus,
    Position,
    ActiveOrder,
)

logger = logging.getLogger(__name__)

BASE_URL = "https://invest-public-api.tinkoff.ru/rest"


def _q(v: dict) -> Decimal:
    units = int(v.get("units", 0))
    nano = int(v.get("nano", 0))
    return Decimal(units) + Decimal(nano) / Decimal("1000000000")


class LiveModeBlockedError(RuntimeError):
    pass


class TinkoffBrokerClient(BrokerClient):

    def __init__(
        self,
        token: str,
        account_id: str,
        sandbox: bool = True,
        live_confirmed: bool = False,
    ):
        self._token = token
        self._account_id = account_id
        self._sandbox = sandbox
        self._live_confirmed = live_confirmed
        self._headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "accept": "application/json",
        }

    # ── Защита live ───────────────────────────────────────────────────────────

    def _guard_live(self) -> None:
        if not self._sandbox and not self._live_confirmed:
            raise LiveModeBlockedError(
                "\n" + "=" * 60 + "\n"
                "ОБНАРУЖЕН LIVE РЕЖИМ. ОСТАНОВИЛСЯ.\n"
                "Запустите: python runner.py --mode live --confirm-live\n"
                + "=" * 60
            )

    async def _post(self, service: str, method: str, body: dict) -> dict:
        """
        Универсальный POST к T-Invest REST.
        Для sandbox-ордеров использует SandboxService.
        """
        svc = "SandboxService" if (self._sandbox and service != "OrdersService") else service
        url = f"{BASE_URL}/tinkoff.public.invest.api.contract.v1.{svc}/{method}"
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(url, json=body, headers=self._headers)
            if resp.status_code != 200:
                raise RuntimeError(
                    f"API {svc}/{method} → {resp.status_code}: {resp.text[:300]}"
                )
            return resp.json()

    # ── BrokerClient interface (существующие методы) ─────────────────────────

    async def get_balance(self) -> Decimal:
        """
        Сумма валютных остатков (totalAmountCurrencies).
        Для оценки общего equity лучше использовать get_total_equity().
        """
        if self._sandbox:
            data = await self._post(
                "SandboxService",
                "GetSandboxPortfolio",
                {"accountId": self._account_id},
            )
        else:
            data = await self._post(
                "OperationsService",
                "GetPortfolio",
                {"accountId": self._account_id},
            )
        val = data.get("totalAmountCurrencies", {})
        return _q(val) if isinstance(val, dict) else Decimal("0")

    async def get_total_equity(self) -> Decimal:
        """
        Общая стоимость портфеля (equity) по счёту в рублях.
        Берётся из поля totalAmountPortfolio метода GetPortfolio / GetSandboxPortfolio.
        """
        if self._sandbox:
            data = await self._post(
                "SandboxService",
                "GetSandboxPortfolio",
                {"accountId": self._account_id},
            )
        else:
            data = await self._post(
                "OperationsService",
                "GetPortfolio",
                {"accountId": self._account_id},
            )

        val = data.get("totalAmountPortfolio")
        if isinstance(val, dict):
            return _q(val)
        return Decimal("0")

    async def get_positions(self) -> list[Position]:
        if self._sandbox:
            data = await self._post(
                "SandboxService",
                "GetSandboxPositions",
                {"accountId": self._account_id},
            )
        else:
            data = await self._post(
                "OperationsService",
                "GetPositions",
                {"accountId": self._account_id},
            )
        positions: list[Position] = []
        for f in data.get("futures", []):
            qty = int(f.get("balance", 0))
            positions.append(
                Position(
                    instrument_uid=f.get("instrumentUid", ""),
                    ticker=f.get("instrumentUid", ""),
                    quantity=qty,
                    avg_price=Decimal("0"),
                    current_price=Decimal("0"),
                )
            )
        return positions

    async def place_market_order(
        self,
        instrument_uid: str,
        ticker: str,
        direction: OrderDirection,
        quantity: int,
    ) -> Order:
        self._guard_live()

        order_id = str(uuid.uuid4())
        tink_dir = (
            "ORDER_DIRECTION_BUY"
            if direction == OrderDirection.BUY
            else "ORDER_DIRECTION_SELL"
        )

        logger.info(
            "ORDER (MKT) → %s %s x%d | sandbox=%s",
            ticker,
            direction.value,
            quantity,
            self._sandbox,
        )

        body = {
            "accountId": self._account_id,
            "instrumentId": instrument_uid,
            "quantity": str(quantity),
            "direction": tink_dir,
            "orderType": "ORDER_TYPE_MARKET",
            "orderId": order_id,
        }

        method = "PostSandboxOrder" if self._sandbox else "PostOrder"
        service = "SandboxService" if self._sandbox else "OrdersService"

        try:
            resp = await self._post(service, method, body)
        except Exception as e:
            logger.error("Ошибка ордера: %s", e)
            return Order(
                uid=order_id,
                instrument_uid=instrument_uid,
                ticker=ticker,
                direction=direction,
                quantity=quantity,
                price=None,
                status=OrderStatus.REJECTED,
                error_message=str(e),
            )

        filled_price: Optional[Decimal] = None
        ep = resp.get("executedOrderPrice")
        if ep:
            filled_price = _q(ep)

        status_str = resp.get("executionReportStatus", "")
        status = OrderStatus.FILLED if "FILL" in status_str else OrderStatus.PENDING

        return Order(
            uid=resp.get("orderId", order_id),
            instrument_uid=instrument_uid,
            ticker=ticker,
            direction=direction,
            quantity=quantity,
            price=None,
            status=status,
            filled_price=filled_price,
        )

    async def cancel_order(self, order_uid: str) -> bool:
        try:
            await self._post(
                "OrdersService",
                "CancelOrder",
                {
                    "accountId": self._account_id,
                    "orderId": order_uid,
                },
            )
            return True
        except Exception as e:
            logger.error("Ошибка отмены ордера %s: %s", order_uid, e)
            return False

    async def sync_positions(self) -> None:
        positions = await self.get_positions()
        logger.info("Sync: %d открытых позиций", len(positions))

    # ── ГО по фьючерсу ────────────────────────────────────────────────────────

    async def get_futures_margin(self, instrument_uid: str) -> Decimal:
        """
        Возвращает размер гарантийного обеспечения (initialMarginOnBuy)
        за 1 фьючерсный контракт по instrument_uid.

        Использует InstrumentsService/GetFuturesMargin Tinkoff Invest API.
        Работает только для боевого контура (в песочнице ГО часто фиктивное).
        """
        body = {"instrumentId": instrument_uid}

        try:
            data = await self._post(
                "InstrumentsService",
                "GetFuturesMargin",
                body,
            )
        except Exception as e:
            logger.error(
                "Не удалось получить ГО через InstrumentsService/GetFuturesMargin "
                "для %s: %s",
                instrument_uid,
                e,
            )
            return Decimal("0")

        mv = data.get("initialMarginOnBuy")
        if isinstance(mv, dict):
            margin = _q(mv)
            logger.debug(
                "Futures margin (initialMarginOnBuy) для %s: %.2f ₽",
                instrument_uid,
                float(margin),
            )
            return margin

        logger.warning(
            "InstrumentsService/GetFuturesMargin вернул неожиданный ответ "
            "для %s: initialMarginOnBuy=%r",
            instrument_uid,
            mv,
        )
        return Decimal("0")

    # ── ДОПОЛНЕНИЯ ДЛЯ MARKET-MAKER РЕЖИМА ───────────────────────────────────

    async def place_limit_order(
        self,
        instrument_uid: str,
        ticker: str,
        direction: OrderDirection,
        quantity: int,
        price: Decimal,
    ) -> Order:
        """
        Лимитный ордер T-Invest (PostOrder / PostSandboxOrder с ORDER_TYPE_LIMIT).
        """
        self._guard_live()

        order_id = f"mm_{int(time.time() * 1000)}"
        tink_dir = (
            "ORDER_DIRECTION_BUY"
            if direction == OrderDirection.BUY
            else "ORDER_DIRECTION_SELL"
        )

        logger.info(
            "ORDER (LMT) → %s %s x%d @ %.2f | sandbox=%s",
            ticker,
            direction.value,
            quantity,
            float(price),
            self._sandbox,
        )

        body = {
            "accountId": self._account_id,
            "instrumentId": instrument_uid,
            "quantity": str(quantity),
            "direction": tink_dir,
            "price": {
                "units": int(price),
                "nano": int((price - int(price)) * Decimal("1000000000")),
            },
            "orderType": "ORDER_TYPE_LIMIT",
            "orderId": order_id,
        }

        method = "PostSandboxOrder" if self._sandbox else "PostOrder"
        service = "SandboxService" if self._sandbox else "OrdersService"

        try:
            resp = await self._post(service, method, body)
        except Exception as e:
            logger.error("Ошибка лимитного ордера: %s", e)
            return Order(
                uid=order_id,
                instrument_uid=instrument_uid,
                ticker=ticker,
                direction=direction,
                quantity=quantity,
                price=price,
                status=OrderStatus.REJECTED,
                error_message=str(e),
            )

        status_str = resp.get("executionReportStatus", "")
        status = OrderStatus.FILLED if "FILL" in status_str else OrderStatus.PENDING

        filled_price: Optional[Decimal] = None
        ep = resp.get("executedOrderPrice")
        if ep:
            filled_price = _q(ep)

        return Order(
            uid=resp.get("orderId", order_id),
            instrument_uid=instrument_uid,
            ticker=ticker,
            direction=direction,
            quantity=quantity,
            price=price,
            status=status,
            filled_price=filled_price,
        )

    async def get_active_orders(self, instrument_uid: str) -> List[ActiveOrder]:
        """
        Возвращает активные (не исполненные/не отменённые) ордера по инструменту.
        """
        body = {"accountId": self._account_id}
        try:
            data = await self._post("OrdersService", "GetOrders", body)
        except Exception as e:
            logger.error("GetOrders error: %s", e)
            return []

        result: List[ActiveOrder] = []
        for o in data.get("orders", []):
            if o.get("instrumentId") != instrument_uid:
                continue
            # executionReportStatus: EXECUTION_REPORT_STATUS_FILL, NEW, PARTIALLYFILL ...
            status = o.get("executionReportStatus", "")
            if "CANCEL" in status or "REJECT" in status or "FILL" in status:
                continue

            dir_str = o.get("direction")
            if dir_str == "ORDER_DIRECTION_BUY":
                side = OrderDirection.BUY
            else:
                side = OrderDirection.SELL

            price_raw = o.get("initialSecurityPrice")
            price = _q(price_raw) if isinstance(price_raw, dict) else Decimal("0")
            qty = int(o.get("lotsRequested", 0))

            dt_raw = o.get("orderDate")
            # orderDate в REST возвращается как строка RFC3339, можно не парсить для TTL
            created_at = time.time()

            result.append(
                ActiveOrder(
                    uid=o.get("orderId", ""),
                    side=side,
                    price=price,
                    quantity=qty,
                    created_at=datetime.datetime.utcfromtimestamp(created_at),
                )
            )
        return result

    async def get_order_book(self, instrument_uid: str, depth: int = 20) -> dict:
        """
        Возвращает стакан для инструмента.
        {"bids": [(price: Decimal, qty: int)], "asks": [...]}.
        """
        body = {"instrumentId": instrument_uid, "depth": depth}
        try:
            data = await self._post("MarketDataService", "GetOrderBook", body)
        except Exception as e:
            logger.error("GetOrderBook error: %s", e)
            return {"bids": [], "asks": []}

        bids = []
        for b in data.get("bids", []):
            price = _q(b.get("price", {}))
            qty = int(b.get("quantity", 0))
            bids.append((price, qty))

        asks = []
        for a in data.get("asks", []):
            price = _q(a.get("price", {}))
            qty = int(a.get("quantity", 0))
            asks.append((price, qty))

        return {"bids": bids, "asks": asks}

    async def get_position_qty(self, instrument_uid: str) -> int:
        """
        Возвращает нетто-позицию по фьючерсу (qty в лотах, знак направления).
        """
        positions = await self.get_positions()
        for p in positions:
            if p.instrument_uid == instrument_uid:
                return int(p.quantity)
        return 0