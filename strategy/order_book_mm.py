# strategy/order_book_mm.py
import logging
import time
from typing import List

from .base import (
    MarketMakerStrategy, LimitOrderRequest, OrderBookState,
    PositionState, Side
)

logger = logging.getLogger(__name__)


class OrderBookMMStrategy(MarketMakerStrategy):
    """
    Market-maker стратегия по BTCUSDperpA.

    Выставляет симметричные лимитки вокруг mid-price,
    смещая котировки под inventory и глобальный тренд.
    """

    def __init__(self, params: dict):
        mm = params.get("market_maker", {})
        self.target_profit_pct    = mm.get("target_profit_pct", 0.08)
        self.min_spread_pct       = mm.get("min_spread_pct", 0.03)
        self.refresh_threshold    = mm.get("quote_refresh_threshold_pct", 0.02)
        self.max_active_orders    = mm.get("max_active_orders", 4)
        self.order_ttl            = mm.get("order_ttl_seconds", 30)
        self.inventory_skew_rate  = mm.get("inventory_skew_per_lot", 0.005)
        self.max_inventory        = mm.get("max_inventory_lots", 5)
        self.trend_skew_pct       = mm.get("trend_skew_pct", 0.015)
        self.price_step           = mm.get("price_step", 1.0)  # шаг цены BTCUSDperpA

        # статистика (обновляется через on_fill)
        self._fills: list = []
        self._last_buy_fill: float = 0.0
        self._last_sell_fill: float = 0.0

    # ------------------------------------------------------------------
    def generate_orders(
        self,
        ob: OrderBookState,
        pos: PositionState,
        trend_bias: int = 0,
    ) -> List[LimitOrderRequest]:

        # 1. Проверяем минимальный спред
        if ob.spread_pct < self.min_spread_pct:
            logger.debug(
                "spread=%.4f%% < min=%.4f%% → skip",
                ob.spread_pct, self.min_spread_pct
            )
            return []

        # 2. Считаем смещения
        base_half = self.target_profit_pct / 2.0
        inv_skew  = pos.net_qty * self.inventory_skew_rate
        tr_skew   = trend_bias  * self.trend_skew_pct

        buy_offset_pct  = base_half + inv_skew - tr_skew
        sell_offset_pct = base_half - inv_skew + tr_skew

        # Защита: ордер не должен быть "внутри" спреда или перевёрнутым
        buy_offset_pct  = max(buy_offset_pct,  0.01)
        sell_offset_pct = max(sell_offset_pct, 0.01)

        # 3. Считаем цены
        buy_price  = self._round(ob.mid_price * (1 - buy_offset_pct / 100))
        sell_price = self._round(ob.mid_price * (1 + sell_offset_pct / 100))

        # Санити: buy < best_bid, sell > best_ask
        buy_price  = min(buy_price,  ob.best_bid)
        sell_price = max(sell_price, ob.best_ask)

        orders: List[LimitOrderRequest] = []

        # 4. Только покупаем, если не перелимит лонга
        if pos.net_qty < self.max_inventory:
            orders.append(LimitOrderRequest(
                side=Side.BUY,
                price=buy_price,
                qty=1,
                ttl_seconds=self.order_ttl,
                comment=f"MM bid inv={pos.net_qty} tr={trend_bias}"
            ))

        # 5. Только продаём, если не перелимит шорта
        if pos.net_qty > -self.max_inventory:
            orders.append(LimitOrderRequest(
                side=Side.SELL,
                price=sell_price,
                qty=1,
                ttl_seconds=self.order_ttl,
                comment=f"MM ask inv={pos.net_qty} tr={trend_bias}"
            ))

        logger.debug(
            "MM orders: buy=%.2f sell=%.2f mid=%.2f inv=%d tr=%d",
            buy_price, sell_price, ob.mid_price, pos.net_qty, trend_bias
        )
        return orders

    def on_fill(self, side: Side, price: float, qty: int) -> None:
        ts = time.time()
        self._fills.append({"side": side, "price": price, "qty": qty, "ts": ts})
        if side == Side.BUY:
            self._last_buy_fill = price
        else:
            self._last_sell_fill = price
        logger.info("FILL %s %.2f x%d", side, price, qty)

    # ------------------------------------------------------------------
    def _round(self, price: float) -> float:
        """Округляет до шага цены инструмента."""
        return round(round(price / self.price_step) * self.price_step, 8)

    def should_refresh(self, current_price: float, order_price: float) -> bool:
        """True, если ордер нужно перевыставить (цена ушла)."""
        if order_price == 0:
            return True
        drift_pct = abs(current_price - order_price) / order_price * 100
        return drift_pct > self.refresh_threshold

    @property
    def stats(self) -> dict:
        if not self._fills:
            return {"fills": 0}
        buys  = [f for f in self._fills if f["side"] == Side.BUY]
        sells = [f for f in self._fills if f["side"] == Side.SELL]
        return {
            "fills": len(self._fills),
            "buy_fills": len(buys),
            "sell_fills": len(sells),
            "avg_buy":  sum(f["price"] for f in buys)  / max(len(buys), 1),
            "avg_sell": sum(f["price"] for f in sells) / max(len(sells), 1),
        }
```
