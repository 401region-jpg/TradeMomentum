from __future__ import annotations

import os
from pathlib import Path

import pandas as pd
import yaml
from rich.table import Table
from textual.app import App, ComposeResult
from textual.widgets import Static
from textual.containers import Horizontal, Vertical

# Пути к конфигу и папке трейдов
CONFIG_PATH = Path("config/params.yaml")
TRADES_DIR = Path("trades")


def _load_params() -> dict:
    """Читает config/params.yaml и возвращает dict."""
    if not CONFIG_PATH.exists():
        return {}
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def _find_last_trades_csv() -> Path | None:
    """Ищет последний CSV с трейдами из папки trades/."""
    if not TRADES_DIR.exists():
        return None
    files = sorted(
        list(TRADES_DIR.glob("paper_*.csv")) + list(TRADES_DIR.glob("live_*.csv"))
    )
    return files[-1] if files else None


def _detect_mode() -> str:
    """
    Определяет режим работы:

    1) Пытаемся прочитать MODE из config.settings (если есть).
    2) Если не получилось — берём MODE из переменной окружения.
    3) Если MODE=live и LIVE_DRY_RUN=true → показываем LIVE-DRY-RUN.
    """
    mode_raw = None

    # Попробуем взять MODE из config/settings.py (если у тебя такой модуль есть)
    try:
        from config import settings  # type: ignore

        mode_raw = getattr(settings, "MODE", None)
    except Exception:
        mode_raw = None

    if not mode_raw:
        # Фоллбек на переменную окружения
        mode_raw = os.environ.get("MODE", "").lower()

    live_dry = os.environ.get("LIVE_DRY_RUN", "false").lower() == "true"

    mode_raw = (mode_raw or "").lower()
    if mode_raw == "backtest":
        return "BACKTEST"
    if mode_raw == "paper":
        return "PAPER"
    if mode_raw == "live":
        return "LIVE-DRY-RUN" if live_dry else "LIVE"
    return "UNKNOWN"


class Header(Static):
    """Верхняя строка с инфоцыганским названием стратегии и режимом."""

    def on_mount(self) -> None:
        self.update(self._render_header())
        # Можно иногда обновлять (если поменяли MODE/LIVE_DRY_RUN)
        self.set_interval(5, self.refresh_header)

    def refresh_header(self) -> None:
        self.update(self._render_header())

    def _render_header(self) -> str:
        params = _load_params()
        risk = params.get("risk", {})
        strat = params.get("strategy", {})

        capital = risk.get("capital_rub", "?")
        max_trades = risk.get("max_trades_per_day", "?")
        sl = strat.get("atr_sl_multiplier", 1.0)
        tp = strat.get("atr_tp_multiplier", 3.0)
        rr = tp / sl if sl else 0

        mode = _detect_mode()

        # Инфоцыганское название стратегии — можно менять текст как хочешь
        title = (
            "[bold green]🚀 AI Momentum Engine v9[/bold green]  "
            "[bold](Institutional-Grade Intraday Trend Follower)[/bold]"
        )

        text = (
            f"{title}\n"
            f"[dim]R:R={rr:.2f} | Capital={capital} ₽ | "
            f"MaxTrades={max_trades} | Mode=[bold yellow]{mode}[/bold yellow][/dim]"
        )
        return text


class RiskPanel(Static):
    """Сводка по риску и конфигу из params.yaml."""

    def on_mount(self) -> None:
        self.update(self._render_table())
        self.set_interval(5, self.refresh_panel)

    def refresh_panel(self) -> None:
        self.update(self._render_table())

    def _render_table(self) -> Table:
        params = _load_params()
        risk = params.get("risk", {})
        strat = params.get("strategy", {})
        sessions = risk.get("sessions", {})

        table = Table(title="[bold]Risk & Config[/bold]", show_header=False, box=None)

        capital = risk.get("capital_rub", "?")
        max_trades = risk.get("max_trades_per_day", "?")
        max_lev = risk.get("max_leverage", "?")
        day_lim = risk.get("daily_loss_limit_pct", 0) * 100
        week_lim = risk.get("weekly_loss_limit_pct", 0) * 100

        sl = strat.get("atr_sl_multiplier", 1.0)
        tp = strat.get("atr_tp_multiplier", 3.0)
        rr = tp / sl if sl else 0

        gt = strat.get("global_trend", {})
        gt_enabled = gt.get("enabled", False)
        gt_lock = gt.get("strict_lock", True)

        table.add_row("Capital", f"[bold]{capital} ₽[/bold]")
        table.add_row("Max trades / day", str(max_trades))
        table.add_row("Max leverage", str(max_lev))
        table.add_row("Daily loss limit", f"{day_lim:.1f}%")
        table.add_row("Weekly loss limit", f"{week_lim:.1f}%")
        table.add_row("R:R (tp/sl)", f"{rr:.2f} ({tp} / {sl})")
        table.add_row("Global trend filter", "ON" if gt_enabled else "OFF")
        if gt_enabled:
            table.add_row("Trend lock", "STRICT" if gt_lock else "SOFT")

        # Сессии (normal / boost / evening)
        if sessions:
            table.add_row("", "")
            table.add_row("[bold]Sessions[/bold]", "")
            for name in ("normal", "boost", "evening"):
                s = sessions.get(name)
                if not s:
                    continue
                pct = s.get("max_position_pct", risk.get("max_position_pct", 0)) * 100
                label = s.get("label", name.upper())
                table.add_row(label, f"{pct:.1f}%")

        return table


class TradesPanel(Static):
    """Последние сделки из последнего CSV в trades/."""

    def on_mount(self) -> None:
        self.update(self._render_table())
        self.set_interval(5, self.refresh_panel)

    def refresh_panel(self) -> None:
        self.update(self._render_table())

    def _render_table(self) -> Table:
        csv_path = _find_last_trades_csv()
        table = Table(
            title="[bold]Last Trades[/bold]",
            show_header=True,
            header_style="bold green",
        )
        table.add_column("Time", no_wrap=True)
        table.add_column("Ticker")
        table.add_column("Dir")
        table.add_column("Qty", justify="right")
        table.add_column("Entry", justify="right")
        table.add_column("Exit", justify="right")
        table.add_column("PnL", justify="right")
        table.add_column("Reason")

        if not csv_path:
            table.add_row("-", "-", "-", "-", "-", "-", "-", "No trades CSV yet")
            return table

        try:
            df = pd.read_csv(csv_path)
        except Exception as e:
            table.add_row("-", "-", "-", "-", "-", "-", "-", f"Error: {e}")
            return table

        # Пытаемся угадать колонку времени
        time_col = None
        for c in ("timestamp", "closed_at", "time", "closed_time"):
            if c in df.columns:
                time_col = c
                break

        for _, row in df.tail(15).iterrows():
            t = row.get(time_col, "") if time_col else ""
            ticker = row.get("ticker", "")
            direction = row.get("direction", row.get("side", ""))
            qty = row.get("qty", row.get("quantity", ""))
            entry = row.get("entry_price", row.get("avg_price", ""))
            exit_p = row.get("exit_price", row.get("exec_price", ""))
            pnl = row.get("pnl", "")
            reason = row.get("reason", row.get("event", ""))

            def _fmt(x):
                return f"{x:.2f}" if isinstance(x, (int, float, float)) else str(x)

            table.add_row(
                str(t),
                str(ticker),
                str(direction),
                str(qty),
                _fmt(entry),
                _fmt(exit_p),
                _fmt(pnl),
                str(reason),
            )

        return table


class BotDashboard(App):
    """Главное TUI‑приложение (экран)."""

    CSS = """
    Screen {
        background: #000000;
        color: #C8FFC8;
    }
    #header {
        padding: 1;
        border: tall #00AA00;
    }
    #risk {
        padding: 1;
        border: tall #006600;
    }
    #trades {
        padding: 1;
        border: tall #006600;
    }
    """

    BINDINGS = [
        ("q", "quit", "Quit"),
        ("r", "refresh", "Refresh now"),
    ]

    def compose(self) -> ComposeResult:
        """Описываем layout: сверху хедер, ниже две панели рядом."""
        yield Header(id="header")
        with Horizontal():
            with Vertical():
                yield RiskPanel(id="risk")
            with Vertical():
                yield TradesPanel(id="trades")

    def action_refresh(self) -> None:
        """Ручное обновление по нажатию 'r'."""
        for w in self.query(Static):
            if hasattr(w, "refresh_panel"):
                w.refresh_panel()


if __name__ == "__main__":
    BotDashboard().run()