import os
import sys
import json
import subprocess
from pathlib import Path
from typing import Optional

import threading
import time

import flet as ft
import yaml
import pandas as pd
from datetime import datetime, timezone, timedelta

BASE_DIR = Path(__file__).parent
CONFIG_PATH = BASE_DIR / "config" / "params.yaml"
TRADES_DIR = BASE_DIR / "trades"
STATE_PATH = BASE_DIR / "state" / "bot_state.json"
RUNNER_PATH = BASE_DIR / "runner.py"

runner_process: Optional[subprocess.Popen] = None


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
    """Пробуем взять trades/live_log.csv, иначе последний CSV в trades."""
    live_log = TRADES_DIR / "live_log.csv"
    if live_log.exists():
        try:
            df = pd.read_csv(live_log)
            return df.tail(50)
        except Exception:
            pass

    if not TRADES_DIR.exists():
        return pd.DataFrame()
    csv_files = list(TRADES_DIR.glob("*.csv"))
    if not csv_files:
        return pd.DataFrame()
    latest = max(csv_files, key=os.path.getctime)
    try:
        df = pd.read_csv(latest)
    except Exception:
        return pd.DataFrame()
    return df.tail(50)


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


def main(page: ft.Page):
    global runner_process

    page.title = "Trading Bot Control"
    page.theme_mode = ft.ThemeMode.DARK
    page.bgcolor = ft.Colors.BLACK
    page.window_width = 1300
    page.window_height = 800
    page.padding = 20

    cfg = load_config()
    strategy_name = (
        cfg.get("strategy", {}).get("name")
        or cfg.get("name")
        or "Trading Bot"
    )
    capital_rub = cfg.get("risk", {}).get("capital_rub", "--")

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

    def update_ui():
        st = load_state()
        mode = st.get("mode", "--")
        equity = st.get("equity", "--")
        positions = st.get("positions", []) or []
        ts_raw = st.get("timestamp", "--")
        ts_msk = utc_to_moscow(ts_raw)

        state_text.value = f"Mode: {mode} | Equity: {equity} | Positions: {len(positions)}"
        ts_text.value = f"Updated: {ts_msk}"

        # trades
        df = load_last_trades()
        rows = []
        if not df.empty:
            # под твой формат: event, ticker, direction, qty, entry_price, exec_price, pnl, opened_at, timestamp
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

        # quotes
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

        # positions
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

    def auto_refresh_loop():
        while True:
            time.sleep(3)
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

    def on_backtest(e):
        _start_runner(["--mode", "backtest"], "Backtest")

    def on_paper(e):
        _start_runner(["--mode", "paper"], "Paper")

    def on_live_dry(e):
        _start_runner(
            ["--mode", "live", "--live-dry-run", "--confirm-live"],
            "LIVE-DRYRUN",
        )

    def on_live_real(e):
        _start_runner(
            ["--mode", "live", "--confirm-live"],
            "LIVE-REAL",
        )

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
            ft.Button(content=ft.Text("Backtest"), on_click=on_backtest),
            ft.Button(content=ft.Text("Paper"), on_click=on_paper),
            ft.Button(content=ft.Text("Live DRY"), on_click=on_live_dry),
            ft.Button(
                content=ft.Text("Live REAL"),
                style=ft.ButtonStyle(
                    bgcolor=ft.Colors.RED_700,
                    color=ft.Colors.WHITE,
                ),
                on_click=on_live_real,
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
            ft.Container(
                content=trades_table,
                expand=False,
                height=400,
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
                height=200,
                bgcolor=ft.Colors.with_opacity(0.1, ft.Colors.BLUE_GREY_900),
                padding=10,
                border_radius=5,
            ),
            ft.Text("Текущие позиции", size=18, color=ft.Colors.GREEN_200),
            ft.Container(
                content=positions_list,
                expand=False,
                height=300,
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

    page.run_thread(auto_refresh_loop)
    update_ui()


if __name__ == "__main__":
    ft.run(main)