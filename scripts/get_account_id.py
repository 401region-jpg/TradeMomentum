"""
scripts/get_account_id.py
Получить Account ID через REST API (без tinkoff-invest-api).
Использование: python scripts\get_account_id.py
"""
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
load_dotenv(Path(__file__).parent.parent / ".env")

TOKEN   = os.environ.get("TINKOFF_API_TOKEN", "")
SANDBOX = os.environ.get("TINKOFF_SANDBOX", "true").lower() == "true"
BASE    = "https://invest-public-api.tinkoff.ru/rest"


async def main():
    if not TOKEN or TOKEN.startswith("t.ВСТАВЬТЕ"):
        print("ОШИБКА: заполните TINKOFF_API_TOKEN в .env")
        return

    import httpx

    headers = {
        "Authorization": f"Bearer {TOKEN}",
        "Content-Type":  "application/json",
        "accept":        "application/json",
    }

    print(f"Режим: {'SANDBOX' if SANDBOX else 'REAL'}")
    print("Подключаюсь к T-Invest REST API...\n")

    async with httpx.AsyncClient(timeout=15) as client:
        if SANDBOX:
            # Получаем sandbox-счета
            url  = f"{BASE}/tinkoff.public.invest.api.contract.v1.SandboxService/GetSandboxAccounts"
            resp = await client.post(url, json={}, headers=headers)

            if resp.status_code != 200:
                print(f"Ошибка API: {resp.status_code}")
                print(resp.text[:500])
                return

            data     = resp.json()
            accounts = data.get("accounts", [])

            if not accounts:
                print("Нет sandbox-счетов. Создаю...\n")
                url2  = f"{BASE}/tinkoff.public.invest.api.contract.v1.SandboxService/OpenSandboxAccount"
                resp2 = await client.post(url2, json={}, headers=headers)
                data2 = resp2.json()
                acc_id = data2.get("accountId", "")
                print(f"Создан sandbox-счёт: {acc_id}")
                print(f"\nДобавьте в .env:\nTINKOFF_ACCOUNT_ID={acc_id}")
                return

        else:
            url  = f"{BASE}/tinkoff.public.invest.api.contract.v1.UsersService/GetAccounts"
            resp = await client.post(url, json={}, headers=headers)

            if resp.status_code != 200:
                print(f"Ошибка API: {resp.status_code}")
                print(resp.text[:500])
                return

            data     = resp.json()
            accounts = data.get("accounts", [])

        if not accounts:
            print("Счета не найдены.")
            return

        print(f"{'ID':<25} {'Название':<30} {'Тип'}")
        print("-" * 70)
        for acc in accounts:
            print(f"{acc.get('id',''):<25} {acc.get('name',''):<30} {acc.get('type','')}")

        first_id = accounts[0].get("id", "")
        print(f"\nДобавьте в .env:")
        print(f"TINKOFF_ACCOUNT_ID={first_id}")


if __name__ == "__main__":
    asyncio.run(main())
