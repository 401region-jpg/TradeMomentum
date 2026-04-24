"""
notifications/notifier_telegram.py

TelegramNotifier: отправляет уведомления в Telegram-чат.
Поддерживает все типы событий: вход/выход, SL/TP, ошибки, сводки, лимиты.

Использует python-telegram-bot в асинхронном режиме.
"""
from __future__ import annotations

import logging
from datetime import datetime
from decimal import Decimal
from typing import Optional

from telegram import Bot
from telegram.error import TelegramError

logger = logging.getLogger(__name__)


class TelegramNotifier:
    """
    Инициализируется токеном и chat_id из .env.
    Все методы — async, вызывать через await.
    Если Telegram недоступен — логирует ошибку, НЕ поднимает исключение
    (не должен останавливать торговлю из-за проблем с уведомлениями).
    """

    def __init__(self, bot_token: str, chat_id: str, cfg: Optional[dict] = None):
        self._bot = Bot(token=bot_token)
        self._chat_id = chat_id
        # Флаги включения/выключения типов уведомлений
        n = (cfg or {}).get("notifications", {})
        self._on_trade_open: bool = n.get("on_trade_open", True)
        self._on_trade_close: bool = n.get("on_trade_close", True)
        self._on_sl_tp: bool = n.get("on_sl_tp_update", True)
        self._on_error: bool = n.get("on_error", True)
        self._on_daily: bool = n.get("on_daily_summary", True)
        self._on_weekly: bool = n.get("on_weekly_summary", True)
        self._on_drawdown: bool = n.get("on_drawdown_limit", True)
        self._on_start_stop: bool = n.get("on_bot_start_stop", True)
        self._on_no_data: bool = n.get("on_no_data", True)

    # ── Вход в позицию ────────────────────────────────────────────────────────
    async def notify_trade_open(
        self,
        ticker: str,
        direction: str,
        price: Decimal,
        quantity: int,
        sl: Decimal,
        tp: Decimal,
        reason: str = "",
        mode: str = "paper",
    ) -> None:
        if not self._on_trade_open:
            return
        emoji = "🟢" if direction.upper() == "BUY" else "🔴"
        mode_tag = "📋 PAPER" if mode == "paper" else "💰 LIVE"
        text = (
            f"{emoji} <b>ВХОД В ПОЗИЦИЮ</b> [{mode_tag}]\n"
            f"Инструмент: <b>{ticker}</b>\n"
            f"Направление: <b>{direction.upper()}</b>\n"
            f"Количество: {quantity} лот(ов)\n"
            f"Цена входа: <b>{price:.4f}</b>\n"
            f"Стоп-лосс:  🛑 {sl:.4f}\n"
            f"Тейк-профит: 🎯 {tp:.4f}\n"
            f"Причина: {reason or '—'}"
        )
        await self._send(text)

    # ── Выход из позиции ─────────────────────────────────────────────────────
    async def notify_trade_close(
        self,
        ticker: str,
        direction: str,
        entry_price: Decimal,
        exit_price: Decimal,
        quantity: int,
        pnl: Decimal,
        reason: str = "",
        mode: str = "paper",
    ) -> None:
        if not self._on_trade_close:
            return
        emoji = "🟩" if pnl >= 0 else "🟥"
        mode_tag = "📋 PAPER" if mode == "paper" else "💰 LIVE"
        text = (
            f"{emoji} <b>ВЫХОД ИЗ ПОЗИЦИИ</b> [{mode_tag}]\n"
            f"Инструмент: <b>{ticker}</b>\n"
            f"Направление: {direction.upper()}\n"
            f"Кол-во: {quantity} лот(ов)\n"
            f"Цена входа:  {entry_price:.4f}\n"
            f"Цена выхода: {exit_price:.4f}\n"
            f"PnL: <b>{'+'if pnl>=0 else ''}{pnl:.2f} ₽</b>\n"
            f"Причина: {reason or '—'}"
        )
        await self._send(text)

    # ── Изменение SL/TP ───────────────────────────────────────────────────────
    async def notify_sl_tp_update(
        self,
        ticker: str,
        new_sl: Optional[Decimal],
        new_tp: Optional[Decimal],
    ) -> None:
        if not self._on_sl_tp:
            return
        parts = [f"⚙️ <b>Обновление SL/TP</b> — {ticker}"]
        if new_sl:
            parts.append(f"Новый SL: 🛑 {new_sl:.4f}")
        if new_tp:
            parts.append(f"Новый TP: 🎯 {new_tp:.4f}")
        await self._send("\n".join(parts))

    # ── Ошибка ────────────────────────────────────────────────────────────────
    async def notify_error(self, message: str, exc: Optional[Exception] = None) -> None:
        if not self._on_error:
            return
        text = f"🚨 <b>ОШИБКА БОТА</b>\n{message}"
        if exc:
            text += f"\n<code>{type(exc).__name__}: {exc}</code>"
        await self._send(text)

    # ── Дневная сводка ────────────────────────────────────────────────────────
    async def notify_daily_summary(
        self,
        day_pnl: Decimal,
        trades_count: int,
        win_trades: int,
        equity: Decimal,
    ) -> None:
        if not self._on_daily:
            return
        hit_rate = (win_trades / trades_count * 100) if trades_count > 0 else 0
        emoji = "📈" if day_pnl >= 0 else "📉"
        text = (
            f"{emoji} <b>ДНЕВНАЯ СВОДКА</b> — {datetime.now().strftime('%d.%m.%Y')}\n"
            f"PnL за день: <b>{'+'if day_pnl>=0 else ''}{day_pnl:.2f} ₽</b>\n"
            f"Сделок: {trades_count} | Прибыльных: {win_trades} ({hit_rate:.0f}%)\n"
            f"Текущий капитал: {equity:.2f} ₽"
        )
        await self._send(text)

    # ── Недельная сводка ──────────────────────────────────────────────────────
    async def notify_weekly_summary(
        self,
        week_pnl: Decimal,
        trades_count: int,
        win_trades: int,
        equity: Decimal,
        sharpe: Optional[float] = None,
        max_dd: Optional[float] = None,
    ) -> None:
        if not self._on_weekly:
            return
        hit_rate = (win_trades / trades_count * 100) if trades_count > 0 else 0
        emoji = "📈" if week_pnl >= 0 else "📉"
        text = (
            f"{emoji} <b>НЕДЕЛЬНАЯ СВОДКА</b>\n"
            f"PnL за неделю: <b>{'+'if week_pnl>=0 else ''}{week_pnl:.2f} ₽</b>\n"
            f"Сделок: {trades_count} | Прибыльных: {win_trades} ({hit_rate:.0f}%)\n"
            f"Текущий капитал: {equity:.2f} ₽"
        )
        if sharpe is not None:
            text += f"\nSharpe: {sharpe:.2f}"
        if max_dd is not None:
            text += f"\nМакс. просадка: {max_dd:.1f}%"
        await self._send(text)

    # ── Превышение лимита просадки ────────────────────────────────────────────
    async def notify_drawdown_limit_hit(
        self,
        limit_type: str,     # "daily" | "weekly"
        current_loss: Decimal,
        limit: Decimal,
    ) -> None:
        if not self._on_drawdown:
            return
        period = "дневной" if limit_type == "daily" else "недельный"
        text = (
            f"🛑 <b>ДОСТИГНУТ {period.upper()} ЛИМИТ ПОТЕРЬ</b>\n"
            f"Потеря: <b>{current_loss:.2f} ₽</b>\n"
            f"Лимит: {limit:.2f} ₽\n"
            f"Бот остановлен. Торговля приостановлена."
        )
        await self._send(text)

    # ── Старт/стоп бота ───────────────────────────────────────────────────────
    async def notify_bot_started(self, mode: str, instruments: list[str]) -> None:
        if not self._on_start_stop:
            return
        text = (
            f"✅ <b>БОТ ЗАПУЩЕН</b>\n"
            f"Режим: <b>{mode.upper()}</b>\n"
            f"Инструменты: {', '.join(instruments)}\n"
            f"Время: {datetime.now().strftime('%d.%m.%Y %H:%M:%S')} МСК"
        )
        await self._send(text)

    async def notify_bot_stopped(self, reason: str = "") -> None:
        if not self._on_start_stop:
            return
        text = (
            f"⏹️ <b>БОТ ОСТАНОВЛЕН</b>\n"
            f"Причина: {reason or 'штатная остановка'}\n"
            f"Время: {datetime.now().strftime('%d.%m.%Y %H:%M:%S')} МСК"
        )
        await self._send(text)

    # ── Отсутствие котировок ──────────────────────────────────────────────────
    async def notify_no_data(self, ticker: str, seconds: int) -> None:
        if not self._on_no_data:
            return
        await self._send(
            f"⚠️ <b>Нет котировок</b> по {ticker} уже {seconds} сек."
        )

    # ── Внутренний метод отправки ─────────────────────────────────────────────
    async def _send(self, text: str) -> None:
        try:
            await self._bot.send_message(
                chat_id=self._chat_id,
                text=text,
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
        except TelegramError as e:
            # Не поднимаем — только логируем. Торговля не должна останавливаться из-за TG.
            logger.error("Ошибка отправки Telegram-уведомления: %s", e)
