from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Optional, Dict, Any

import os
import time
import logging

import requests

logger = logging.getLogger(__name__)


@dataclass
class FuturesContractSpec:
    """
    Спецификация фьючерса:
    - min_price_increment: шаг цены (Quotation -> Decimal)
    - min_price_increment_amount: стоимость шага цены в рублях (Quotation -> Decimal)
    - contract_size: размер базового актива (basic_asset_size, если есть)
    """
    ticker: str
    figi: Optional[str]
    class_code: Optional[str]
    min_price_increment: Decimal
    min_price_increment_amount: Decimal
    contract_size: Decimal


# ── Локальная база известных контрактов (можно пополнять) ────────────────────
# Значения для S1M6/SVM6 здесь примерные, лучше дать API собрать реальные.
LOCAL_CONTRACTS_BY_TICKER: Dict[str, FuturesContractSpec] = {
    "S1M6": FuturesContractSpec(
        ticker="S1M6",
        figi=None,
        class_code="SPBFUT",
        min_price_increment=Decimal("0.01"),
        min_price_increment_amount=Decimal("0.75"),  # TODO: уточнить/переписать из API
        contract_size=Decimal("1"),                  # TODO: уточнить basic_asset_size
    ),
    "SVM6": FuturesContractSpec(
        ticker="SVM6",
        figi=None,
        class_code="SPBFUT",
        min_price_increment=Decimal("0.01"),
        min_price_increment_amount=Decimal("7.5"),   # TODO: уточнить/переписать из API
        contract_size=Decimal("10"),                 # TODO: уточнить basic_asset_size
    ),
    "BTCUSDperpA": FuturesContractSpec(
        ticker="BTCUSDperpA",
        figi=None,              # можно будет заполнить после первого вызова API
        class_code="SPBFUT",    # для фьючей MOEX
        min_price_increment=Decimal("0.01"),        # шаг цены, 1 пункт = 0.01
        min_price_increment_amount=Decimal("0.01"), # стоимость 1 пункта = 0.01 ₽
        contract_size=Decimal("1"),                 # 1 контракт
    ),
}

# кэш загруженных из API контрактов
_CACHE_BY_TICKER: Dict[str, FuturesContractSpec] = {}
_CACHE_BY_FIGI: Dict[str, FuturesContractSpec] = {}
_CACHE_TTL_SEC = 60 * 30  # 30 минут

# для простоты — запомним время последнего обновления (грубо для всех)
_CACHE_LAST_UPDATE = 0.0


# ── Вспомогательные функции для Quotation/Decimal ────────────────────────────

def _quotation_to_decimal(q: dict | None) -> Decimal:
    """
    Преобразует Quotation {units, nano} в Decimal.
    """
    if not q:
        return Decimal("0")
    units = int(q.get("units", 0))
    nano = int(q.get("nano", 0))
    return Decimal(units) + (Decimal(nano) / Decimal(10**9))


# ── Вызовы Tinkoff Invest API (REST) ─────────────────────────────────────────

def _get_api_token(explicit_token: Optional[str] = None) -> Optional[str]:
    """
    Берём токен для T-Invest API:
    - сначала из параметра,
    - затем из переменной окружения TINKOFF_API_TOKEN.
    """
    if explicit_token:
        return explicit_token
    return os.getenv("TINKOFF_API_TOKEN")


def _rest_post(url: str, token: str, json_body: dict) -> dict:
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    resp = requests.post(url, headers=headers, json=json_body, timeout=10)
    resp.raise_for_status()
    return resp.json()


def _fetch_future_by_ticker(
    ticker: str,
    class_code: str,
    token: str,
) -> Optional[dict]:
    """
    FutureBy по тикеру + class_code. [web:123][web:221]

    POST https://invest-public-api.tbank.ru/rest/tinkoff.public.invest.api.contract.v1.InstrumentsService/FutureBy

    body: {
      "id_type": "INSTRUMENT_ID_TYPE_TICKER",
      "id": ticker,
      "class_code": class_code
    }
    """
    url = (
        "https://invest-public-api.tbank.ru/rest/"
        "tinkoff.public.invest.api.contract.v1.InstrumentsService/FutureBy"
    )
    body = {
        "id_type": "INSTRUMENT_ID_TYPE_TICKER",
        "id": ticker,
        "class_code": class_code,
    }
    try:
        data = _rest_post(url, token, body)
        return data.get("instrument") or data
    except Exception as e:
        logger.warning("FutureBy REST error for %s (%s): %s", ticker, class_code, e)
        return None


def _fetch_futures_margin(
    figi: str,
    token: str,
) -> Optional[dict]:
    """
    GetFuturesMargin для получения min_price_increment и min_price_increment_amount. [web:123][web:223]

    POST https://invest-public-api.tbank.ru/rest/tinkoff.public.invest.api.contract.v1.InstrumentsService/GetFuturesMargin

    body: {
      "figi": figi
    }
    """
    url = (
        "https://invest-public-api.tbank.ru/rest/"
        "tinkoff.public.invest.api.contract.v1.InstrumentsService/GetFuturesMargin"
    )
    body = {"figi": figi}
    try:
        data = _rest_post(url, token, body)
        return data
    except Exception as e:
        logger.warning("GetFuturesMargin REST error for figi=%s: %s", figi, e)
        return None


# ── Основная логика получения спецификации ───────────────────────────────────

def _build_spec_from_api_future_and_margin(
    ticker: str,
    future_data: dict,
    margin_data: Optional[dict],
) -> FuturesContractSpec:
    """
    Собираем FuturesContractSpec из FutureBy + GetFuturesMargin. [web:123][web:223]
    """
    figi = future_data.get("figi")
    class_code = future_data.get("class_code")

    # min_price_increment из future_data или из margin_data
    mpi = future_data.get("min_price_increment") or (margin_data or {}).get("min_price_increment")
    min_price_increment = _quotation_to_decimal(mpi)

    # стоимость шага min_price_increment_amount только из margin_data [web:223]
    mpi_amount_raw = (margin_data or {}).get("min_price_increment_amount")
    min_price_increment_amount = _quotation_to_decimal(mpi_amount_raw)

    # размер базового актива (basic_asset_size), если есть [web:123]
    bas_raw = future_data.get("basic_asset_size")
    contract_size = _quotation_to_decimal(bas_raw) if bas_raw else Decimal("1")

    return FuturesContractSpec(
        ticker=ticker,
        figi=figi,
        class_code=class_code,
        min_price_increment=min_price_increment,
        min_price_increment_amount=min_price_increment_amount,
        contract_size=contract_size,
    )


def get_futures_contract_spec(
    ticker: Optional[str] = None,
    figi: Optional[str] = None,
    class_code: str = "SPBFUT",
    token: Optional[str] = None,
    use_api: bool = True,
) -> Optional[FuturesContractSpec]:
    """
    Главная точка входа.

    - Сначала смотрим в кэш.
    - Потом в локальную таблицу LOCAL_CONTRACTS_BY_TICKER.
    - Если use_api=True и есть токен — подтягиваем через FutureBy+GetFuturesMargin. [web:123][web:221][web:223]
    - Результат кешируем и отдаём.

    Можно вызывать:
      get_futures_contract_spec(ticker="S1M6")
      get_futures_contract_spec(figi="...")

    class_code по умолчанию SPBFUT (фьючи MOEX).
    """
    global _CACHE_LAST_UPDATE

    token = _get_api_token(token)

    now = time.time()
    # грубый TTL для всех кэшей
    if now - _CACHE_LAST_UPDATE > _CACHE_TTL_SEC:
        _CACHE_BY_TICKER.clear()
        _CACHE_BY_FIGI.clear()
        _CACHE_LAST_UPDATE = now

    # 1) Попытка через FIGI
    if figi:
        if figi in _CACHE_BY_FIGI:
            return _CACHE_BY_FIGI[figi]
        # поиск в локальной базе
        for spec in LOCAL_CONTRACTS_BY_TICKER.values():
            if spec.figi == figi:
                _CACHE_BY_FIGI[figi] = spec
                if spec.ticker:
                    _CACHE_BY_TICKER.setdefault(spec.ticker, spec)
                return spec

    # 2) Попытка через ticker
    if ticker:
        if ticker in _CACHE_BY_TICKER:
            return _CACHE_BY_TICKER[ticker]

        if ticker in LOCAL_CONTRACTS_BY_TICKER:
            spec = LOCAL_CONTRACTS_BY_TICKER[ticker]
            _CACHE_BY_TICKER[ticker] = spec
            if spec.figi:
                _CACHE_BY_FIGI[spec.figi] = spec
            return spec

    # 3) Если нет токена или вырубили API — дальше не лезем
    if not use_api or not token:
        return None

    # 4) Пытаемся получить через FutureBy по тикеру
    if ticker:
        future_data = _fetch_future_by_ticker(ticker=ticker, class_code=class_code, token=token)
        if not future_data:
            return None

        figi_from_api = future_data.get("figi")
        margin_data = None
        if figi_from_api:
            margin_data = _fetch_futures_margin(figi=figi_from_api, token=token)

        spec = _build_spec_from_api_future_and_margin(
            ticker=ticker,
            future_data=future_data,
            margin_data=margin_data,
        )

        # кладём в кэш и локную базу (можно не класть в локную, если не хочешь)
        _CACHE_BY_TICKER[ticker] = spec
        if spec.figi:
            _CACHE_BY_FIGI[spec.figi] = spec
        return spec

    # 5) Если тикера нет, но есть FIGI — можно реализовать FutureBy по FIGI аналогично
    # (в REST нужен id_type=INSTRUMENT_ID_TYPE_FIGI, id=figi)
    if figi:
        # Реализация по FIGI по аналогии с тикером (если понадобится).
        # Пока опустим — тикер у тебя есть в config/params.yaml.
        return None

    return None


# Удобные шорткаты, если хочешь оставить старый интерфейс
def get_futures_contract_by_ticker(ticker: str) -> Optional[FuturesContractSpec]:
    return get_futures_contract_spec(ticker=ticker, figi=None, use_api=True)


def get_futures_contract_by_figi(figi: str) -> Optional[FuturesContractSpec]:
    return get_futures_contract_spec(ticker=None, figi=figi, use_api=True)

# В конец того же файла, где FuturesContractSpec / get_futures_contract_spec

def calc_pnl_rub_from_spec(
    spec: FuturesContractSpec,
    entry_price: float,
    exit_price: float,
    qty: int,
    side: str,        # "long" или "short"
    usd_rub_rate: float = 90.0,
) -> float:
    """
    PnL в рублях для фьючерса на BTC.

    Для BTCUSDperpA (USD-номинированный перп):
      pnl_usd = (exit - entry) * qty * sign
      pnl_rub = pnl_usd * usd_rub_rate

    Для чисто рублёвых фьючей можно считать через шаг цены:
      pnl_rub = (exit - entry) / min_price_increment * min_price_increment_amount * qty
    """
    sign = 1 if side == "long" else -1

    # Пример: для BTCUSDperpA считаем сразу в USD и умножаем на курс
    if spec.ticker == "BTCUSDperpA":
        pnl_usd = (exit_price - entry_price) * qty * sign
        return pnl_usd * usd_rub_rate

    # Общий случай через шаг цены
    ticks = (exit_price - entry_price) / float(spec.min_price_increment)
    pnl_rub = ticks * float(spec.min_price_increment_amount) * qty * sign
    return pnl_rub


def calc_pnl_rub(
    ticker: str,
    entry_price: float,
    exit_price: float,
    qty: int,
    side: str,
    usd_rub_rate: float = 90.0,
) -> float:
    """
    Обёртка: сам подтягивает spec через get_futures_contract_spec().
    """
    spec = get_futures_contract_spec(ticker=ticker)
    if spec is None:
        raise ValueError(f"Unknown instrument spec for ticker={ticker}")
    return calc_pnl_rub_from_spec(
        spec=spec,
        entry_price=entry_price,
        exit_price=exit_price,
        qty=qty,
        side=side,
        usd_rub_rate=usd_rub_rate,
    )