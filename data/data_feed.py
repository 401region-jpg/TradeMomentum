"""
data/data_feed.py
Загрузка котировок через T-Invest REST API.

BACKTEST DATE OVERRIDE:
  Поставьте BACKTEST_DATE = "2026-03-05" (или любой день)
  чтобы тестировать конкретный торговый день.
  Поставьте BACKTEST_DATE = None чтобы грузить 180 дней (стандартный бэктест).
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx
import pandas as pd

logger = logging.getLogger(__name__)

BASE_URL = "https://invest-public-api.tinkoff.ru/rest"

# ── Настройка дня для бэктеста ────────────────────────────────────────────────
# None = стандартный бэктест (180 дней)
# "YYYY-MM-DD" = тестируем только этот день
BACKTEST_DATE: Optional[str] = None   # например "2026-03-05"
# ─────────────────────────────────────────────────────────────────────────────

TIMEFRAME_MAP: dict[str, str] = {
    "1m":  "CANDLE_INTERVAL_1_MIN",
    "5m":  "CANDLE_INTERVAL_5_MIN",
    "15m": "CANDLE_INTERVAL_15_MIN",
    "1h":  "CANDLE_INTERVAL_HOUR",
    "4h":  "CANDLE_INTERVAL_4_HOUR",
    "1d":  "CANDLE_INTERVAL_DAY",
}

CHUNK_DAYS: dict[str, int] = {
    "1m": 1, "5m": 1, "15m": 3, "1h": 7, "4h": 30, "1d": 365
}


def _q(v: dict) -> float:
    units = int(v.get("units", 0))
    nano  = int(v.get("nano", 0))
    return units + nano / 1_000_000_000


class TinkoffRestClient:
    def __init__(self, token: str, sandbox: bool = True):
        self._token   = token
        self._sandbox = sandbox
        self._headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type":  "application/json",
            "accept":        "application/json",
        }

    def _url(self, service: str, method: str) -> str:
        return f"{BASE_URL}/tinkoff.public.invest.api.contract.v1.{service}/{method}"

    async def post(self, service: str, method: str, body: dict) -> dict:
        url = self._url(service, method)
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(url, json=body, headers=self._headers)
            if resp.status_code != 200:
                raise RuntimeError(
                    f"T-Invest API error {resp.status_code}: {resp.text[:300]}"
                )
            return resp.json()


class DataFeed(TinkoffRestClient):

    async def get_candles(
        self,
        figi: str,
        timeframe: str,
        from_dt: datetime,
        to_dt: Optional[datetime] = None,
    ) -> pd.DataFrame:

        # ── Date Override для бэктеста конкретного дня ────────────────────
        if BACKTEST_DATE is not None:
            year, month, day = [int(x) for x in BACKTEST_DATE.split("-")]
            from_dt = datetime(year, month, day, 0, 0, 0, tzinfo=timezone.utc)
            to_dt   = datetime(year, month, day, 23, 59, 59, tzinfo=timezone.utc)
            logger.info(
                "BACKTEST DATE OVERRIDE: %s — %s",
                from_dt.isoformat(), to_dt.isoformat()
            )
        elif to_dt is None:
            to_dt = datetime.now(tz=timezone.utc)
        # ─────────────────────────────────────────────────────────────────

        interval = TIMEFRAME_MAP.get(timeframe)
        if not interval:
            raise ValueError(
                f"Неизвестный таймфрейм: {timeframe}. "
                f"Допустимы: {list(TIMEFRAME_MAP)}"
            )

        chunk = CHUNK_DAYS.get(timeframe, 7)
        rows  = []
        cur   = from_dt

        while cur < to_dt:
            nxt = min(cur + timedelta(days=chunk), to_dt)
            logger.debug("Загружаю свечи %s %s — %s", figi, cur.date(), nxt.date())
            try:
                data = await self.post("MarketDataService", "GetCandles", {
                    "figi":     figi,
                    "from":     cur.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "to":       nxt.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "interval": interval,
                })
                for c in data.get("candles", []):
                    if c.get("isComplete", False):
                        rows.append({
                            "time":   c["time"],
                            "open":   _q(c["open"]),
                            "high":   _q(c["high"]),
                            "low":    _q(c["low"]),
                            "close":  _q(c["close"]),
                            "volume": int(c.get("volume", 0)),
                        })
            except Exception as e:
                logger.error("Ошибка загрузки свечей %s: %s", figi, e)
            cur = nxt

        if not rows:
            logger.warning("Нет данных для %s за указанный период", figi)
            return pd.DataFrame(
                columns=["time","open","high","low","close","volume"]
            )

        df = pd.DataFrame(rows)
        df["time"] = pd.to_datetime(df["time"], utc=True)
        df = (df.sort_values("time")
                .drop_duplicates("time")
                .reset_index(drop=True))
        logger.info(
            "Загружено %d свечей для %s (%s)", len(df), figi, timeframe
        )
        return df

    async def find_instrument(self, ticker: str) -> Optional[dict]:
        try:
            data = await self.post(
                "InstrumentsService", "FindInstrument", {
                    "query": ticker,
                    "instrumentKind": "INSTRUMENT_TYPE_FUTURES",
                    "apiTradeAvailableFlag": True,
                }
            )
            for item in data.get("instruments", []):
                logger.info(
                    "Найден: ticker=%s figi=%s uid=%s name=%s",
                    item.get("ticker"), item.get("figi"),
                    item.get("uid"),    item.get("name"),
                )
                if item.get("ticker") == ticker:
                    return {
                        "ticker":   item.get("ticker"),
                        "figi":     item.get("figi"),
                        "uid":      item.get("uid"),
                        "name":     item.get("name"),
                        "lot":      item.get("lot", 1),
                        "currency": item.get("currency", "rub"),
                    }
        except Exception as e:
            logger.error("Ошибка поиска %s: %s", ticker, e)
        return None

    async def stream_candles(
        self,
        figis: list[str],
        timeframe: str,
    ):
        """Потоковые свечи для paper/live через polling REST API."""
        import asyncio

        interval = TIMEFRAME_MAP.get(timeframe)
        if not interval:
            raise ValueError(f"Неизвестный таймфрейм: {timeframe}")

        # Интервал опроса в секундах (зависит от таймфрейма)
        poll_seconds = {
            "1m": 30, "5m": 60, "15m": 120, "1h": 300, "4h": 600
        }.get(timeframe, 60)

        last_times: dict[str, str] = {}

        logger.info(
            "Stream: опрос каждые %d сек для %d инструментов",
            poll_seconds, len(figis)
        )

        while True:
            for figi in figis:
                try:
                    now = datetime.now(tz=timezone.utc)
                    frm = now - timedelta(hours=2)
                    data = await self.post("MarketDataService", "GetCandles", {
                        "figi":     figi,
                        "from":     frm.strftime("%Y-%m-%dT%H:%M:%SZ"),
                        "to":       now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                        "interval": interval,
                    })
                    candles = data.get("candles", [])
                    if not candles:
                        continue

                    for c in candles:
                        t = c.get("time", "")
                        if t == last_times.get(figi):
                            continue
                        last_times[figi] = t
                        yield {
                            "figi":        figi,
                            "time":        t,
                            "open":        _q(c["open"]),
                            "high":        _q(c["high"]),
                            "low":         _q(c["low"]),
                            "close":       _q(c["close"]),
                            "volume":      int(c.get("volume", 0)),
                            "is_complete": c.get("isComplete", False),
                        }
                except Exception as e:
                    logger.error("Stream ошибка %s: %s", figi, e)

            await asyncio.sleep(poll_seconds)
