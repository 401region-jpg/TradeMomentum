"""
runner_gui.py — упрощённый раннер под GUI (paper + live)

Использует те же модули, что runner.py, но:
- добавляет online-лог trades/live_log.csv;
- гарантированно и часто обновляет state/bot_state.json для GUI;
- восстанавливает состояние позиций при рестарте (с SL/TP через live_positions.json);
- добавляет детальное логирование открытий/закрытий для отладки SL/TP;
- добавляет метрики (PnL%, дистанция) в state;
- OrderTracker: ждёт реального исполнения, учитывает частичные заполнения;
- Cooldown на сигналы — защита от спама ордерами;
- SL/TP = 0 guard — восстановленные без уровней позиции не закрываются ложно;
- Reconciliation с брокером при старте — сверяет persist-файл с реальным счётом.

АРХИТЕКТУРА:
  run_paper()   — бэктест на живом потоке (MomentumStrategy)
  run_live()    — LIVE исполнение (MomentumStrategy)
  run_mm_live() — LIVE Market Making (OrderBookMMStrategy) — отдельная модель,
                  НЕ смешивается с Momentum. Запускается отдельно.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import logging
import logging.handlers
import os
import random
import sys
import time
from strategy.order_book_mm import OrderBookMMStrategy
from strategy.base import OrderBookState, PositionState
from data.trend_feed import TrendFeed
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Optional
from risk.contracts import get_futures_contract_by_ticker, FuturesContractSpec

import psutil
import yaml

logger = logging.getLogger(__name__)
FIGI_BTCUSD_PERP = "BTCUSDPERP00"

# ── State / PID для GUI ──────────────────────────────────────────────────────

STATE_DIR = Path("state")
STATE_DIR.mkdir(parents=True, exist_ok=True)

STATE_PATH = STATE_DIR / "bot_state.json"
STOP_FLAG_PATH = STATE_DIR / "stop.flag"
# Файл хранения позиций с SL/TP между перезапусками
POSITIONS_PERSIST_PATH = STATE_DIR / "live_positions.json"

# заглушка для универсального пути (если будешь использовать в других модулях)
BOT_STATE_PATH = STATE_PATH

# Cooldown между сигналами на один тикер (сек) — защита от спама ордерами
SIGNAL_COOLDOWN_SEC = 60


def read_bot_state() -> dict:
    """Читает state/bot_state.json (если есть)."""
    try:
        if STATE_PATH.exists():
            with open(STATE_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception as e:
        logger.warning("Не удалось прочитать bot_state.json: %s", e)
    return {}


def write_bot_state(state: dict) -> None:
    """Пишет произвольный state для GUI (equity, позиции, и т.п.)."""
    try:
        with open(STATE_PATH, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error("Не удалось записать state: %s", e)


def write_pid_state(pid: int, mode: str, status: str = "running") -> None:
    """
    Пишет PID раннера + метаданные.

    Используется app.py, чтобы не запускать второй экземпляр,
    если PID из bot_state.json ещё жив.
    """
    state = read_bot_state()
    state.update(
        {
            "runner_pid": pid,
            "mode": mode,
            "started_at": datetime.now(tz=timezone.utc).isoformat(),
            "status": status,
        }
    )
    write_bot_state(state)


def clear_bot_state() -> None:
    """Очищает bot_state.json (используем при штатной остановке)."""
    try:
        if STATE_PATH.exists():
            STATE_PATH.unlink()
    except Exception as e:
        logger.warning("Не удалось удалить bot_state.json: %s", e)


def is_pid_running(pid: int) -> bool:
    """Проверка, жив ли процесс с таким PID (через psutil)."""
    try:
        p = psutil.Process(pid)
        return p.is_running() and p.status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return False
    except Exception as e:
        logger.warning("Ошибка при проверке PID %s: %s", pid, e)
        return False


# ── Persistence позиций с SL/TP (выживает рестарт) ───────────────────────────

def save_live_positions(positions: dict) -> None:
    """Сохраняет live_positions (с SL/TP) в JSON для восстановления после рестарта."""
    try:
        data = {
            ticker: {
                **pos,
                "entry_price": str(pos["entry_price"]),
                "sl": str(pos["sl"]),
                "tp": str(pos["tp"]),
            }
            for ticker, pos in positions.items()
        }
        POSITIONS_PERSIST_PATH.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    except Exception as e:
        logger.warning("Не удалось сохранить позиции в %s: %s", POSITIONS_PERSIST_PATH, e)


def load_live_positions() -> dict:
    """Загружает ранее сохранённые позиции (с SL/TP) при рестарте."""
    try:
        if POSITIONS_PERSIST_PATH.exists():
            raw = json.loads(POSITIONS_PERSIST_PATH.read_text(encoding="utf-8"))
            return {
                ticker: {
                    **pos,
                    "entry_price": Decimal(pos["entry_price"]),
                    "sl": Decimal(pos["sl"]),
                    "tp": Decimal(pos["tp"]),
                }
                for ticker, pos in raw.items()
            }
    except Exception as e:
        logger.warning("Не удалось загрузить позиции из %s: %s", POSITIONS_PERSIST_PATH, e)
    return {}


# ── OrderTracker — отслеживание статуса ордера после выставления ─────────────

class OrderTracker:
    """
    Опрашивает брокера после place_market_order до получения итогового статуса.
    Решает проблему fire-and-forget: мы знаем реальный filled_price и filled_qty.
    """

    def __init__(self, broker, poll_interval: float = 0.5, max_polls: int = 30):
        self._broker = broker
        self._poll_interval = poll_interval
        self._max_polls = max_polls

    async def wait_fill(self, order_id: str):
        """
        Ждёт исполнения ордера. Возвращает объект статуса или None при таймауте.
        Брокер должен поддерживать get_order_state(order_id).
        """
        if not hasattr(self._broker, "get_order_state"):
            return None  # брокер не поддерживает — работаем как раньше

        TERMINAL = {"filled", "rejected", "cancelled", "expired"}
        for attempt in range(self._max_polls):
            await asyncio.sleep(self._poll_interval)
            try:
                state = await self._broker.get_order_state(order_id)
                status = getattr(state, "status", None)
                if status is not None and str(status).lower() in TERMINAL:
                    logger.debug(
                        "OrderTracker: %s → статус=%s после %d попыток",
                        order_id, status, attempt + 1,
                    )
                    return state
            except Exception as e:
                logger.warning("OrderTracker: ошибка опроса %s (попытка %d): %s", order_id, attempt + 1, e)

        logger.warning(
            "OrderTracker: таймаут ожидания ордера %s (%d попыток × %.1fс)",
            order_id, self._max_polls, self._poll_interval,
        )
        return None


# ── Signal cooldown — защита от дублирующихся сигналов ───────────────────────

_signal_last_time: dict[str, datetime] = {}


def _cooldown_ok(ticker: str, now: datetime, cooldown_sec: int = SIGNAL_COOLDOWN_SEC) -> bool:
    """Возвращает True, если с последнего сигнала прошло достаточно времени."""
    last = _signal_last_time.get(ticker)
    if last is not None and (now - last).total_seconds() < cooldown_sec:
        return False
    _signal_last_time[ticker] = now
    return True


# ── Универсальный helper для PnL по фьючам ───────────────────────────────────

def futures_pnl_rub(
    entry_price: Decimal,
    exit_price: Decimal,
    qty: int,
    min_price_increment: Decimal,
    min_price_increment_amount: Decimal,
    side: str,
    ticker: str | None = None,
) -> Decimal:
    """
    PnL в рублях по фьючам Tinkoff.

    Для большинства фьючей (Si, BR, S1M6 и т.п.) считаем через шаг и стоимость шага.
    Для BTCUSDperpA используем прямое движение цены * qty, т.к. контракт нефондовый.
    """
    if qty == 0:
        return Decimal("0")

    side_norm = (side or "").lower()

    # Спец-кейс для BTCUSDperpA: считаем напрямую
    if ticker == "BTCUSDperpA":
        raw_move = exit_price - entry_price
        if side_norm in ("sell", "short"):
            raw_move = -raw_move
        # предполагаем, что 1 контракт = 1 "единица" цены, PnL уже в рублях
        return raw_move * Decimal(qty)

    # Стандартный расчёт через шаг и стоимость шага
    raw_move = exit_price - entry_price

    # для шорта движение цены инвертируем
    if side_norm in ("sell", "short"):
        raw_move = -raw_move

    # сколько шагов прошло
    steps = raw_move / min_price_increment

    # PnL за 1 лот
    pnl_per_lot_rub = steps * min_price_increment_amount

    return pnl_per_lot_rub * Decimal(qty)


# ── Online-лог сделок для GUI ────────────────────────────────────────────────

def append_live_trade(
    event: str,
    ticker: str,
    direction: str,
    qty: int,
    entry_price: Decimal,
    exec_price: Decimal,
    pnl: Decimal,
    opened_at: datetime,
    ts: datetime,
    path: Optional[Path] = None,
) -> None:
    """
    Пишет сделку в trades/live_log.csv в формате:

    event,ticker,direction,qty,entry_price,exec_price,pnl,opened_at,timestamp,duration_min
    """
    if path is None:
        path = Path("trades") / "live_log.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    file_exists = path.exists()

    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if not file_exists:
            w.writerow(
                [
                    "event",
                    "ticker",
                    "direction",
                    "qty",
                    "entry_price",
                    "exec_price",
                    "pnl",
                    "opened_at",
                    "timestamp",
                    "duration_min",
                ]
            )

        duration_min = (ts - opened_at).total_seconds() / 60.0
        w.writerow(
            [
                event,
                ticker,
                direction,
                int(qty),
                float(entry_price),
                float(exec_price),
                float(pnl),
                opened_at.isoformat(),
                ts.isoformat(),
                round(duration_min, 4),
            ]
        )


# ── Логирование ───────────────────────────────────────────────────────────────

def setup_logging(cfg: dict) -> None:
    lc = cfg.get("logging", {})
    lvl = getattr(logging, lc.get("level", "INFO").upper(), logging.INFO)
    fpath = lc.get("file", "logs/bot.log")
    Path(fpath).parent.mkdir(parents=True, exist_ok=True)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    logging.basicConfig(
        level=lvl,
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.handlers.RotatingFileHandler(
                fpath,
                maxBytes=lc.get("max_bytes", 10_485_760),
                backupCount=lc.get("backup_count", 5),
                encoding="utf-8",
            ),
        ],
    )


# ── Конфиг ───────────────────────────────────────────────────────────────────

def load_config(path: str = "config/params.yaml") -> dict:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"{path} не найден")
    with open(p, encoding="utf-8") as f:
        return yaml.safe_load(f)


# ═════════════════════════════════════════════════════════════════════════════
# PAPER TRADING
# ═════════════════════════════════════════════════════════════════════════════

async def run_paper(cfg: dict) -> None:
    from broker.base import OrderDirection
    from broker.broker_paper import PaperBrokerClient
    from config.settings import (
        TELEGRAM_BOT_TOKEN,
        TELEGRAM_CHAT_ID,
        TINKOFF_API_TOKEN,
        TINKOFF_SANDBOX,
    )
    from data.data_feed import DataFeed
    from notifications.notifier_telegram import TelegramNotifier
    from risk.risk_manager import RiskManager, RiskViolation
    from strategy.base import SignalType
    from strategy.order_book_mm import OrderBookMM

    # очищаем возможный старый стоп-флаг
    if STOP_FLAG_PATH.exists():
        STOP_FLAG_PATH.unlink(missing_ok=True)

    strategy = OrderBookMM(cfg)
    risk = RiskManager(cfg)
    broker = PaperBrokerClient(Decimal(str(cfg["risk"]["capital_rub"])))
    notifier = TelegramNotifier(TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, cfg)
    feed = DataFeed(TINKOFF_API_TOKEN, TINKOFF_SANDBOX)

    tf_main = cfg["timeframe"]
    tf_global = cfg.get("global_trend_timeframe", "1h")

    gt_cfg = cfg["strategy"].get("global_trend", {})
    gt_enabled = gt_cfg.get("enabled", False)

    instruments = [i for i in cfg["instruments"] if i.get("enabled", True) and i.get("figi")]
    figis = [i["figi"] for i in instruments]
    ticker_by_figi = {i["figi"]: i["ticker"] for i in instruments}
    lot_by_figi = {i["figi"]: i.get("lot", 1) for i in instruments}
    inst_by_ticker = {i["ticker"]: i for i in instruments}

    # Загружаем спецификации фьючей по тикерам (если есть в risk/contracts.py или через API)
    paper_contract_specs: dict[str, FuturesContractSpec] = {}
    for inst in instruments:
        t = inst["ticker"]
        spec = get_futures_contract_by_ticker(t)
        if spec:
            paper_contract_specs[t] = spec
            logger.info(
                "[PAPER] Контракт %s: step=%s, step_amount=%s, size=%s",
                t,
                spec.min_price_increment,
                spec.min_price_increment_amount,
                spec.contract_size,
            )
        else:
            logger.warning("[PAPER] Не удалось получить спецификацию фьюча для %s", t)

    if not figis:
        logger.error("Нет активных инструментов с FIGI")
        return

    candle_buffers: dict[str, list] = {f: [] for f in figis}

    logger.info("Прогрев (3 дня) для PAPER...")
    for figi in figis:
        to_dt = datetime.now(tz=timezone.utc)
        from_dt = to_dt - timedelta(days=3)
        df = await feed.get_candles(figi, tf_main, from_dt, to_dt)
        candle_buffers[figi] = df.to_dict("records")

    await notifier.notify_bot_started("paper", list(ticker_by_figi.values()))
    logger.info("=== PAPER TRADING (GUI) ===")

    import pandas as pd

    gt_refresh_interval = 60
    gt_counter = 0
    entry_levels: dict[str, dict] = {}
    gui_prices: dict[str, Decimal] = {}

    stop_reason = "штатная остановка"
    stop_notified = False

    async def snapshot_paper_state(reason: str = "tick") -> None:
        try:
            positions = await broker.get_positions()
        except Exception:
            positions = []

        try:
            total_equity = broker.get_total_equity()
        except Exception:
            total_equity = Decimal(str(cfg["risk"]["capital_rub"]))

        capital_cfg = Decimal(str(cfg["risk"]["capital_rub"]))
        pnl_total = total_equity - capital_cfg

        prices = {t: float(p) for t, p in gui_prices.items()}

        state_positions = []
        for p in positions:
            meta = entry_levels.get(p.ticker, {})
            
            # GUI Metrics calculation
            entry_price = Decimal(str(meta.get("entry_price", p.avg_price)))
            sl_price = Decimal(str(meta.get("sl", 0)))
            tp_price = Decimal(str(meta.get("tp", 0)))
            last_price = gui_prices.get(p.ticker)
            side = meta.get("side", "")
            
            pnl_pct = None
            dist_to_tp_pct = None
            dist_to_sl_pct = None

            if last_price is not None and entry_price > 0:
                last_price_dec = Decimal(str(last_price))
                if side == "long":
                    move = last_price_dec - entry_price
                else:
                    move = entry_price - last_price_dec
                
                pnl_pct = float((move / entry_price) * Decimal("100"))

                if tp_price != 0:
                    dist_tp = (tp_price - last_price_dec) if side == "long" else (last_price_dec - tp_price)
                    dist_to_tp_pct = float((dist_tp / entry_price) * Decimal("100"))
                
                if sl_price != 0:
                    dist_sl = (last_price_dec - sl_price) if side == "long" else (sl_price - last_price_dec)
                    dist_to_sl_pct = float((dist_sl / entry_price) * Decimal("100"))

            state_positions.append(
                {
                    "ticker": p.ticker,
                    "side": side,
                    "qty": float(p.quantity),
                    "entry_price": float(entry_price),
                    "sl": float(sl_price) if sl_price is not None else None,
                    "tp": float(tp_price) if tp_price is not None else None,
                    "entry_time": meta.get("entry_time", ""),
                    "pnl_pct": pnl_pct,
                    "dist_to_tp_pct": dist_to_tp_pct,
                    "dist_to_sl_pct": dist_to_sl_pct,
                }
            )

        state = {
            "mode": "paper",
            "reason": reason,
            "timestamp": datetime.now(tz=timezone.utc).isoformat(),
            "equity": float(total_equity),
            "pnl_total": float(pnl_total),
            "prices": prices,
            "positions": state_positions,
        }
        write_bot_state(state)

    try:
        tf_main = cfg["timeframe"]
        async for figi, ob, candle in feed.stream_mm_data(figis, tf_main):
            # Проверка стоп-флага от GUI
            if STOP_FLAG_PATH.exists():
                logger.warning("Получен стоп-флаг от GUI. Останавливаю paper-loop.")
                stop_reason = "Остановка через GUI (stop.flag)"
                break

            figi = candle["figi"]
            ticker = ticker_by_figi.get(figi, figi)
            lot = lot_by_figi.get(figi, 1)
            now_utc = datetime.now(tz=timezone.utc)

            candle_buffers[figi].append(candle)
            if len(candle_buffers[figi]) > 500:
                candle_buffers[figi] = candle_buffers[figi][-500:]

            price_dec = Decimal(str(candle["close"]))
            broker.update_prices({ticker: price_dec})
            gui_prices[ticker] = price_dec

            # неполная свеча — просто обновляем state
            if not candle.get("is_complete", False):
                await snapshot_paper_state(reason="tick")
                continue

            # глобальный тренд
            gt_counter += 1
            if gt_enabled and gt_counter % gt_refresh_interval == 0:
                inst = inst_by_ticker.get(ticker, {})
                global_figi = inst.get("global_figi") or figi
                df_h = await feed.get_candles(
                    global_figi, tf_global, now_utc - timedelta(days=5), now_utc
                )
                trend = compute_global_trend(
                    df_h,
                    ema_fast=gt_cfg.get("ema_fast", 20),
                    ema_slow=gt_cfg.get("ema_slow", 50),
                    flat_gap_pct=gt_cfg.get("flat_gap_pct", 0.003),
                )
                strategy.set_global_trend(trend, ticker)

            df = pd.DataFrame(candle_buffers[figi])
            if len(df) < 20:
                await snapshot_paper_state(reason="tick")
                continue

            try:
                signals = strategy.generate_signals(df, ticker)
            except Exception as e:
                logger.error("Ошибка стратегии %s: %s", ticker, e)
                await notifier.notify_error(f"Ошибка стратегии {ticker}", e)
                continue

            positions = await broker.get_positions()
            pos_count = len(positions)
            pos_by_ticker = {p.ticker: p for p in positions}
            current_pos = pos_by_ticker.get(ticker)

            # проверка SL/TP
            if current_pos and ticker in entry_levels:
                meta = entry_levels[ticker]

                # защита от "вход и выход в одной свече"
                entry_candle_time = meta.get("entry_candle_time")
                if entry_candle_time and candle.get("time") == entry_candle_time:
                    await snapshot_paper_state(reason="same_candle_skip_sl_tp")
                    continue

                side = meta["side"]
                sl_price = meta["sl"]
                tp_price = meta["tp"]
                close_reason: Optional[str] = None
                close_price: Optional[Decimal] = None

                if side == "long":
                    if Decimal(str(candle["low"])) <= sl_price:
                        close_reason = "sl"
                        close_price = sl_price
                    elif Decimal(str(candle["high"])) >= tp_price:
                        close_reason = "tp"
                        close_price = tp_price
                else:
                    if Decimal(str(candle["high"])) >= sl_price:
                        close_reason = "sl"
                        close_price = sl_price
                    elif Decimal(str(candle["low"])) <= tp_price:
                        close_reason = "tp"
                        close_price = tp_price

                if close_reason and close_price is not None:
                    qty_to_close = abs(current_pos.quantity)
                    exit_direction = (
                        OrderDirection.SELL if current_pos.quantity > 0 else OrderDirection.BUY
                    )

                    # >>> DEBUG LOG: PAPER CLOSE <<<
                    logger.info(
                        "[%s] PAPER CLOSE %s | side=%s entry=%.4f sl=%.4f tp=%.4f low=%.4f high=%.4f",
                        ticker,
                        close_reason,
                        meta["side"],
                        float(meta["entry_price"]),
                        float(meta["sl"]),
                        float(meta["tp"]),
                        float(Decimal(str(candle["low"]))),
                        float(Decimal(str(candle["high"]))),
                    )
                    
                    # Wick Monitoring
                    if close_reason == "sl":
                        logger.info(
                            "[%s] PAPER Wick Monitor (SL Hit) | sl=%.4f low=%.4f close=%.4f",
                            ticker,
                            float(sl_price),
                            float(Decimal(str(candle["low"]))),
                            float(Decimal(str(candle["close"]))),
                        )

                    await broker.place_market_order(
                        figi, ticker, exit_direction, qty_to_close, execution_price=close_price
                    )

                    spec = paper_contract_specs.get(ticker)
                    if spec and spec.min_price_increment_amount > 0:
                        pnl = futures_pnl_rub(
                            entry_price=meta["entry_price"],
                            exit_price=close_price,
                            qty=qty_to_close,
                            min_price_increment=spec.min_price_increment,
                            min_price_increment_amount=spec.min_price_increment_amount,
                            side=side,
                        )
                    else:
                        # Фоллбек, если спецификации не нашли
                        pnl = (close_price - meta["entry_price"]) * Decimal(
                            qty_to_close if side == "long" else -qty_to_close
                        )
                    risk.record_pnl(pnl)
                    risk.update_capital(broker.get_total_equity())

                    await notifier.notify_trade_close(
                        ticker=ticker,
                        direction=side,
                        entry_price=meta["entry_price"],
                        exit_price=close_price,
                        quantity=qty_to_close,
                        pnl=pnl,
                        reason=close_reason,
                        mode="paper",
                    )

                    opened_at = datetime.fromisoformat(meta.get("entry_time", now_utc.isoformat()))
                    append_live_trade(
                        event="close",
                        ticker=ticker,
                        direction=side,
                        qty=qty_to_close,
                        entry_price=meta["entry_price"],
                        exec_price=close_price,
                        pnl=pnl,
                        opened_at=opened_at,
                        ts=now_utc,
                    )

                    logger.info(
                        "[%s] Закрытие по %s | entry=%.4f exit=%.4f pnl=%.2f ₽",
                        ticker,
                        close_reason,
                        float(meta["entry_price"]),
                        float(close_price),
                        float(pnl),
                    )
                    del entry_levels[ticker]
                    await snapshot_paper_state(reason=f"close_{close_reason}")
                    continue

            opened_here = False

            for sig in signals:
                price = Decimal(str(candle["close"]))

                if sig.is_entry:
                    if ticker in pos_by_ticker:
                        continue
                    try:
                        risk.check_entry_allowed(pos_count, now_utc)
                    except RiskViolation as e:
                        logger.warning("[%s] Риск-блок: %s", ticker, e)
                        continue

                    qty = risk.calculate_quantity(
                        price,
                        lot,
                        abs(price - sig.sl_price),
                        now_utc,
                    )
                    if qty == 0:
                        continue

                    direction = (
                        OrderDirection.BUY if sig.type == SignalType.LONG else OrderDirection.SELL
                    )

                    await broker.place_market_order(
                        figi, ticker, direction, qty, execution_price=price
                    )

                    entry_levels[ticker] = {
                        "side": "long" if sig.type == SignalType.LONG else "short",
                        "entry_price": price,
                        "sl": sig.sl_price,
                        "tp": sig.tp_price,
                        "entry_time": now_utc.isoformat(),
                        "entry_candle_time": candle.get("time") or candle.get("ts"),
                    }

                    # >>> DEBUG LOG: PAPER OPEN <<<
                    logger.info(
                        "[%s] PAPER OPEN | side=%s entry=%.4f sl=%.4f tp=%.4f candle_time=%s",
                        ticker,
                        entry_levels[ticker]["side"],
                        float(price),
                        float(entry_levels[ticker]["sl"]),
                        float(entry_levels[ticker]["tp"]),
                        candle.get("time") or candle.get("ts"),
                    )

                    await notifier.notify_trade_open(
                        ticker,
                        direction.value,
                        price,
                        qty,
                        sig.sl_price,
                        sig.tp_price,
                        sig.reason,
                        mode="paper",
                    )

                    append_live_trade(
                        event="open",
                        ticker=ticker,
                        direction="buy" if sig.type == SignalType.LONG else "sell",
                        qty=qty,
                        entry_price=price,
                        exec_price=price,
                        pnl=Decimal("0"),
                        opened_at=now_utc,
                        ts=now_utc,
                    )

                    opened_here = True
                    await snapshot_paper_state(reason="open")
                    break

            if opened_here or random.randint(1, 10) == 1:
                await snapshot_paper_state(reason="periodic")
            else:
                await snapshot_paper_state(reason="tick")

    except (KeyboardInterrupt, asyncio.CancelledError):
        stop_reason = "Остановка пользователем (Ctrl+C)"
        logger.info("Paper trading (GUI) остановлен пользователем")
        await notifier.notify_bot_stopped(stop_reason)
        stop_notified = True
    except Exception as e:
        stop_reason = f"Ошибка: {type(e).__name__}: {e}"
        logger.exception("Критическая ошибка в paper-режиме (GUI)")
        await notifier.notify_bot_stopped(stop_reason)
        stop_notified = True
        raise
    finally:
        if not stop_notified:
            await notifier.notify_bot_stopped(stop_reason)
        from runner import _save_paper_trades
        _save_paper_trades(broker.get_trade_log())
        await snapshot_paper_state(reason="final")
        # при штатной остановке можно очистить PID-состояние (если используешь)
        clear_bot_state()


# ═════════════════════════════════════════════════════════════════════════════
# LIVE (REAL)
# ═════════════════════════════════════════════════════════════════════════════

async def run_live(cfg: dict) -> None:
    from broker.base import OrderDirection
    from broker.broker_tinkoff import TinkoffBrokerClient
    from config.settings import (
        TELEGRAM_BOT_TOKEN,
        TELEGRAM_CHAT_ID,
        TINKOFF_ACCOUNT_ID,
        TINKOFF_API_TOKEN,
        TINKOFF_SANDBOX,
    )
    from data.data_feed import DataFeed
    from notifications.notifier_telegram import TelegramNotifier
    from risk.risk_manager import RiskManager, RiskViolation
    from strategy.base import SignalType
    from strategy.momentum import MomentumStrategy, compute_global_trend

    mode_label = "LIVE"
    logger.warning("=== %s EXECUTION LOOP (GUI) ===", mode_label)
    if TINKOFF_SANDBOX:
        logger.warning("Запущен live-loop в sandbox окружении T-Invest")

    # очищаем возможный старый стоп-флаг
    if STOP_FLAG_PATH.exists():
        STOP_FLAG_PATH.unlink(missing_ok=True)

    strategy = MomentumStrategy(cfg)
    risk = RiskManager(cfg)
    notifier = TelegramNotifier(TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, cfg)
    feed = DataFeed(TINKOFF_API_TOKEN, TINKOFF_SANDBOX)
    broker = TinkoffBrokerClient(
        token=TINKOFF_API_TOKEN,
        account_id=TINKOFF_ACCOUNT_ID,
        sandbox=TINKOFF_SANDBOX,
        live_confirmed=True,  # GUI live всегда с явным подтверждением через кнопку
    )

    # --- LIVE: OrderTracker для контроля статуса после выставления ордеров ---
    order_tracker = OrderTracker(broker, poll_interval=0.5, max_polls=30)

    # --- LIVE: Восстановление позиций при старте ---
    # Шаг 1: загружаем сохранённые позиции с SL/TP из файла
    live_positions: dict[str, dict] = load_live_positions()

    if live_positions:
        logger.info(
            "[LIVE] Загружены сохранённые позиции из %s: %s",
            POSITIONS_PERSIST_PATH,
            list(live_positions.keys()),
        )

    # Шаг 2: сверяем с реальным состоянием счёта через брокера
    now_utc = datetime.now(tz=timezone.utc)
    try:
        open_positions = await broker.get_positions()
    except Exception as e:
        logger.warning("[LIVE] Не удалось получить текущие позиции при старте: %s", e)
        open_positions = []

    broker_tickers = set()
    for p in open_positions:
        ticker = p.ticker
        qty = int(p.quantity)
        if qty == 0:
            continue
        broker_tickers.add(ticker)
        if ticker in live_positions:
            # позиция уже есть в persist-файле → обновляем только qty/avg_price
            # SL/TP сохраняем из файла (они точные)
            live_positions[ticker]["qty"] = abs(qty)
            live_positions[ticker]["entry_price"] = Decimal(str(p.avg_price))
            logger.info(
                "[LIVE] Sync: %s qty=%d entry=%.4f | SL=%.4f TP=%.4f (из файла)",
                ticker, abs(qty), float(p.avg_price),
                float(live_positions[ticker]["sl"]),
                float(live_positions[ticker]["tp"]),
            )
        else:
            # позиция есть на бирже, но файла нет → SL/TP неизвестны
            live_positions[ticker] = {
                "side": "long" if qty > 0 else "short",
                "qty": abs(qty),
                "entry_price": Decimal(str(p.avg_price)),
                "sl": Decimal("0"),   # SL неизвестен — SL/TP check будет пропущен
                "tp": Decimal("0"),   # TP неизвестен
                "entry_time": now_utc.isoformat(),
                "entry_candle_time": None,
                "_recovered_no_levels": True,  # маркер для логгирования
            }
            logger.warning(
                "[LIVE] Позиция %s (qty=%d) восстановлена из брокера БЕЗ SL/TP. "
                "Торговля по ней возобновится, но SL/TP не будут отслеживаться до закрытия.",
                ticker, abs(qty),
            )

    # Удаляем из live_positions тикеры, которых нет в брокере (позиция уже закрыта)
    stale = [t for t in list(live_positions) if t not in broker_tickers and open_positions]
    for t in stale:
        logger.warning("[LIVE] Позиция %s есть в файле, но не в брокере — удаляем.", t)
        del live_positions[t]

    if live_positions:
        logger.warning(
            "[LIVE] Итоговые активные позиции: %s",
            {t: {"side": v["side"], "qty": v["qty"], "sl": str(v["sl"]), "tp": str(v["tp"])}
             for t, v in live_positions.items()},
        )

    # --- LIVE: синхронизируем капитал с реальным счётом ---
    try:
        real_equity = await broker.get_total_equity()
        risk.update_capital(real_equity)
        logger.info(
            "[LIVE] Обновил капитал риска по счёту брокера: %.2f ₽",
            float(real_equity),
        )
    except Exception as e:
        logger.warning("[LIVE] Не удалось обновить капитал из брокера: %s", e)

    tf_main = cfg["timeframe"]
    tf_global = cfg.get("global_trend_timeframe", "1h")
    gt_cfg = cfg["strategy"].get("global_trend", {})
    gt_enabled = gt_cfg.get("enabled", False)

    instruments = [i for i in cfg["instruments"] if i.get("enabled", True) and i.get("figi")]
    figis = [i["figi"] for i in instruments]
    ticker_by_figi = {i["figi"]: i["ticker"] for i in instruments}
    lot_by_figi = {i["figi"]: i.get("lot", 1) for i in instruments}
    inst_by_ticker = {i["ticker"]: i for i in instruments}

    # Загружаем спецификации фьючей по тикерам
    live_contract_specs: dict[str, FuturesContractSpec] = {}
    for inst in instruments:
        t = inst["ticker"]
        spec = get_futures_contract_by_ticker(t)
        if spec:
            live_contract_specs[t] = spec
            logger.info(
                "[LIVE] Контракт %s: step=%s, step_amount=%s, size=%s",
                t,
                spec.min_price_increment,
                spec.min_price_increment_amount,
                spec.contract_size,
            )
        else:
            logger.warning("[LIVE] Не удалось получить спецификацию фьюча для %s", t)

    if not figis:
        logger.error("Нет активных инструментов с FIGI")
        return

    candle_buffers: dict[str, list] = {f: [] for f in figis}
    # live_positions уже инициализирован выше, здесь не дублируем
    gui_prices: dict[str, Decimal] = {}

    logger.info("[%s] Прогрев (3 дня)...", mode_label)
    for figi in figis:
        to_dt = datetime.now(tz=timezone.utc)
        from_dt = to_dt - timedelta(days=3)
        df = await feed.get_candles(figi, tf_main, from_dt, to_dt)
        candle_buffers[figi] = df.to_dict("records")

    await notifier.notify_bot_started("live", list(ticker_by_figi.values()))

    import pandas as pd

    gt_refresh_interval = 60
    gt_counter = 0
    stop_reason = "штатная остановка"
    stop_notified = False

    # ── Кэш equity — обновляем не чаще раза в 30 сек, чтобы не спамить API ──
    _equity_cache: dict = {"value": None, "ts": 0.0}
    EQUITY_REFRESH_SEC = 30.0

    async def snapshot_live_state(reason: str = "tick") -> None:
        nonlocal _equity_cache
        now_ts = time.monotonic()

        # Запрашиваем equity только при событиях (open/close/final) или раз в 30 сек
        force_equity = reason not in ("tick", "periodic", "same_candle_skip_sl_tp")
        if force_equity or (now_ts - _equity_cache["ts"]) >= EQUITY_REFRESH_SEC:
            try:
                _equity_cache["value"] = await broker.get_total_equity()
                _equity_cache["ts"] = now_ts
            except Exception:
                pass  # используем предыдущее значение

        total_equity = _equity_cache["value"]

        positions_view = []

        for ticker, pos in live_positions.items():
            entry_price = Decimal(str(pos["entry_price"]))
            sl = Decimal(str(pos["sl"]))
            tp = Decimal(str(pos["tp"]))
            last_price = gui_prices.get(ticker)

            pnl_pct = None
            dist_to_tp_pct = None
            dist_to_sl_pct = None

            if last_price is not None and entry_price > 0:
                last_price_dec = Decimal(str(last_price))
                
                # Расчет PnL % в зависимости от стороны
                if pos["side"] == "long":
                    move = last_price_dec - entry_price
                else:
                    move = entry_price - last_price_dec
                
                pnl_pct = float((move / entry_price) * Decimal("100"))

                if tp != 0:
                    dist_tp = (tp - last_price_dec) if pos["side"] == "long" else (last_price_dec - tp)
                    dist_to_tp_pct = float((dist_tp / entry_price) * Decimal("100"))
                
                if sl != 0:
                    dist_sl = (last_price_dec - sl) if pos["side"] == "long" else (sl - last_price_dec)
                    dist_to_sl_pct = float((dist_sl / entry_price) * Decimal("100"))

            positions_view.append(
                {
                    "ticker": ticker,
                    "side": pos["side"],
                    "qty": float(pos["qty"]),
                    "entry_price": float(entry_price),
                    "sl": float(sl),
                    "tp": float(tp),
                    "entry_time": pos.get("entry_time", ""),
                    "pnl_pct": pnl_pct,
                    "dist_to_tp_pct": dist_to_tp_pct,
                    "dist_to_sl_pct": dist_to_sl_pct,
                }
            )

        prices = {t: float(p) for t, p in gui_prices.items()}

        state = {
            "mode": "live",
            "reason": reason,
            "timestamp": datetime.now(tz=timezone.utc).isoformat(),
            "equity": float(total_equity) if total_equity is not None else None,
            "prices": prices,
            "positions": positions_view,
        }
        write_bot_state(state)

    try:
        tf_main = cfg["timeframe"]
        async for figi, ob, candle in feed.stream_mm_data(figis, tf_main):
            # Проверка стоп-флага от GUI
            if STOP_FLAG_PATH.exists():
                logger.warning("Получен стоп-флаг от GUI. Останавливаю %s-loop.", mode_label)
                stop_reason = "Остановка через GUI (stop.flag)"
                break

            figi = candle["figi"]
            ticker = ticker_by_figi.get(figi, figi)
            lot = lot_by_figi.get(figi, 1)
            now_utc = datetime.now(tz=timezone.utc)

            candle_buffers[figi].append(candle)
            if len(candle_buffers[figi]) > 500:
                candle_buffers[figi] = candle_buffers[figi][-500:]

            price_dec = Decimal(str(candle["close"]))
            gui_prices[ticker] = price_dec
            try:
                if hasattr(broker, "last_prices"):
                    if broker.last_prices is None:
                        broker.last_prices = {}
                    broker.last_prices[ticker] = price_dec
            except Exception:
                pass

            if not candle.get("is_complete", False):
                await snapshot_live_state(reason="tick")
                continue

            # ── Диагностика: видим когда реально приходит закрытая свеча ──
            logger.info(
                "[%s] СВЕЧА ЗАКРЫТА | time=%s close=%.2f buf=%d",
                ticker,
                candle.get("time") or candle.get("ts", "?"),
                float(candle["close"]),
                len(candle_buffers[figi]),
            )

            gt_counter += 1
            if gt_enabled and gt_counter % gt_refresh_interval == 0:
                inst = inst_by_ticker.get(ticker, {})
                global_figi = inst.get("global_figi") or figi
                df_h = await feed.get_candles(
                    global_figi, tf_global, now_utc - timedelta(days=5), now_utc
                )
                trend = compute_global_trend(
                    df_h,
                    ema_fast=gt_cfg.get("ema_fast", 20),
                    ema_slow=gt_cfg.get("ema_slow", 50),
                    flat_gap_pct=gt_cfg.get("flat_gap_pct", 0.003),
                )
                strategy.set_global_trend(trend, ticker)

            df = pd.DataFrame(candle_buffers[figi])
            if len(df) < 20:
                await snapshot_live_state(reason="tick")
                continue

            try:
                signals = strategy.generate_signals(df, ticker)
            except Exception as e:
                logger.error("Ошибка стратегии %s: %s", ticker, e)
                await notifier.notify_error(f"Ошибка стратегии {ticker}", e)
                continue

            # ── Диагностика: что вернула стратегия ──
            entry_sigs = [s for s in signals if s.is_entry]
            logger.info(
                "[%s] Сигналов всего=%d, входных=%d | позиций=%d",
                ticker, len(signals), len(entry_sigs), len(live_positions),
            )
            for s in entry_sigs:
                logger.info(
                    "[%s]   → %s sl=%.4f tp=%.4f reason=%s",
                    ticker, s.type, float(s.sl_price), float(s.tp_price),
                    getattr(s, "reason", "?"),
                )

            # SL/TP по уже открытым позициям
            position_closed_this_tick = False
            if ticker in live_positions:
                pos = live_positions[ticker]

                # защита от "вход и выход в одной свече"
                entry_candle_time = pos.get("entry_candle_time")
                if entry_candle_time and candle.get("time") == entry_candle_time:
                    await snapshot_live_state(reason="same_candle_skip_sl_tp")
                    continue

                # ── Защита от SL/TP = 0 (восстановленные без уровней позиции) ──
                sl_known = pos["sl"] != Decimal("0")
                tp_known = pos["tp"] != Decimal("0")

                if not sl_known or not tp_known:
                    logger.warning(
                        "[%s] SL/TP не определены (позиция восстановлена без уровней). "
                        "SL/TP check пропускается во избежание ложного закрытия.",
                        ticker,
                    )
                else:
                    close_reason: Optional[str] = None
                    close_price: Optional[Decimal] = None

                    if pos["side"] == "long":
                        if Decimal(str(candle["low"])) <= pos["sl"]:
                            close_reason = "sl"
                            close_price = pos["sl"]
                        elif Decimal(str(candle["high"])) >= pos["tp"]:
                            close_reason = "tp"
                            close_price = pos["tp"]
                    else:
                        if Decimal(str(candle["high"])) >= pos["sl"]:
                            close_reason = "sl"
                            close_price = pos["sl"]
                        elif Decimal(str(candle["low"])) <= pos["tp"]:
                            close_reason = "tp"
                            close_price = pos["tp"]

                    if close_reason and close_price is not None:
                        qty_to_close = pos["qty"]
                        exit_direction = (
                            OrderDirection.SELL if pos["side"] == "long" else OrderDirection.BUY
                        )

                        # >>> DEBUG LOG: LIVE CLOSE <<<
                        logger.info(
                        "[%s] LIVE CLOSE %s | side=%s entry=%.4f sl=%.4f tp=%.4f low=%.4f high=%.4f",
                        ticker,
                        close_reason,
                        pos["side"],
                        float(pos["entry_price"]),
                        float(pos["sl"]),
                        float(pos["tp"]),
                        float(Decimal(str(candle["low"]))),
                        float(Decimal(str(candle["high"]))),
                    )

                    # Wick Monitoring
                    if close_reason == "sl":
                        logger.info(
                            "[%s] LIVE Wick Monitor (SL Hit) | sl=%.4f low=%.4f close=%.4f",
                            ticker,
                            float(pos["sl"]),
                            float(Decimal(str(candle["low"]))),
                            float(Decimal(str(candle["close"]))),
                        )

                    await broker.place_market_order(
                        figi,
                        ticker,
                        exit_direction,
                        qty_to_close,
                    )

                    spec = live_contract_specs.get(ticker)
                    if spec and spec.min_price_increment_amount > 0:
                        pnl = futures_pnl_rub(
                            entry_price=pos["entry_price"],
                            exit_price=close_price,
                            qty=qty_to_close,
                            min_price_increment=spec.min_price_increment,
                            min_price_increment_amount=spec.min_price_increment_amount,
                            side=pos["side"],
                            ticker=ticker,
                        )
                    else:
                        pnl = (close_price - pos["entry_price"]) * Decimal(
                            qty_to_close if pos["side"] == "long" else -qty_to_close
                        )
                    risk.record_pnl(pnl)

                    await notifier.notify_trade_close(
                        ticker=ticker,
                        direction=pos["side"],
                        entry_price=pos["entry_price"],
                        exit_price=close_price,
                        quantity=qty_to_close,
                        pnl=pnl,
                        reason=close_reason,
                        mode="live",
                    )

                    opened_at = datetime.fromisoformat(pos.get("entry_time", now_utc.isoformat()))
                    append_live_trade(
                        event="close",
                        ticker=ticker,
                        direction=pos["side"],
                        qty=qty_to_close,
                        entry_price=pos["entry_price"],
                        exec_price=close_price,
                        pnl=pnl,
                        opened_at=opened_at,
                        ts=now_utc,
                    )

                    del live_positions[ticker]
                    save_live_positions(live_positions)  # персистим удаление позиции
                    await snapshot_live_state(reason=f"close_{close_reason}")
                    position_closed_this_tick = True

            # Открытие новых позиций (только если позиция не была закрыта в эту свечу)
            opened_here = False
            if not position_closed_this_tick:
              for sig in signals:
                if not sig.is_entry or ticker in live_positions:
                    continue

                price = Decimal(str(candle["close"]))

                # Cooldown — защита от дублирующихся сигналов на одном тикере
                if not _cooldown_ok(ticker, now_utc, cfg.get("signal_cooldown_sec", SIGNAL_COOLDOWN_SEC)):
                    logger.debug("[%s] Сигнал пропущен (cooldown)", ticker)
                    continue

                try:
                    risk.check_entry_allowed(len(live_positions), now_utc)
                except RiskViolation as e:
                    logger.warning("[%s] Риск-блок: %s", ticker, e)
                    continue

                # Запрашиваем ГО у Tinkoff API для этого фьючерса
                try:
                    margin_per_lot = await broker.get_futures_margin(figi)
                    logger.info(
                        "[%s] Margin per lot (ГО) по данным брокера: %.2f ₽",
                        ticker,
                        float(margin_per_lot),
                    )
                except Exception as e:
                    logger.warning(
                        "[%s] Не удалось получить ГО через GetFuturesMargin: %s. "
                        "Использую только риск-модель без ГО.",
                        ticker,
                        e,
                    )
                    margin_per_lot = None

                qty = risk.calculate_quantity(
                    price=price,
                    lot_size=lot,
                    sl_distance=abs(price - sig.sl_price),
                    dt_utc=now_utc,
                    margin_per_lot=margin_per_lot,
                )
                if qty <= 0:
                   logger.warning("%s | qty<=0 после расчёта риска — сигнал пропускаем", ticker)
                   continue

                # Дополнительный safety: ограничим лотность сверху
                if qty > 1:
                   logger.warning("%s | qty=%d > 1 — урезаем до 1 лота для LIVE-режима защиты", ticker, qty)
                   qty = 1

                if qty <= 0:
                    continue

                direction = (
                    OrderDirection.BUY if sig.type == SignalType.LONG else OrderDirection.SELL
                )

                order = await broker.place_market_order(figi, ticker, direction, qty)
                if order.status.value == "rejected":
                    logger.error("[%s] Ордер отклонён: %s", ticker, order.error_message)
                    continue

                # ── OrderTracker: ждём реального исполнения (не fire-and-forget) ──
                final_qty = qty
                entry_price = Decimal(str(order.filled_price)) if order.filled_price is not None else price

                if getattr(order, "order_id", None):
                    filled_state = await order_tracker.wait_fill(order.order_id)
                    if filled_state is not None:
                        # Частичное исполнение
                        filled_qty = getattr(filled_state, "filled_qty", None) or \
                                     getattr(filled_state, "lots_executed", None)
                        filled_price = getattr(filled_state, "filled_price", None) or \
                                       getattr(filled_state, "average_price", None)
                        if filled_qty is not None and int(filled_qty) != qty:
                            logger.warning(
                                "[%s] Частичное исполнение: запрошено %d лотов, исполнено %d",
                                ticker, qty, int(filled_qty),
                            )
                            final_qty = int(filled_qty)
                        if filled_price is not None:
                            entry_price = Decimal(str(filled_price))

                if final_qty <= 0:
                    logger.warning("[%s] Исполнено 0 лотов — позицию не открываем", ticker)
                    continue

                live_positions[ticker] = {
                    "side": "long" if sig.type == SignalType.LONG else "short",
                    "qty": final_qty,
                    "entry_price": entry_price,
                    "sl": sig.sl_price,
                    "tp": sig.tp_price,
                    "entry_time": now_utc.isoformat(),
                    "entry_candle_time": candle.get("time") or candle.get("ts"),
                }
                save_live_positions(live_positions)  # сохраняем SL/TP на диск

                # >>> DEBUG LOG: LIVE OPEN <<<
                logger.info(
                    "[%s] LIVE OPEN | side=%s entry=%.4f sl=%.4f tp=%.4f qty=%d candle_time=%s",
                    ticker,
                    live_positions[ticker]["side"],
                    float(entry_price),
                    float(live_positions[ticker]["sl"]),
                    float(live_positions[ticker]["tp"]),
                    final_qty,
                    candle.get("time") or candle.get("ts"),
                )

                await notifier.notify_trade_open(
                    ticker,
                    direction.value,
                    entry_price,
                    final_qty,
                    sig.sl_price,
                    sig.tp_price,
                    sig.reason,
                    mode="live",
                )

                append_live_trade(
                    event="open",
                    ticker=ticker,
                    direction="buy" if sig.type == SignalType.LONG else "sell",
                    qty=final_qty,
                    entry_price=entry_price,
                    exec_price=entry_price,
                    pnl=Decimal("0"),
                    opened_at=now_utc,
                    ts=now_utc,
                )

                opened_here = True
                await snapshot_live_state(reason="open")
                break

            if opened_here or random.randint(1, 10) == 1:
                await snapshot_live_state(reason="periodic")
            else:
                await snapshot_live_state(reason="tick")

    except (KeyboardInterrupt, asyncio.CancelledError):
        stop_reason = "Остановка пользователем (Ctrl+C)"
        logger.info("%s остановлен пользователем (GUI)", mode_label)
        await notifier.notify_bot_stopped(stop_reason)
        stop_notified = True
    except Exception as e:
        stop_reason = f"Ошибка: {type(e).__name__}: {e}"
        logger.exception("Критическая ошибка в live-режиме (GUI)")
        await notifier.notify_bot_stopped(stop_reason)
        stop_notified = True
        raise
    finally:
        if not stop_notified:
            await notifier.notify_bot_stopped(stop_reason)
        await snapshot_live_state(reason="final")
        clear_bot_state()


# ── CLI для runner_gui ───────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Trading Bot GUI runner (paper/live)",
    )
    p.add_argument("--mode", choices=["paper", "live"], default="paper")
    p.add_argument("--config", default="config/params.yaml")
    p.add_argument(
        "--confirm-live",
        action="store_true",
        help="Явно подтвердить запуск live-режима",
    )
    return p.parse_args()


async def main() -> None:
    args = parse_args()

    # ── Выставляем TRADER_LIVE_CONFIRMED до первого импорта config.settings ──
    # config/settings.py проверяет эту переменную при импорте (module-level),
    # поэтому её нужно установить ДО того как run_live() сделает lazy-import.
    if args.mode == "live" and args.confirm_live:
        os.environ.setdefault("TRADER_LIVE_CONFIRMED", "true")

    cfg = load_config(args.config)
    setup_logging(cfg)

    logger.info("GUI Runner | Режим: %s | Конфиг: %s", args.mode.upper(), args.config)

    try:
        if args.mode == "paper":
            await run_paper(cfg)
        elif args.mode == "live":
            if not args.confirm_live:
                logger.critical(
                    "⛔ LIVE требует --confirm-live. "
                    "Запустите через GUI 'Live' или добавьте --confirm-live."
                )
                sys.exit(1)
            await run_live(cfg)
    except Exception:
        logger.exception("Необработанная ошибка в runner_gui.main()")
        raise

if __name__ == "__main__":
    asyncio.run(main())

def run_mm_live(params: dict, broker, notifier=None, state_writer=None):
    """
    Основной MM-цикл для LIVE / Paper режима.
    Принимает брокера (TinkoffBrokerClient или PaperBrokerClient).

    ══════════════════════════════════════════════════════════════════════════
    ВНИМАНИЕ: Это ОТДЕЛЬНАЯ архитектура — Market Making на стакане.
    НЕ смешивать с run_live() / run_paper() (Momentum на свечах).
    Две модели работают на разных принципах:
      - Momentum: сигналы по свечам → открытие направленных позиций
      - Market Making: котирование bid/ask → заработок на спреде
    Для одновременного запуска обеих → отдельные процессы/конфиги.
    ══════════════════════════════════════════════════════════════════════════
    """
    strategy = OrderBookMMStrategy(params)
    risk_mgr = RiskManager(params)

    trend_cfg = params.get("trend_feed", {})
    trend_feed = TrendFeed(
        symbol=trend_cfg.get("symbol", "BTCUSDT"),
        interval=trend_cfg.get("interval", "1h"),
        ema_fast=trend_cfg.get("ema_fast", 20),
        ema_slow=trend_cfg.get("ema_slow", 50),
        update_interval_minutes=trend_cfg.get("update_interval_minutes", 5),
    )
    trend_feed.start()

    figi = params["instrument"]["figi"]
    loop_interval = params["market_maker"].get("loop_interval_seconds", 3)

    # Словарь для отслеживания выставленных ордеров: order_id → LimitOrderRequest
    live_orders: dict[str, object] = {}

    logger.info("MM loop started. figi=%s loop_interval=%ds", figi, loop_interval)

    try:
        while True:
            t0 = time.time()

            # 1. Получить стакан
            try:
                ob_raw = broker.get_order_book(figi, depth=20)
            except Exception as e:
                logger.error("get_order_book failed: %s", e)
                time.sleep(loop_interval)
                continue

            bids, asks = ob_raw["bids"], ob_raw["asks"]
            if not bids or not asks:
                logger.warning("Empty order book, skipping")
                time.sleep(loop_interval)
                continue

            best_bid  = bids[0][0]
            best_ask  = asks[0][0]
            mid_price = (best_bid + best_ask) / 2
            spread_pct = (best_ask - best_bid) / mid_price * 100

            ob = OrderBookState(
                best_bid=best_bid,
                best_ask=best_ask,
                mid_price=mid_price,
                spread_pct=spread_pct,
                bids=bids,
                asks=asks,
                timestamp=time.time(),
            )

            # 2. Текущая позиция
            net_qty = broker.get_position_qty(figi)
            active_orders = broker.get_active_orders(figi)
            active_buy  = sum(1 for o in active_orders if o.side == "BUY")
            active_sell = sum(1 for o in active_orders if o.side == "SELL")

            pos = PositionState(
                net_qty=net_qty,
                avg_entry=0.0,  # можно хранить в памяти
                unrealized_pnl=0.0,
                active_buy_orders=active_buy,
                active_sell_orders=active_sell,
            )

            # 3. Проверить TTL старых ордеров → отменить просроченные
            now = time.time()
            for order in list(active_orders):
                age = now - order.created_at
                ttl = params["market_maker"].get("order_ttl_seconds", 30)
                if age > ttl:
                    logger.info("TTL expired, cancelling %s (age=%.0fs)", order.order_id, age)
                    broker.cancel_order(order.order_id)

            # 4. Получить желаемые ордера от стратегии
            trend_bias = trend_feed.trend_bias
            desired_orders = strategy.generate_orders(ob, pos, trend_bias)

            # 5. Обновить (cancel+replace если нужно)
            _sync_orders(broker, figi, active_orders, desired_orders, strategy, risk_mgr, ob)

            # 6. Записать состояние для GUI
            if state_writer:
                state_writer({
                    "mode": "mm_live",
                    "equity": risk_mgr.capital_rub,
                    "mid_price": mid_price,
                    "spread_pct": round(spread_pct, 4),
                    "trend_bias": trend_bias,
                    "net_qty": net_qty,
                    "active_orders": len(active_orders),
                    "mm_stats": strategy.stats,
                })

            # 7. Пауза до следующего тика
            elapsed = time.time() - t0
            sleep_t = max(0, loop_interval - elapsed)
            time.sleep(sleep_t)

    except KeyboardInterrupt:
        logger.info("MM loop stopped by user")
    finally:
        trend_feed.stop()
        # Отменить все открытые ордера при выходе
        for order in broker.get_active_orders(figi):
            broker.cancel_order(order.order_id)
        logger.info("All active orders cancelled on exit")


def _sync_orders(broker, figi, active_orders, desired_orders, strategy, risk_mgr, ob):
    """
    Синхронизирует текущие активные ордера с желаемыми.
    """
    desired_by_side = {o.side.value: o for o in desired_orders}
    active_by_side  = {}
    for ao in active_orders:
        active_by_side.setdefault(ao.side, []).append(ao)

    for side_str, desired in desired_by_side.items():
        existing = active_by_side.get(side_str, [])

        if not existing:
            # Выставляем новый ордер
            allowed, reason = risk_mgr.check_can_place_order(
                side=side_str,
                active_orders=active_orders,
                current_inventory=0,  # TODO: передавать из pos
                price=desired.price,
                qty=desired.qty,
            )
            if allowed:
                oid = broker.place_limit_order(figi, side_str, desired.qty, desired.price)
                logger.info("PLACE %s %.2f x%d → %s", side_str, desired.price, desired.qty, oid)
            else:
                logger.debug("SKIP %s: %s", side_str, reason)
        else:
            # Проверяем, нужно ли обновить цену
            ao = existing[0]
            if strategy.should_refresh(desired.price, ao.price):
                broker.cancel_order(ao.order_id)
                oid = broker.place_limit_order(figi, side_str, desired.qty, desired.price)
                logger.info(
                    "REFRESH %s %.2f → %.2f (drift) → %s",
                    side_str, ao.price, desired.price, oid
                )