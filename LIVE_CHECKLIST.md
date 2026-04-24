# Pre-Live Checklist

## Strategy and Stability
- [ ] `run_paper` прогнан минимум на 2-4 неделях в разных рыночных фазах.
- [ ] Метрики стабильны: `WinRate`, `Profit Factor`, `max drawdown`.
- [ ] Нет необработанных исключений в `run_paper` и `run_live` в длительном прогоне.

## Risk Controls
- [ ] Проверены лимиты: `daily_loss_limit_pct`, `weekly_loss_limit_pct`.
- [ ] Проверены лимиты: `max_positions`, `max_trades_per_day`, cooldown.
- [ ] Размер позиции (`max_position_pct`, `max_leverage`) соответствует реальному депозиту.

## Live Execution Safety
- [ ] `--mode live --confirm-live --live-dry-run` отработал без ошибок.
- [ ] Логика входов/выходов в dry-run структурно совпадает с paper-flow.
- [ ] Закрытия по `SL/TP` подтверждены в логах на нескольких сессиях.

## Environment and Credentials
- [ ] Корректны `TINKOFF_API_TOKEN` и `TINKOFF_ACCOUNT_ID`.
- [ ] Осознанно выставлен `TINKOFF_SANDBOX` (`true`/`false`).
- [ ] Telegram уведомления приходят (`bot started`, `trade open/close`, `bot stopped`).

## Before Real Money
- [ ] Команда запуска проверена: `python runner.py --mode live --confirm-live`.
- [ ] Есть план аварийной остановки (Ctrl+C + проверка `bot stopped`).
- [ ] Зафиксирована версия конфига и кода, с которой стартует live.
