"""
scripts/fund_sandbox.py
Пополнить sandbox-счёт через REST API.
Использование: python scripts\fund_sandbox.py
"""
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
load_dotenv(Path(__file__).parent.parent / ".env")

TOKEN      = os.environ.get("TINKOFF_API_TOKEN", "")
ACCOUNT_ID = os.environ.get("TINKOFF_ACCOUNT_ID", "")
BASE       = "https://invest-public-api.tinkoff.ru/rest"
AMOUNT     = 500_000


async def main():
    if not TOKEN or not ACCOUNT_ID:
        print("Проверьте TINKOFF_API_TOKEN и TINKOFF_ACCOUNT_ID в .env")
        return

    import httpx

    headers = {
        "Authorization": f"Bearer {TOKEN}",
        "Content-Type":  "application/json",
        "accept":        "application/json",
    }

    url  = f"{BASE}/tinkoff.public.invest.api.contract.v1.SandboxService/SandboxPayIn"
    body = {
        "accountId": ACCOUNT_ID,
        "amount": {
            "currency": "rub",
            "units":    str(AMOUNT),
            "nano":     0,
        }
    }

    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(url, json=body, headers=headers)
        if resp.status_code != 200:
            print(f"Ошибка: {resp.status_code} — {resp.text[:300]}")
            return
        data    = resp.json()
        balance = data.get("balance", {})
        units   = int(balance.get("units", 0))
        print(f"Пополнено! Текущий баланс sandbox: {units:,} руб.")


if __name__ == "__main__":
    asyncio.run(main())
