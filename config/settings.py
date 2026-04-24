"""
config/settings.py
Читает .env и params.yaml. Единственная точка входа для всей конфигурации.
"""
import logging
import os
from pathlib import Path

import yaml
from dotenv import load_dotenv

# Загружаем .env из корня проекта
load_dotenv(Path(__file__).parent.parent / ".env")


def _require(key: str) -> str:
    val = os.environ.get(key)
    if not val:
        raise EnvironmentError(
            f"[settings] Переменная {key!r} не найдена в .env. "
            f"Проверьте .env.example для справки."
        )
    return val


# ── Tinkoff ──────────────────────────────────────────────────────────────────
TINKOFF_API_TOKEN: str = _require("TINKOFF_API_TOKEN")
TINKOFF_ACCOUNT_ID: str = _require("TINKOFF_ACCOUNT_ID")
TINKOFF_SANDBOX: bool = os.environ.get("TINKOFF_SANDBOX", "true").lower() == "true"

# ── Telegram ─────────────────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN: str = _require("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID: str = _require("TELEGRAM_CHAT_ID")

# ── Режим работы ─────────────────────────────────────────────────────────────
MODE: str = os.environ.get("MODE", "backtest").lower()

ALLOWED_MODES = {"backtest", "paper", "live"}
if MODE not in ALLOWED_MODES:
    raise ValueError(f"MODE={MODE!r} недопустим. Допустимы: {ALLOWED_MODES}")

# ── КРИТИЧЕСКАЯ ЗАЩИТА live-режима ───────────────────────────────────────────
if MODE == "live" and not TINKOFF_SANDBOX:
    logging.getLogger(__name__).critical(
        "\n" + "=" * 60 + "\n"
        "ОБНАРУЖЕН LIVE РЕЖИМ. ОСТАНОВИЛСЯ.\n"
        "Нужна ваша команда и явное подтверждение\n"
        "для перехода к реальной торговле.\n"
        "Запустите: python runner.py --mode live --confirm-live\n"
        + "=" * 60
    )


# ── Параметры стратегии и риска из YAML ──────────────────────────────────────
_PARAMS_PATH = Path(__file__).parent / "params.yaml"


def load_params() -> dict:
    with open(_PARAMS_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


PARAMS: dict = load_params()
