import os
import sys
import json
import subprocess
from pathlib import Path
from typing import Optional

import asyncio
from datetime import datetime, timezone, timedelta

import flet as ft
import yaml
import pandas as pd


BASE_DIR = Path(__file__).parent
CONFIG_PATH = BASE_DIR / "config" / "params.yaml"
TRADES_DIR = BASE_DIR / "trades"
STATE_PATH = BASE_DIR / "state" / "bot_state.json"
RUNNER_PATH = BASE_DIR / "runner_gui.py"  # запускаем GUI-раннер

runner_process: Optional[subprocess.Popen] = None


# ── вспомогательные функции ──────────────────────────────────────────────────

def load_config() -> dict:
    if not CONFIG_PATH.exists():
        return {}
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            return yaml.safe_load(f)
    except Exception:
        return {}


def load_state() -> dict:
    if not STATE_PATH.exists():
        return {}
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def load_last_trades() -> pd.DataFrame:
    """Читаем именно online-лог trades/live_log.csv."""
    live_log = TRADES_DIR / "live_log.csv"
    if live_log.exists():
        try:
            df = pd.read_csv(live_log)
            return df.tail(50)
        except Exception:
            return pd.DataFrame()
    return pd.DataFrame()


def reset_live_log() -> None:
    """Очищаем trades/live_log.csv при старте GUI (новая сессия)."""
    live_log = TRADES_DIR / "live_log.csv"
    TRADES_DIR.mkdir(parents=True, exist_ok=True)
    if live_log.exists():
        try:
            live_log.unlink()
        except Exception:
            pass


def utc_to_tz_str(dt_utc: datetime, offset_hours: int, label: str) -> str:
    tz = timezone(timedelta(hours=offset_hours))
    dt_local = dt_utc.astimezone(tz)
    return dt_local.strftime(f"%Y-%m-%d %H:%M:%S {label}")


def utc_to_moscow(ts: str) -> str:
    if not ts or not isinstance(ts, str):
        return "--"
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        msktz = timezone(timedelta(hours=3))
        dt_msk = dt.astimezone(msktz)
        return dt_msk.strftime("%Y-%m-%d %H:%M:%S MSK")
    except Exception:
        return ts


# ── main ─────────────────────────────────────────────────────────────────────

def main(page: ft.Page):
    global runner_process

    # новая сессия — чистим онлайн-лог
    reset_live_log()

    page.title = "Trading Bot Control"
    page.theme_mode = ft.ThemeMode.DARK
    page.bgcolor = ft.Colors.BLACK
    page.window_width = 1400
    page.window_height = 850
    page.padding = 20

    cfg = load_config()
    strategy_name = (
        cfg.get("strategy", {}).get("name")
        or cfg.get("name")
        or "Trading Bot"
    )
    capital_rub = cfg.get("risk", {}).get("capital_rub", "--")

    # флаг песочницы (для подсветки REAL LIVE)
    tinkoff_sandbox = str(
        cfg.get("broker", {}).get("tinkoff_sandbox", os.getenv("TINKOFF_SANDBOX", "true"))
    ).lower() in ("1", "true", "yes")

    title = ft.Text(
        strategy_name,
        size=26,
        color=ft.Colors.GREEN_400,
        weight=ft.FontWeight.BOLD,
    )
    risk_text = ft.Text(
        f"Capital: {capital_rub} RUB",
        size=16,
        color=ft.Colors.CYAN_300,
    )

    state_text = ft.Text(
        "Mode: -- | Equity: -- | Positions: 0",
        size=18,
        color=ft.Colors.GREEN_300,
    )
    ts_text = ft.Text(
        "Updated: --",
        size=14,
        color=ft.Colors.GREY,
    )

    # индикатор реального лайва
    real_live_banner = ft.Text(
        "",
        size=16,
        color=ft.Colors.RED_400,
        weight=ft.FontWeight.BOLD,
    )

    # Часы мировых рынков
    clock_msk = ft.Text("MSK: --", size=14, color=ft.Colors.AMBER_200)
    clock_ldn = ft.Text("London: --", size=14, color=ft.Colors.AMBER_200)
    clock_ny = ft.Text("New York: --", size=14, color=ft.Colors.AMBER_200)
    clock_sh = ft.Text("Shanghai: --", size=14, color=ft.Colors.AMBER_200)

    clocks_row = ft.Row(
        [
            clock_msk,
            clock_ldn,
            clock_ny,
            clock_sh,
        ],
        spacing=20,
    )

    trades_table = ft.DataTable(
        columns=[
            ft.DataColumn(ft.Text("entry_time")),
            ft.DataColumn(ft.Text("exit_time")),
            ft.DataColumn(ft.Text("ticker")),
            ft.DataColumn(ft.Text("direction")),
            ft.DataColumn(ft.Text("qty")),
            ft.DataColumn(ft.Text("pnl (RUB)")),
        ],
        rows=[],
        column_spacing=10,
        bgcolor=ft.Colors.with_opacity(0.05, ft.Colors.GREEN_900),
    )

    quotes_list = ft.ListView(expand=1, spacing=5, padding=10)
    positions_list = ft.ListView(expand=2, spacing=5, padding=10)

    status = ft.Text("Ready", color=ft.Colors.AMBER_300)

    # --------- helpers ----------

    def update_ui():
        now_utc = datetime.now(tz=timezone.utc)

        # --- часы ---
        clock_msk.value = utc_to_tz_str(now_utc, 3, "MSK")
        clock_ldn.value = utc_to_tz_str(now_utc, 0, "LON")   # Лондон ~ UTC
        clock_ny.value = utc_to_tz_str(now_utc, -4, "NY")    # грубо UTC-4
        clock_sh.value = utc_to_tz_str(now_utc, 8, "SHA")    # Шанхай UTC+8

        # --- state.json ---
        st = load_state()
        mode = st.get("mode", "--")
        equity = st.get("equity", "--")
        positions = st.get("positions", []) or []
        ts_raw = st.get("timestamp", "--")
        ts_msk = utc_to_moscow(ts_raw)

        state_text.value = f"Mode: {mode} | Equity: {equity} | Positions: {len(positions)}"
        ts_text.value = f"Updated: {ts_msk}"

        # баннер REAL LIVE
        if mode == "live" and not tinkoff_sandbox:
            real_live_banner.value = "REAL LIVE: торгуем боевыми деньгами"
        elif mode == "live" and tinkoff_sandbox:
            real_live_banner.value = "LIVE (sandbox): торговля в песочнице"
        else:
            real_live_banner.value = ""

        # --- trades: live_log.csv ---
        df = load_last_trades()
        rows = []
        if not df.empty:
            for _, row in df.iterrows():
                entry_time = row.get("entry_time") or row.get("opened_at", "")
                exit_time = row.get("exit_time") or row.get("timestamp", "")
                pnl_val = row.get("pnl", 0)
                try:
                    pnl_str = f"{float(pnl_val):.2f} ₽"
                except Exception:
                    pnl_str = str(pnl_val)
                rows.append(
                    ft.DataRow(
                        cells=[
                            ft.DataCell(ft.Text(str(entry_time))),
                            ft.DataCell(ft.Text(str(exit_time))),
                            ft.DataCell(ft.Text(str(row.get("ticker", "")))),
                            ft.DataCell(ft.Text(str(row.get("direction", "")))),
                            ft.DataCell(ft.Text(str(row.get("qty", "")))),
                            ft.DataCell(ft.Text(pnl_str)),
                        ]
                    )
                )
        trades_table.rows = rows

        # --- quotes ---
        quotes_list.controls.clear()
        prices = st.get("prices") or st.get("quotes") or {}
        if isinstance(prices, dict) and prices:
            for ticker, price in prices.items():
                quotes_list.controls.append(
                    ft.Text(
                        f"{ticker}: {price}",
                        color=ft.Colors.CYAN_200,
                        size=14,
                    )
                )
        else:
            quotes_list.controls.append(
                ft.Text("Нет котировок в state", color=ft.Colors.GREY, size=14)
            )

        # --- positions ---
        positions_list.controls.clear()
        if positions:
            for p in positions:
                line = ft.Text(
                    f"{p.get('ticker', '')} | "
                    f"{p.get('side', p.get('direction',''))} | "
                    f"qty={p.get('qty', p.get('quantity', ''))} | "
                    f"entry={p.get('entry_price', p.get('avg_price', ''))} "
                    f"@ {p.get('entry_time', '')} "
                    f"SL={p.get('sl', '')} TP={p.get('tp', '')}",
                    color=ft.Colors.GREEN_200,
                    size=13,
                )
                positions_list.controls.append(line)
        else:
            positions_list.controls.append(
                ft.Text("Нет открытых позиций", color=ft.Colors.GREY, size=14)
            )

        page.update()

    async def auto_refresh():
        # 1 секунда — часы и state максимально живые
        while True:
            await asyncio.sleep(1)
            update_ui()

    def _start_runner(args_list: list[str], label: str):
        nonlocal status
        global runner_process

        if runner_process and runner_process.poll() is None:
            status.value = f"Уже запущен PID={runner_process.pid}"
            page.update()
            return

        env = os.environ.copy()
        cmd = [sys.executable, str(RUNNER_PATH)] + args_list
        try:
            creationflags = 0
            if os.name == "nt":
                creationflags = subprocess.CREATE_NO_WINDOW
            runner_process = subprocess.Popen(
                cmd,
                cwd=str(BASE_DIR),
                env=env,
                creationflags=creationflags,
            )
            status.value = f"{label} стартовал, PID={runner_process.pid}"
        except Exception as e:
            status.value = f"Ошибка запуска: {e}"
        page.update()

    def on_paper(e):
        _start_runner(["--mode", "paper"], "PAPER")

    def on_live(e):
        nonlocal status
        global runner_process

        if runner_process and runner_process.poll() is None:
            status.value = f"Уже запущен PID={runner_process.pid}"
            page.update()
            return

        # для LIVE подтверждаем real-режим для config.settings
        env = os.environ.copy()
        env["TRADER_LIVE_CONFIRMED"] = "true"

        cmd = [sys.executable, str(RUNNER_PATH), "--mode", "live", "--confirm-live"]

        try:
            creationflags = 0
            if os.name == "nt":
                creationflags = subprocess.CREATE_NO_WINDOW
            runner_process = subprocess.Popen(
                cmd,
                cwd=str(BASE_DIR),
                env=env,
                creationflags=creationflags,
            )
            status.value = f"LIVE стартовал, PID={runner_process.pid}"
        except Exception as e:
            status.value = f"Ошибка запуска LIVE: {e}"
        page.update()

    def on_stop(e):
        nonlocal status
        global runner_process

        if runner_process and runner_process.poll() is None:
            runner_process.terminate()
            status.value = "Runner остановлен"
        else:
            status.value = "Runner не запущен"
        page.update()

    def on_refresh(e=None):
        update_ui()

    buttons_row = ft.Row(
        [
            ft.Button(content=ft.Text("PAPER"), on_click=on_paper),
            ft.Button(
                content=ft.Text("LIVE"),
                style=ft.ButtonStyle(
                    bgcolor=ft.Colors.RED_700,
                    color=ft.Colors.WHITE,
                ),
                on_click=on_live,
            ),
            ft.Button(
                content=ft.Text("Stop"),
                style=ft.ButtonStyle(
                    bgcolor=ft.Colors.BLUE_GREY_700,
                    color=ft.Colors.WHITE,
                ),
                on_click=on_stop,
            ),
            ft.Button(content=ft.Text("Refresh"), on_click=on_refresh),
            real_live_banner,
        ],
        spacing=10,
        alignment=ft.MainAxisAlignment.START,
    )

    left_column = ft.Column(
        [
            title,
            risk_text,
            state_text,
            ts_text,
            clocks_row,
            ft.Container(
                content=trades_table,
                expand=False,
                height=420,
                margin=ft.Margin(top=10, right=10, bottom=10, left=0),
            ),
        ],
        expand=3,
    )

    right_column = ft.Column(
        [
            ft.Text("Котировки", size=18, color=ft.Colors.CYAN_200),
            ft.Container(
                content=quotes_list,
                expand=False,
                height=220,
                bgcolor=ft.Colors.with_opacity(0.1, ft.Colors.BLUE_GREY_900),
                padding=10,
                border_radius=5,
            ),
            ft.Text("Текущие позиции", size=18, color=ft.Colors.GREEN_200),
            ft.Container(
                content=positions_list,
                expand=False,
                height=320,
                bgcolor=ft.Colors.with_opacity(0.1, ft.Colors.GREEN_900),
                padding=10,
                border_radius=5,
            ),
            ft.Container(content=status, padding=10),
        ],
        expand=2,
    )

    main_row = ft.Row(
        [
            left_column,
            right_column,
        ],
        expand=True,
    )

    page.add(main_row, buttons_row)

    # авто-обновление раз в 1 секунду + стартовый снэпшот
    page.run_task(auto_refresh)
    update_ui()


if __name__ == "__main__":
    ft.run(main)