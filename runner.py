"""
runner.py v5 + GUI state (prices, entry_time)

Команды:
  python runner.py --mode backtest
  python runner.py --mode backtest --day random
  python runner.py --mode backtest --day 2026-03-05
  python runner.py --mode backtest --month 2026-02
  python runner.py --mode backtest --period "2026-01-01 to 2026-04-01"
  python runner.py --mode paper
  python runner.py --mode live
  python runner.py --find-figi GDM6
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import logging
import logging.handlers
import random
import sys
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Optional

import yaml


logger = logging.getLogger(__name__)

# ── State для GUI ─────────────────────────────────────────────────────────────
STATE_PATH = Path("state/bot_state.json")
STATE_PATH.parent.mkdir(parents=True, exist_ok=True)


def write_bot_state(state: dict) -> None:
    try:
        with open(STATE_PATH, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error("Не удалось записать state: %s", e)


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


# ── Диапазон дат ─────────────────────────────────────────────────────────────
MOEX_HOLIDAYS_2026 = {
    "2026-01-01",
    "2026-01-02",
    "2026-01-05",
    "2026-01-06",
    "2026-01-07",
    "2026-01-08",
    "2026-01-09",
    "2026-02-23",
    "2026-03-09",
    "2026-05-01",
    "2026-05-04",
    "2026-05-11",
    "2026-06-12",
    "2026-06-15",
    "2026-11-04",
    "2026-12-31",
}


def resolve_backtest_range(
    day_arg: Optional[str],
    month_arg: Optional[str],
    period_arg: Optional[str],
) -> tuple[datetime, datetime, str]:
    if period_arg:
        parts = period_arg.lower().split(" to ")
        if len(parts) != 2:
            raise ValueError(
                "Неверный формат --period. Используйте: '2026-01-01 to 2026-04-01'"
            )
        from_dt = datetime.fromisoformat(parts[0].strip()).replace(tzinfo=timezone.utc)
        to_dt = datetime.fromisoformat(parts[1].strip()).replace(
            hour=23, minute=59, second=59, tzinfo=timezone.utc
        )
        return from_dt, to_dt, f"период {parts[0].strip()} — {parts[1].strip()}"

    if day_arg:
        if day_arg.lower() == "random":
            return _pick_random_day()
        d = date.fromisoformat(day_arg)
        return *_day_bounds(d), f"день {d}"

    if month_arg:
        y, m = month_arg.split("-")
        return _month_bounds(int(y), int(m))

    try:
        from backtest.backtest_config import FROM_DATE, MODE, MONTH, TO_DATE, YEAR

        if MODE in ("day", "random"):
            return _pick_random_day()
        if MODE == "month":
            return _month_bounds(YEAR, MONTH)
        if MODE == "range":
            from_dt = datetime.fromisoformat(FROM_DATE).replace(tzinfo=timezone.utc)
            to_dt = datetime.fromisoformat(TO_DATE).replace(
                hour=23,
                minute=59,
                second=59,
                tzinfo=timezone.utc,
            )
            return from_dt, to_dt, f"{FROM_DATE} — {TO_DATE}"
    except ImportError:
        pass

    to_dt = datetime.now(tz=timezone.utc)
    from_dt = to_dt - timedelta(days=30)
    return from_dt, to_dt, "последние 30 дней"


def _day_bounds(d: date) -> tuple[datetime, datetime]:
    f = datetime(d.year, d.month, d.day, 6, 0, tzinfo=timezone.utc)
    t = datetime(d.year, d.month, d.day, 20, 59, 59, tzinfo=timezone.utc)
    return f, t


def _pick_random_day() -> tuple[datetime, datetime, str]:
    today = date.today()
    cands = [
        today - timedelta(days=i)
        for i in range(1, 91)
        if (today - timedelta(days=i)).weekday() < 5
        and (today - timedelta(days=i)).isoformat() not in MOEX_HOLIDAYS_2026
    ]
    if not cands:
        raise RuntimeError("Нет подходящих торговых дней в последних 90 днях")
    chosen = random.choice(cands)
    return *_day_bounds(chosen), f"случайный день {chosen}"


def _month_bounds(year: int, month: int) -> tuple[datetime, datetime, str]:
    import calendar

    f = datetime(year, month, 1, tzinfo=timezone.utc)
    last = calendar.monthrange(year, month)[1]
    t = datetime(year, month, last, 23, 59, 59, tzinfo=timezone.utc)
    return f, t, f"{calendar.month_name[month]} {year}"


# ══════════════════════════════════════════════════════════════════════════════
#  BACKTEST ПОРТФЕЛЯ
# ══════════════════════════════════════════════════════════════════════════════
async def run_backtest(
    cfg: dict,
    day_arg: Optional[str],
    month_arg: Optional[str],
    period_arg: Optional[str],
) -> None:
    from backtest.backtester import Backtester, BacktestResult
    from config.settings import TINKOFF_API_TOKEN, TINKOFF_SANDBOX
    from data.data_feed import DataFeed
    from risk.risk_manager import RiskManager
    from strategy.momentum import MomentumStrategy, compute_global_trend

    strategy = MomentumStrategy(cfg)
    risk = RiskManager(cfg)
    backtester = Backtester(strategy, risk)
    feed = DataFeed(TINKOFF_API_TOKEN, TINKOFF_SANDBOX)

    tf_main = cfg["timeframe"]
    tf_global = cfg.get("global_trend_timeframe", "1h")
    initial = cfg["risk"]["capital_rub"]
    gt_cfg = cfg["strategy"].get("global_trend", {})
    gt_enabled = gt_cfg.get("enabled", False)

    from_dt, to_dt, label = resolve_backtest_range(day_arg, month_arg, period_arg)

    logger.info("=" * 65)
    logger.info("PORTFOLIO BACKTEST | %s | tf=%s | %s", label, tf_main, strategy.name())
    logger.info("Период: %s → %s", from_dt.isoformat(), to_dt.isoformat())
    logger.info("=" * 65)

    instruments = [i for i in cfg["instruments"] if i.get("enabled", True) and i.get("figi")]
    if not instruments:
        logger.error("Нет активных инструментов с FIGI!")
        return

    portfolio_trades = []
    portfolio_pnl = 0.0
    portfolio_results: list[dict] = []

    for inst in instruments:
        ticker = inst["ticker"]
        figi = inst["figi"]
        lot = inst.get("lot", 1)

        logger.info("─── %s (%s) ─────────────────────────", ticker, figi)

        if gt_enabled:
            global_figi = inst.get("global_figi") or figi
            logger.info("[%s] Загружаю глобальный тренд (%s)...", ticker, tf_global)
            df_higher = await feed.get_candles(global_figi, tf_global, from_dt, to_dt)
            trend = compute_global_trend(
                df_higher,
                ema_fast=gt_cfg.get("ema_fast", 20),
                ema_slow=gt_cfg.get("ema_slow", 50),
                flat_gap_pct=gt_cfg.get("flat_gap_pct", 0.003),
            )
            strategy.set_global_trend(trend, ticker)
            logger.info("[%s] Глобальный тренд: %s", ticker, trend.value)

        logger.info("[%s] Загружаю основные свечи (%s)...", ticker, tf_main)
        df = await feed.get_candles(figi, tf_main, from_dt, to_dt)

        if df.empty:
            logger.warning("[%s] Нет данных за период", ticker)
            continue

        logger.info("[%s] Свечей: %d", ticker, len(df))

        try:
            result: BacktestResult = backtester.run(
                df, ticker, lot_size=lot, initial_capital=initial
            )
        except ValueError as e:
            logger.warning("[%s] %s", ticker, e)
            continue

        result.print_summary()

        ts = datetime.now().strftime("%Y%m%d_%H%M")
        fpath = Path("trades") / f"backtest_{ticker}_{ts}.csv"
        fpath.parent.mkdir(exist_ok=True)
        _save_trades_csv(result.trades, fpath)
        logger.info("[%s] Сделки → %s", ticker, fpath)

        portfolio_trades.extend(result.trades)
        portfolio_pnl += result.summary.get("total_pnl", 0)
        portfolio_results.append(
            {
                "ticker": ticker,
                "trades": result.summary.get("total_trades", 0),
                "pnl": result.summary.get("total_pnl", 0),
                "hit_rate": result.summary.get("hit_rate_pct", 0),
                "pf": result.summary.get("profit_factor", 0),
                "dd": result.summary.get("max_drawdown_pct", 0),
            }
        )

    if len(portfolio_results) > 1:
        _print_portfolio_summary(portfolio_results, portfolio_pnl, initial)


def _print_portfolio_summary(results: list[dict], total_pnl: float, initial: float) -> None:
    print("\n" + "═" * 65)
    print("  PORTFOLIO SUMMARY")
    print("═" * 65)
    print(f"  {'Тикер':<8} {'Сделок':>7} {'PnL':>10} {'Hit%':>7} {'PF':>7} {'DD%':>7}")
    print("  " + "─" * 55)
    total_trades = 0
    for r in results:
        print(
            f"  {r['ticker']:<8} {r['trades']:>7} "
            f"{r['pnl']:>10.2f} {r['hit_rate']:>6.1f}% "
            f"{r['pf']:>7.3f} {r['dd']:>6.1f}%"
        )
        total_trades += r["trades"]
    print("  " + "─" * 55)
    ret_pct = (total_pnl / initial * 100) if initial > 0 else 0
    print(
        f"  {'ИТОГО':<8} {total_trades:>7} {total_pnl:>10.2f}  "
        f"({ret_pct:+.2f}% от {initial:.0f} ₽)"
    )
    print("═" * 65)


# ══════════════════════════════════════════════════════════════════════════════
#  PAPER TRADING
# ══════════════════════════════════════════════════════════════════════════════
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
    from strategy.momentum import MomentumStrategy, compute_global_trend

    strategy = MomentumStrategy(cfg)
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

    if not figis:
        logger.error("Нет активных инструментов с FIGI")
        return

    candle_buffers: dict[str, list] = {f: [] for f in figis}
    logger.info("Прогрев (3 дня)...")
    for figi in figis:
        to_dt = datetime.now(tz=timezone.utc)
        from_dt = to_dt - timedelta(days=3)
        df = await feed.get_candles(figi, tf_main, from_dt, to_dt)
        candle_buffers[figi] = df.to_dict("records")

    await notifier.notify_bot_started("paper", list(ticker_by_figi.values()))
    logger.info("=== PAPER TRADING ===")

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
            state_positions.append(
                {
                    "ticker": p.ticker,
                    "side": meta.get("side", ""),
                    "qty": float(p.quantity),
                    "entry_price": float(meta.get("entry_price", p.avg_price)),
                    "sl": float(meta.get("sl", 0)) if meta.get("sl") is not None else None,
                    "tp": float(meta.get("tp", 0)) if meta.get("tp") is not None else None,
                    "entry_time": meta.get("entry_time", ""),
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
        async for candle in feed.stream_candles(figis, tf_main):
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

            if not candle.get("is_complete", False):
                await snapshot_paper_state(reason="tick")
                continue

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

            if current_pos and ticker in entry_levels:
                meta = entry_levels[ticker]
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
                    await broker.place_market_order(
                        figi, ticker, exit_direction, qty_to_close, execution_price=close_price
                    )
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
                    logger.info(
                        "[%s] Закрытие по %s | entry=%.4f exit=%.4f pnl=%.2f",
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

                    qty = risk.calculate_quantity(price, lot, abs(price - sig.sl_price), now_utc)
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
                    }
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
                    opened_here = True
                    await snapshot_paper_state(reason="open")
                    break

            if opened_here or random.randint(1, 10) == 1:
                await snapshot_paper_state(reason="periodic")
            else:
                await snapshot_paper_state(reason="tick")

    except (KeyboardInterrupt, asyncio.CancelledError):
        stop_reason = "Остановка пользователем (Ctrl+C)"
        logger.info("Paper trading остановлен пользователем (Ctrl+C)")
        await notifier.notify_bot_stopped(stop_reason)
        stop_notified = True
    except Exception as e:
        stop_reason = f"Ошибка: {type(e).__name__}: {e}"
        logger.exception("Критическая ошибка в paper-режиме")
        await notifier.notify_bot_stopped(stop_reason)
        stop_notified = True
        raise
    finally:
        if not stop_notified:
            await notifier.notify_bot_stopped(stop_reason)
        _save_paper_trades(broker.get_trade_log())
        await snapshot_paper_state(reason="final")


# ══════════════════════════════════════════════════════════════════════════════
#  LIVE
# ══════════════════════════════════════════════════════════════════════════════
async def run_live(cfg: dict, dry_run: bool = False) -> None:
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

    mode_label = "LIVE-DRYRUN" if dry_run else "LIVE"
    logger.warning("=== %s EXECUTION LOOP ===", mode_label)
    if TINKOFF_SANDBOX:
        logger.warning("Запущен live-loop в sandbox окружении T-Invest")

    strategy = MomentumStrategy(cfg)
    risk = RiskManager(cfg)
    notifier = TelegramNotifier(TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, cfg)
    feed = DataFeed(TINKOFF_API_TOKEN, TINKOFF_SANDBOX)
    broker = TinkoffBrokerClient(
        token=TINKOFF_API_TOKEN,
        account_id=TINKOFF_ACCOUNT_ID,
        sandbox=TINKOFF_SANDBOX,
        live_confirmed=not dry_run,
    )

    tf_main = cfg["timeframe"]
    tf_global = cfg.get("global_trend_timeframe", "1h")
    gt_cfg = cfg["strategy"].get("global_trend", {})
    gt_enabled = gt_cfg.get("enabled", False)

    instruments = [i for i in cfg["instruments"] if i.get("enabled", True) and i.get("figi")]
    figis = [i["figi"] for i in instruments]
    ticker_by_figi = {i["figi"]: i["ticker"] for i in instruments}
    lot_by_figi = {i["figi"]: i.get("lot", 1) for i in instruments}
    inst_by_ticker = {i["ticker"]: i for i in instruments}

    if not figis:
        logger.error("Нет активных инструментов с FIGI")
        return

    candle_buffers: dict[str, list] = {f: [] for f in figis}
    live_positions: dict[str, dict] = {}
    gui_prices: dict[str, Decimal] = {}

    logger.info("[%s] Прогрев (3 дня)...", mode_label)
    for figi in figis:
        to_dt = datetime.now(tz=timezone.utc)
        from_dt = to_dt - timedelta(days=3)
        df = await feed.get_candles(figi, tf_main, from_dt, to_dt)
        candle_buffers[figi] = df.to_dict("records")

    await notifier.notify_bot_started(
        "live-dry-run" if dry_run else "live", list(ticker_by_figi.values())
    )

    import pandas as pd

    gt_refresh_interval = 60
    gt_counter = 0
    stop_reason = "штатная остановка"
    stop_notified = False

    async def snapshot_live_state(reason: str = "tick") -> None:
        try:
            total_equity = broker.get_total_equity()
        except Exception:
            total_equity = None

        positions_view = []
        for ticker, pos in live_positions.items():
            positions_view.append(
                {
                    "ticker": ticker,
                    "side": pos["side"],
                    "qty": float(pos["qty"]),
                    "entry_price": float(pos["entry_price"]),
                    "sl": float(pos["sl"]),
                    "tp": float(pos["tp"]),
                    "entry_time": pos.get("entry_time", ""),
                }
            )

        prices = {t: float(p) for t, p in gui_prices.items()}

        state = {
            "mode": "live-dry-run" if dry_run else "live",
            "reason": reason,
            "timestamp": datetime.now(tz=timezone.utc).isoformat(),
            "equity": float(total_equity) if total_equity is not None else None,
            "prices": prices,
            "positions": positions_view,
        }
        write_bot_state(state)

    try:
        async for candle in feed.stream_candles(figis, tf_main):
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

            if ticker in live_positions:
                pos = live_positions[ticker]
                close_reason = None
                close_price = None
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
                    if dry_run:
                        logger.info(
                            "[DRY-RUN] CLOSE %s %s x%d | reason=%s @ %.4f",
                            ticker,
                            exit_direction.value,
                            qty_to_close,
                            close_reason,
                            float(close_price),
                        )
                    else:
                        await broker.place_market_order(figi, ticker, exit_direction, qty_to_close)
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
                    del live_positions[ticker]
                    await snapshot_live_state(reason=f"close_{close_reason}")
                    continue

            opened_here = False
            for sig in signals:
                if not sig.is_entry or ticker in live_positions:
                    continue
                price = Decimal(str(candle["close"]))
                try:
                    risk.check_entry_allowed(len(live_positions), now_utc)
                except RiskViolation as e:
                    logger.warning("[%s] Риск-блок: %s", ticker, e)
                    continue

                qty = risk.calculate_quantity(price, lot, abs(price - sig.sl_price), now_utc)
                if qty <= 0:
                    continue
                direction = (
                    OrderDirection.BUY if sig.type == SignalType.LONG else OrderDirection.SELL
                )

                if dry_run:
                    logger.info(
                        "[DRY-RUN] OPEN %s %s x%d @ %.4f",
                        ticker,
                        direction.value,
                        qty,
                        float(price),
                    )
                    entry_price = price
                else:
                    order = await broker.place_market_order(figi, ticker, direction, qty)
                    if order.status.value == "rejected":
                        logger.error("[%s] Ордер отклонён: %s", ticker, order.error_message)
                        continue
                    entry_price = order.filled_price or price

                live_positions[ticker] = {
                    "side": "long" if sig.type == SignalType.LONG else "short",
                    "qty": qty,
                    "entry_price": entry_price,
                    "sl": sig.sl_price,
                    "tp": sig.tp_price,
                    "entry_time": now_utc.isoformat(),
                }
                await notifier.notify_trade_open(
                    ticker,
                    direction.value,
                    entry_price,
                    qty,
                    sig.sl_price,
                    sig.tp_price,
                    sig.reason,
                    mode="live",
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
        logger.info("%s остановлен пользователем", mode_label)
        await notifier.notify_bot_stopped(stop_reason)
        stop_notified = True
    except Exception as e:
        stop_reason = f"Ошибка: {type(e).__name__}: {e}"
        logger.exception("Критическая ошибка в live-режиме")
        await notifier.notify_bot_stopped(stop_reason)
        stop_notified = True
        raise
    finally:
        if not stop_notified:
            await notifier.notify_bot_stopped(stop_reason)
        await snapshot_live_state(reason="final")


# ══════════════════════════════════════════════════════════════════════════════
#  FIND FIGI
# ══════════════════════════════════════════════════════════════════════════════
async def find_figi(ticker: str) -> None:
    from config.settings import TINKOFF_API_TOKEN, TINKOFF_SANDBOX
    from data.data_feed import DataFeed

    r = await DataFeed(TINKOFF_API_TOKEN, TINKOFF_SANDBOX).find_instrument(ticker)
    if r:
        print(f"\nНайден: {r}")
        print(f'\n  - ticker: "{r["ticker"]}"\n    figi: "{r["figi"]}"\n    lot: {r.get("lot",1)}')
    else:
        print(f"Не найден: {ticker}")


# ── Утилиты ───────────────────────────────────────────────────────────────────
def _save_trades_csv(trades, path: Path) -> None:
    if not trades:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(
            f,
            fieldnames=[
                "ticker",
                "direction",
                "entry_time",
                "entry_price",
                "exit_price",
                "exit_reason",
                "qty",
                "lot_size",
                "pnl",
                "commission",
            ],
        )
        w.writeheader()
        for t in trades:
            w.writerow(
                {
                    "ticker": t.ticker,
                    "direction": t.direction,
                    "entry_time": t.entry_time,
                    "entry_price": t.entry_price,
                    "exit_price": t.exit_price,
                    "exit_reason": t.exit_reason,
                    "qty": t.qty,
                    "lot_size": t.lot_size,
                    "pnl": round(t.pnl, 2),
                    "commission": round(t.commission, 4),
                }
            )


def _save_paper_trades(log: list[dict]) -> None:
    if not log:
        logger.info("Paper trades: журнал сделок пуст, CSV не создан")
        return
    p = Path("trades") / f"paper_{datetime.now().strftime('%Y%m%d_%H%M')}.csv"
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(log[0].keys()))
        w.writeheader()
        w.writerows(log)
    logger.info("Paper trades → %s", p)


# ── CLI ───────────────────────────────────────────────────────────────────────
def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Trading Bot v5 — Т-Инвест портфельный интрадей",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Примеры:
  python runner.py --mode backtest
  python runner.py --mode backtest --day random
  python runner.py --mode backtest --day 2026-03-05
  python runner.py --mode backtest --month 2026-02
  python runner.py --mode backtest --period "2026-01-01 to 2026-04-01"
  python runner.py --mode paper
  python runner.py --mode live
  python runner.py --find-figi GDM6
        """,
    )
    p.add_argument("--mode", choices=["backtest", "paper", "live"], default="backtest")
    p.add_argument("--day", metavar="DATE_OR_RANDOM", help='"random" или "2026-03-05"')
    p.add_argument("--month", metavar="YYYY-MM", help='"2026-02"')
    p.add_argument("--period", metavar="RANGE", help='"2026-01-01 to 2026-04-01"')
    p.add_argument("--find-figi", metavar="TICKER")
    p.add_argument("--config", default="config/params.yaml")
    p.add_argument(
        "--confirm-live", action="store_true", help="Явно подтвердить запуск live-режима"
    )
    p.add_argument(
        "--live-dry-run",
        action="store_true",
        help="Запустить live execution loop без отправки реальных ордеров",
    )
    p.add_argument(
        "--find-figi",
        metavar="TICKER",
        help="Найти FIGI по тикеру",
    )
    return p.parse_args()


async def main() -> None:
    args = parse_args()
    cfg = load_config(args.config)
    setup_logging(cfg)

    try:
        # === ПОИСК FIGI ПО ТИКЕРУ ===
        if args.find_figi:
            await find_figi(args.find_figi)
            return
        # ============================

        logger.info("Режим: %s | Конфиг: %s", args.mode.upper(), args.config)

        if args.mode == "backtest":
            await run_backtest(cfg, args.day, args.month, args.period)

        elif args.mode == "paper":
            await run_paper(cfg)

        elif args.mode == "live":
            if not args.confirm_live:
                logger.critical(
                    "⛔ LIVE требует --confirm-live. "
                    "Для безопасной проверки используйте --live-dry-run."
                )
                sys.exit(1)
            await run_live(cfg, dry_run=args.live_dry_run)
    except Exception:
        logger.exception("Необработанная ошибка в main()")
        raise

if __name__ == "__main__":
    asyncio.run(main())