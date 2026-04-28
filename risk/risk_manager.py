"""
risk/risk_manager.py  v6 — session risk + margin_per_lot + мягкий R:R

Ключевые моменты:
  - Сессионный риск: normal / boost / evening (разный max_position_pct)
  - get_session_info() → возвращает (label, pct, require_trend) для текущего времени МСК
  - Логи: "SESSION=BOOST | position_pct=8.0%"
  - УЧЁТ ГО: margin_per_lot — ограничивает qty по доступной марже
  - Портфельный лимит сделок убран: бот может делать сколько угодно входов
  - Ограничение по R:R ослаблено — низкий R:R не блокирует вход, только даёт warning
  - Добавлена защита: дневной/недельный лимит по % капитала, защита от аномального PnL
"""

from __future__ import annotations

import datetime
import logging
from decimal import Decimal
from typing import Optional

import pytz

logger = logging.getLogger(__name__)
MSK = pytz.timezone("Europe/Moscow")

# Минимальный "рекомендованный" R:R для предупреждений
MIN_RR = 0.1

# Кап ДД/PNL на одну сделку (как доля капитала): если PnL по одной сделке > 5×cap,
# считаем, что это ошибка расчёта или масштабов и жёстко режем.
MAX_SINGLE_PNL_PCT = Decimal("5")  # 500% капитала на сделку — явно баг


class RiskViolation(Exception):
    pass


class DailyLimitExceeded(RiskViolation):
    pass


class WeeklyLimitExceeded(RiskViolation):
    pass


class MaxPositionsExceeded(RiskViolation):
    pass


class ConsecutiveLossLimitExceeded(RiskViolation):
    pass


class CooldownActive(RiskViolation):
    pass


def _parse_time(t: str) -> datetime.time:
    h, m = t.split(":")
    return datetime.time(int(h), int(m))


class SessionConfig:
    """Конфиг одной торговой сессии."""

    def __init__(self, d: dict, fallback_pct: float):
        self.start = _parse_time(d.get("start", "00:00"))
        self.end = _parse_time(d.get("end", "23:59"))
        self.position_pct = float(d.get("max_position_pct", fallback_pct))
        self.label = d.get("label", "UNKNOWN")
        self.require_trend = bool(d.get("require_global_trend", False))


class RiskManager:

    def __init__(self, cfg: dict):
        r = cfg["risk"]
        s = cfg["strategy"]

        # Базовый капитал (обновляется в рантайме по счёту брокера)
        self._capital = Decimal(str(r["capital_rub"]))

        # Базовый процент капитала под позицию (если нет сессий)
        self._default_position_pct = float(r.get("max_position_pct", 1.0))

        # Плечо (линейный множитель)
        self._max_leverage = float(r.get("max_leverage", 1.0))

        # Лимиты по дроудауну
        self._daily_loss_limit_pct = float(r.get("daily_loss_limit_pct", 1.0))
        self._weekly_loss_limit_pct = float(r.get("weekly_loss_limit_pct", 1.0))

        # Лимит позиций одновременно
        self._max_positions = int(s.get("max_positions", 1))

        # ГО на 1 лот (в рублях); если не задано, считаем, что не ограничивает
        self._margin_per_lot: Optional[Decimal] = None
        margin_cfg = r.get("margin_per_lot")
        if margin_cfg is not None:
            try:
                self._margin_per_lot = Decimal(str(margin_cfg))
                logger.info(
                    "Margin per lot (ГО) задан: %.2f ₽", float(self._margin_per_lot)
                )
            except Exception:
                logger.warning("Не удалось распарсить margin_per_lot=%r", margin_cfg)

        # Серия убытков и кулдаун
        self._max_consec_losses = int(r.get("max_consecutive_losses", 999))
        self._cooldown_min = int(r.get("cooldown_after_loss_min", 0))

        # Проверка R:R по конфигу стратегии
        sl = float(s.get("atr_sl_multiplier", 1.0))
        tp = float(s.get("atr_tp_multiplier", 2.0))
        self._configured_rr = tp / sl if sl > 0 else 0.0

        if self._configured_rr < MIN_RR:
            # Низкий R:R — только предупреждение, входы не блочим
            logger.warning(
                "⚠️  Низкий R:R=%.2f < %.1f. Проверьте atr_sl/atr_tp в params.yaml "
                "(тейк очень близкий к стопу).",
                self._configured_rr,
                MIN_RR,
            )
        else:
            logger.info(
                "✅ R:R=%.2f (sl×%.2f / tp×%.2f)", self._configured_rr, sl, tp
            )

        # Сессионные конфиги
        ses_cfg = r.get("sessions", {})
        self._sessions: dict[str, SessionConfig] = {}
        for key in ("normal", "boost", "evening"):
            cfg_ses = ses_cfg.get(key)
            if cfg_ses:
                self._sessions[key] = SessionConfig(cfg_ses, self._default_position_pct)
        if not self._sessions:
            logger.info(
                "Сессионный риск не настроен — используется базовый position_pct"
            )

        # Состояние (сбрасывается ежедневно / еженедельно)
        self._day_pnl = Decimal("0")
        self._week_pnl = Decimal("0")
        self._portfolio_trades = 0
        self._consec_losses = 0
        self._last_loss_time: Optional[datetime.datetime] = None

        _today = datetime.date.today()
        self._current_date = _today
        self._current_week = _today.isocalendar()[1]

    # ── Сессионный риск ───────────────────────────────────────────────────────

    def get_session_info(
        self, dt_utc: Optional[datetime.datetime] = None
    ) -> tuple[str, float, bool]:
        """
        Возвращает (label, position_pct, require_trend) для текущего времени МСК.
        Если сессионный конфиг не задан — возвращает базовые значения.
        """
        if not self._sessions:
            return "DEFAULT", self._default_position_pct, False

        if dt_utc is None:
            dt_utc = datetime.datetime.now(datetime.UTC)
        elif dt_utc.tzinfo is None:
            dt_utc = dt_utc.replace(tzinfo=pytz.utc)

        msk_time = dt_utc.astimezone(MSK).time()

        # Приоритет: BOOST → EVENING → NORMAL
        for key in ("boost", "evening", "normal"):
            ses = self._sessions.get(key)
            if not ses:
                continue
            if ses.start <= msk_time <= ses.end:
                return ses.label, ses.position_pct, ses.require_trend

        return "OFF_HOURS", 0.0, False

    # ── Обновление капитала и PnL ─────────────────────────────────────────────

    def update_capital(self, c: Decimal) -> None:
        """Обновление капитала по счёту брокера."""
        if c <= 0:
            logger.warning("Попытка обновить капитал некорректным значением: %.2f", float(c))
            return
        self._capital = c

    def record_pnl(self, pnl: Decimal) -> None:
        """
        Регистрирует результат сделки, но с защитой от аномальных величин PnL.
        Если PnL по модулю > MAX_SINGLE_PNL_PCT * capital, он обрезается.
        """
        if self._capital > 0:
            limit_abs = self._capital * MAX_SINGLE_PNL_PCT
            if pnl > limit_abs:
                logger.error(
                    "❌ Аномальный PnL=%.2f ₽ > %.0f×капитал (%.2f). Обрезаем до лимита.",
                    float(pnl),
                    float(MAX_SINGLE_PNL_PCT),
                    float(limit_abs),
                )
                pnl = limit_abs
            elif pnl < -limit_abs:
                logger.error(
                    "❌ Аномальный PnL=%.2f ₽ < -%.0f×капитал (%.2f). Обрезаем до лимита.",
                    float(pnl),
                    float(MAX_SINGLE_PNL_PCT),
                    float(limit_abs),
                )
                pnl = -limit_abs

        self._day_pnl += pnl
        self._week_pnl += pnl
        self._portfolio_trades += 1

        if pnl < 0:
            self._consec_losses += 1
            self._last_loss_time = datetime.datetime.now(datetime.UTC)
            logger.warning(
                "📉 Убыток %.2f ₽ | Серия: %d/%d | Сделок за день: %d",
                float(pnl),
                self._consec_losses,
                self._max_consec_losses,
                self._portfolio_trades,
            )
        else:
            self._consec_losses = 0
            logger.info(
                "📈 Прибыль %.2f ₽ | Сделок за день: %d",
                float(pnl),
                self._portfolio_trades,
            )

        logger.info(
            "День: %.2f ₽ | Неделя: %.2f ₽",
            float(self._day_pnl),
            float(self._week_pnl),
        )

    def reset_daily(self) -> None:
        logger.info(
            "🔄 Сброс дня. PnL: %.2f ₽ | Сделок: %d",
            float(self._day_pnl),
            self._portfolio_trades,
        )
        self._day_pnl = Decimal("0")
        self._portfolio_trades = 0
        self._consec_losses = 0
        self._last_loss_time = None

    def reset_weekly(self) -> None:
        logger.info("🔄 Сброс недели. PnL: %.2f ₽", float(self._week_pnl))
        self._week_pnl = Decimal("0")

    # ── Проверки перед входом ─────────────────────────────────────────────────

    def check_entry_allowed(
        self,
        current_positions_count: int,
        dt_utc: Optional[datetime.datetime] = None,
    ) -> None:
        """Комплексная проверка перед открытием новой позиции."""
        self._check_date_reset()
        self._check_daily_limit()
        self._check_weekly_limit()
        self._check_max_positions(current_positions_count)
        self._check_consecutive_losses()
        self._check_cooldown()

        # Проверяем торговые часы по сессиям
        if self._sessions and dt_utc is not None:
            label, pct, _ = self.get_session_info(dt_utc)
            if pct <= 0:
                raise RiskViolation(f"Нет торговой сессии в {label}")

    def _check_daily_limit(self) -> None:
        limit = self._capital * Decimal(str(self._daily_loss_limit_pct))
        if self._day_pnl < -limit:
            raise DailyLimitExceeded(
                f"Дневной лимит: {float(self._day_pnl):.2f} ₽ "
                f"(лимит -{float(limit):.2f} ₽)"
            )

    def _check_weekly_limit(self) -> None:
        limit = self._capital * Decimal(str(self._weekly_loss_limit_pct))
        if self._week_pnl < -limit:
            raise WeeklyLimitExceeded(
                f"Недельный лимит: {float(self._week_pnl):.2f} ₽ "
                f"(лимит -{float(limit):.2f} ₽)"
            )

    def _check_max_positions(self, count: int) -> None:
        if count >= self._max_positions:
            raise MaxPositionsExceeded(f"Лимит позиций: {count}/{self._max_positions}")

    def _check_consecutive_losses(self) -> None:
        if self._consec_losses >= self._max_consec_losses:
            raise ConsecutiveLossLimitExceeded(
                f"Серия убытков: {self._consec_losses}/{self._max_consec_losses}"
            )

    def _check_cooldown(self) -> None:
        if self._cooldown_min <= 0 or self._last_loss_time is None:
            return
        elapsed = (
            datetime.datetime.now(datetime.UTC) - self._last_loss_time
        ).total_seconds() / 60
        if elapsed < self._cooldown_min:
            raise CooldownActive(
                f"Кулдаун: ещё {int(self._cooldown_min - elapsed)} мин"
            )

    def _check_date_reset(self) -> None:
        today = datetime.date.today()
        week = today.isocalendar()[1]
        if self._current_date != today:
            self.reset_daily()
            self._current_date = today
        if self._current_week != week:
            self.reset_weekly()
            self._current_week = week

    # ── Размер позиции (с учётом сессии и ГО) ────────────────────────────────

    def calculate_quantity(
        self,
        price: Decimal,
        lot_size: int,
        sl_distance: Decimal,
        dt_utc: Optional[datetime.datetime] = None,
        margin_per_lot: Optional[Decimal] = None,
    ) -> int:
        """
        price         — цена входа за 1 контракт
        lot_size      — размер лота в контрактах (обычно 1)
        sl_distance   — расстояние до стопа в рублях на 1 контракт
        margin_per_lot — ГО на 1 контракт (если None — используем self._margin_per_lot)
        """
        if sl_distance <= 0 or price <= 0:
            logger.warning(
                "calculate_quantity: некорректные price/sl_distance: price=%.4f sl=%.4f",
                float(price),
                float(sl_distance),
            )
            return 0

        # Ограничение по R:R теперь не блокирует вход, только предупреждает
        if self._configured_rr < MIN_RR:
            logger.warning(
                "⚠️ Низкий R:R=%.2f < %.1f — расчёт qty разрешён, но настройка atr_sl/atr_tp выглядит агрессивной.",
                self._configured_rr,
                MIN_RR,
            )

        # Определяем session position_pct
        ses_label, ses_pct, _ = self.get_session_info(dt_utc)
        position_pct = float(ses_pct if self._sessions else self._default_position_pct)

        # Защита: не позволяем использовать >100% капитала на сделку
        if position_pct > 1.0:
            logger.warning(
                "position_pct=%.2f > 1.0 — урезаем до 100%% капитала.",
                position_pct,
            )
            position_pct = 1.0
        if position_pct <= 0:
            logger.warning(
                "position_pct=0 для сессии %s — qty=0", ses_label
            )
            return 0

        logger.info(
            "SESSION=%s | position_pct=%.1f%% | capital=%.2f ₽",
            ses_label,
            position_pct * 100,
            float(self._capital),
        )

        leverage = Decimal(str(self._max_leverage))
        capital_dec = self._capital

        # 1) Ограничение по стоимости позиции (капитал × pct × leverage)
        max_pos_val = capital_dec * Decimal(str(position_pct)) * leverage
        lots_cap = int(max_pos_val / (price * lot_size))

        # 2) Ограничение по стоп‑риску (капитал × pct под стоп)
        risk_budget = capital_dec * Decimal(str(position_pct))
        risk_per_lot = sl_distance * lot_size
        lots_risk = int(risk_budget / risk_per_lot) if risk_per_lot > 0 else 0

        # 3) Ограничение по ГО
        eff_margin_per_lot: Optional[Decimal] = None
        if margin_per_lot is not None and margin_per_lot > 0:
            eff_margin_per_lot = margin_per_lot
        elif self._margin_per_lot is not None and self._margin_per_lot > 0:
            eff_margin_per_lot = self._margin_per_lot

        if eff_margin_per_lot is not None:
            margin_budget = capital_dec * Decimal(str(position_pct))
            lots_margin = int(margin_budget / eff_margin_per_lot)
        else:
            lots_margin = lots_cap  # если ГО не задано, не ограничиваем дополнительно

        # Итоговое количество — минимум из трёх
        qty = min(lots_cap, lots_risk, lots_margin)

        if qty <= 0:
            logger.warning(
                "qty=0: capital=%.2f price=%.4f sl=%.4f lot=%d ses=%s "
                "(cap=%d risk=%d margin=%d)",
                float(capital_dec),
                float(price),
                float(sl_distance),
                lot_size,
                ses_label,
                lots_cap,
                lots_risk,
                lots_margin,
            )
        else:
            sl_risk = float(sl_distance) * qty * lot_size
            tp_goal = sl_risk * float(self._configured_rr)
            logger.info(
                "📊 %s | %d лот(ов) @ %.4f | R:R=%.2f | SL-риск=%.2f ₽ | TP-цель=%.2f ₽ "
                "(cap=%d risk=%d margin=%d)",
                ses_label,
                qty,
                float(price),
                self._configured_rr,
                sl_risk,
                tp_goal,
                lots_cap,
                lots_risk,
                lots_margin,
            )

        return max(qty, 0)

    # ── Геттеры ───────────────────────────────────────────────────────────────

    @property
    def day_pnl(self) -> Decimal:
        return self._day_pnl

    @property
    def week_pnl(self) -> Decimal:
        return self._week_pnl

    @property
    def portfolio_trades(self) -> int:
        return self._portfolio_trades

    @property
    def capital(self) -> Decimal:
        return self._capital

    @property
    def consecutive_losses(self) -> int:
        return self._consec_losses