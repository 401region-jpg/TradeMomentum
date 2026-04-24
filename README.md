# Trading Bot для Т‑Инвест

Алгоритмический торговый бот для фьючерсов MOEX (Si, Brent, Silver и др.)
через официальный T‑Invest API. Стратегия: EMA Cross + RSI-фильтр + ATR-стопы.

---

## ⚠️ КРИТИЧЕСКИЕ ПРАВИЛА

1. **Никогда** не запускается в live без явного флага `--confirm-live`
2. Всегда начинайте с `backtest` → `paper` → только потом `live`
3. `.env` **никогда** не коммитится в git
4. Реальные токены из `.env` **никогда** не публикуются

---

## Структура проекта

```
trading-bot/
├── config/
│   ├── params.yaml          ← ВСЕ параметры стратегии и риска
│   └── settings.py          ← читает .env
├── data/
│   └── data_feed.py         ← исторические и потоковые котировки
├── strategy/
│   ├── base.py              ← абстрактный интерфейс Strategy
│   └── multi_signal.py      ← EMA Cross + RSI + ATR
├── risk/
│   └── risk_manager.py      ← все лимиты, размер позиции
├── broker/
│   ├── base.py              ← абстрактный BrokerClient
│   ├── broker_tinkoff.py    ← адаптер T-Invest API
│   └── broker_paper.py      ← paper-симулятор (без реальных ордеров)
├── notifications/
│   └── notifier_telegram.py ← все типы уведомлений
├── backtest/
│   ├── backtester.py        ← движок бэктеста (без API)
│   └── metrics.py           ← Sharpe, Sortino, PF, DD и др.
├── tests/
│   ├── conftest.py
│   ├── test_strategy.py
│   ├── test_risk_manager.py
│   └── test_backtester.py
├── scripts/
│   ├── get_account_id.py    ← получить Account ID
│   ├── get_chat_id.py       ← получить Telegram chat_id
│   └── fund_sandbox.py      ← пополнить sandbox-счёт
├── runner.py                ← точка входа
├── requirements.txt
├── .env.example
└── .gitignore
```

---

## Быстрый старт

### 1. Клонирование и зависимости

```bash
git clone <ваш-репо>
cd trading-bot
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### 2. Настройка .env

```bash
cp .env.example .env
# Откройте .env и заполните токены (см. инструкции ниже)
```

### 3. Получение Account ID

```bash
python scripts/get_account_id.py
# Скопируйте ID в .env → TINKOFF_ACCOUNT_ID=...
```

### 4. Получение Telegram chat_id

```bash
python scripts/get_chat_id.py
# Напишите боту в Telegram → получите chat_id → скопируйте в .env
```

### 5. Поиск FIGI инструментов

```bash
python runner.py --find-figi SiM6
python runner.py --find-figi BRM6
python runner.py --find-figi SVM6
# Вставьте FIGI в config/params.yaml
```

---

## Режимы запуска

### Backtest (на исторических данных, без API-ордеров)

```bash
python runner.py --mode backtest
```

- Загружает 180 дней исторических свечей через T-Invest API
- Прогоняет стратегию, считает PnL, метрики, equity-кривую
- Сохраняет сделки в `trades/backtest_*.csv`
- **Не отправляет ордеров**

### Paper Trading (реальные котировки, симулированные ордера)

```bash
python runner.py --mode paper
```

- Подписывается на живой поток котировок
- Моделирует сделки в памяти без реальных ордеров
- Отправляет уведомления в Telegram
- Сохраняет лог сделок в `trades/paper_*.csv`

### Live Trading (только после успешного paper-этапа)

```bash
# ⛔ ТОЛЬКО ПОСЛЕ ЯВНОГО РЕШЕНИЯ
# Сначала измените TINKOFF_SANDBOX=false в .env
python runner.py --mode live --confirm-live
```

---

## Конфигурация (config/params.yaml)

Все числовые параметры — только здесь:

```yaml
risk:
  capital_rub: 1000.0          # ваш капитал
  max_position_pct: 0.30       # макс. 30% на позицию
  max_leverage: 5              # плечо
  daily_loss_limit_pct: 0.05   # стоп-день при -5%
  weekly_loss_limit_pct: 0.10  # стоп-неделя при -10%
  max_trades_per_day: 10

strategy:
  ema_fast: 20
  ema_slow: 50
  rsi_period: 14
  atr_sl_multiplier: 1.5       # SL = 1.5 * ATR
  atr_tp_multiplier: 3.0       # TP = 3.0 * ATR → R:R = 2
```

---

## Тесты

```bash
pytest tests/ -v
```

Тесты запускаются **без** подключения к T-Invest API — только синтетические данные.

---

## Добавление новой стратегии

1. Создайте `strategy/my_strategy.py`, унаследуйте `Strategy`
2. Реализуйте `generate_signals()` и `add_indicators()`
3. В `runner.py` замените `MultiSignalStrategy` на вашу
4. Добавьте параметры в `config/params.yaml`

---

## Добавление нового брокера

1. Создайте `broker/broker_new.py`, унаследуйте `BrokerClient`
2. Реализуйте все абстрактные методы
3. Подключите в `runner.py`

---

## Важные предупреждения

- **С капиталом 1000 ₽ невозможно торговать фьючерсами** (ГО ≥ 5000 ₽ за контракт)
- Бот корректно считает размер позиции и вернёт `qty=0` — ордер не будет выставлен
- Рекомендуемый минимум для реальной торговли: **50 000–100 000 ₽**
- Всегда начинайте с sandbox и paper-режима перед live
