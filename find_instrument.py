import asyncio
import httpx
import os
from datetime import datetime, timedelta, timezone

TOKEN = os.getenv("TINKOFF_API_TOKEN")
INSTRUMENT_ID = "BTCUSDperpA_SPBDMFUT"  # можно попробовать и BTCUSDPERP00, и uid

BASE_URL = "https://invest-public-api.tinkoff.ru/rest/tinkoff.public.invest.api.contract.v1"

async def get_candles():
    if not TOKEN:
        raise RuntimeError("Нет TINKOFF_API_TOKEN")

    headers = {
        "Authorization": f"Bearer {TOKEN}",
        "Content-Type": "application/json",
    }

    to_dt = datetime.now(timezone.utc)
    from_dt = to_dt - timedelta(days=3)

    payload = {
        "instrumentId": INSTRUMENT_ID,
        "from": from_dt.isoformat().replace("+00:00", "Z"),
        "to": to_dt.isoformat().replace("+00:00", "Z"),
        "interval": "CANDLE_INTERVAL_15_MIN",
        "candleSourceType": "CANDLE_SOURCE_EXCHANGE",
    }

    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.post(
            f"{BASE_URL}.MarketDataService/GetCandles",
            headers=headers,
            json=payload,
        )
        print("Status:", r.status_code)
        print("Body:", r.text)

if __name__ == "__main__":
    asyncio.run(get_candles())