import os
import glob
import pandas as pd

TRADES_DIR = r"C:\invest\trading-bot\trades"

def load_all_trades(trades_dir: str) -> pd.DataFrame:
    pattern = os.path.join(trades_dir, "*.csv")
    files = glob.glob(pattern)
    dfs = []
    for f in files:
        try:
            df = pd.read_csv(f)
            df["source_file"] = os.path.basename(f)
            dfs.append(df)
        except Exception as e:
            print(f"Ошибка при чтении {f}: {e}")
    if not dfs:
        return pd.DataFrame()
    return pd.concat(dfs, ignore_index=True)

def calc_stats(df: pd.DataFrame) -> None:
    if df.empty:
        print("Нет сделок.")
        return

    if "pnl" not in df.columns:
        print("В CSV нет колонки 'pnl'. Проверь структуру файлов.")
        return

    pnl = df["pnl"]
    wins = pnl[pnl > 0]
    losses = pnl[pnl < 0]

    total_trades = len(pnl)
    win_trades = len(wins)
    loss_trades = len(losses)

    win_rate = win_trades / total_trades if total_trades > 0 else 0.0
    avg_win = wins.mean() if win_trades > 0 else 0.0
    avg_loss = -losses.mean() if loss_trades > 0 else 0.0  # модуль

    expectancy = win_rate * avg_win - (1 - win_rate) * avg_loss

    gross_profit = wins.sum()
    gross_loss = -losses.sum()
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else float("inf")

    print(f"Всего сделок: {total_trades}")
    print(f"Побед: {win_trades} | Убытков: {loss_trades}")
    print(f"WinRate: {win_rate*100:.1f}%")
    print(f"AvgWin: {avg_win:.2f}  AvgLoss: {avg_loss:.2f}")
    print(f"Expectancy: {expectancy:.2f} руб/сделку")
    print(f"Profit Factor: {profit_factor:.3f}")
    print(f"ΣPnl: {pnl.sum():.2f} руб")

if __name__ == "__main__":
    df_all = load_all_trades(TRADES_DIR)
    calc_stats(df_all)