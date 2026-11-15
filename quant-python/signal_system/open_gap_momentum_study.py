"""开盘涨幅动量策略回测（研究脚本，不接生产）。

预注册口径（2026-11-14 首次运行前固定）
--------------------------------------
股票池
    A 股 qfq 日线（baostock 2018-2024 + stock_pit 2025-2026 + 退市股），
    剔除北交所（4/8/92）与 B 股（200/900）；当日有成交，开盘/收盘/前收有效，
    至少 21 根历史 K 线。ST 历史标记不可得，未过滤。

信号（开盘时已知）
    open_gap = open_t / close_{t-1} - 1 <= 4%
    yest_ret = close_{t-1} / close_{t-2} - 1 <= 6%
    ret20    = close_{t-1} / close_{t-21} - 1 >= 0%
    按 open_gap 降序，每天最多 2 只，symbol 升序打破平局。

执行
    当日开盘价买入。主口径 T+1 次日开盘卖出（open_1）。
    另测 open_3/open_5/open_10/open_20 与 close_1/close_5（均在 T+1 之后）。

成本
    佣金 3bp/边，卖出印花税 10bp，滑点 10bp/边；压力测试 20bp/边。

组合
    100 万，2 个半仓槽位，100 股整手；主口径每日轮动。
    同时报告分数股口径的每日等权信号组合（纯策略收益，不含整手约束）。

本脚本是研究代码，不修改生产配置、不写 paper ledger、不自动交易。
"""

from __future__ import annotations

import argparse
import json
import math
import pickle
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from quality_filter_study import extend_delisted_panels, extend_panels
from smallcap_strategy_study import Config, load_panels

BASE_DIR = Path(__file__).resolve().parent
PIT_DIR = BASE_DIR / "cache" / "stock_pit"
PANEL_CACHE = BASE_DIR / "cache" / "open_gap_study" / "panels_2018_2026.pkl"
INDEX_399101 = BASE_DIR / "cache" / "smallcap_study" / "index_399101.csv"
DEFAULT_OUT = (
    BASE_DIR.parent.parent
    / "docs"
    / "backtest-results"
    / "2026-11-14-open-gap-momentum"
)

BOARD_LABELS = {
    "000": "深主板",
    "001": "深主板",
    "002": "中小板",
    "003": "深主板",
    "300": "创业板",
    "301": "创业板",
    "600": "沪主板",
    "601": "沪主板",
    "603": "沪主板",
    "605": "沪主板",
    "688": "科创板",
    "689": "科创板",
}


@dataclass(frozen=True)
class StudyConfig:
    max_gap: float = 0.04
    max_yest: float = 0.06
    min_ret20: float = 0.0
    max_picks: int = 2
    min_history: int = 21
    horizons: tuple[int, ...] = (1, 3, 5, 10, 20)
    close_horizons: tuple[int, ...] = (1, 5)
    initial_capital: float = 1_000_000.0
    lot: int = 100
    commission: float = 0.0003
    stamp_tax: float = 0.0010
    slippage: float = 0.0010
    stress_slippage: float = 0.0020


# ---------------------------------------------------------------------------
# 数据
# ---------------------------------------------------------------------------


def add_new_listings(
    panels: dict[str, pd.DataFrame], price_dir: Path
) -> dict[str, pd.DataFrame]:
    """把 2025 年后新上市、不在 baostock 面板里的 qfq 序列加为列。

    ``extend_panels`` 只遍历已有列，因此 2025-2026 新上市股票不会被它加入。
    新上市没有 2024-12-31 锚点，不能做拼接缩放；但序列内部日收益仍然有效。
    """

    existing = set(map(str, panels["close"].columns))
    frames: list[pd.DataFrame] = []
    for path in sorted(price_dir.glob("*.csv")):
        symbol = path.stem
        if symbol in existing:
            continue
        frame = pd.read_csv(path)
        date_column = "date" if "date" in frame.columns else frame.columns[0]
        frame["datetime"] = pd.to_datetime(frame[date_column], errors="coerce")
        for column in ("open", "high", "low", "close", "volume", "amount"):
            if column in frame.columns:
                frame[column] = pd.to_numeric(frame[column], errors="coerce")
        if "turnover" in frame.columns:
            frame["turnover_rate"] = (
                pd.to_numeric(frame["turnover"], errors="coerce") * 100.0
            )
        else:
            frame["turnover_rate"] = np.nan
        frame = frame[frame["datetime"] >= pd.Timestamp("2025-01-01")]
        frame = frame.dropna(subset=["datetime", "close"])
        if frame.empty:
            continue
        frame["symbol"] = symbol
        frames.append(
            frame[
                [
                    "datetime",
                    "symbol",
                    "open",
                    "high",
                    "low",
                    "close",
                    "volume",
                    "amount",
                    "turnover_rate",
                ]
            ]
        )
    if not frames:
        return panels

    long = pd.concat(frames, ignore_index=True)
    extended = dict(panels)
    for column in ("open", "high", "low", "close", "volume", "amount", "turnover_rate"):
        extra = long.pivot(index="datetime", columns="symbol", values=column)
        extra = extra.reindex(index=panels[column].index)
        extended[column] = pd.concat([panels[column], extra], axis=1)
    return extended


def load_study_panels(
    cache_path: Path = PANEL_CACHE, rebuild: bool = False
) -> dict[str, pd.DataFrame]:
    if cache_path.exists() and not rebuild:
        with cache_path.open("rb") as handle:
            return pickle.load(handle)

    base_cfg = Config(start="2018-01-01", end="2026-10-30", min_history=250)
    panels = load_panels(
        BASE_DIR / "cache" / "baostock_history_20261026",
        BASE_DIR / "cache" / "smallcap_study" / "panels_2018_2024.pkl",
        base_cfg,
    )
    panels = extend_panels(
        panels,
        PIT_DIR / "prices_qfq",
        list(panels["close"].columns),
        "2025-01-01",
    )
    panels = add_new_listings(panels, PIT_DIR / "prices_qfq")
    panels = extend_delisted_panels(panels, PIT_DIR / "prices_delisted")

    for column, frame in list(panels.items()):
        frame = frame.sort_index()
        frame = frame.loc[~frame.index.duplicated(keep="last")]
        if frame.columns.duplicated().any():
            frame = frame.loc[:, ~frame.columns.duplicated(keep="last")]
        panels[column] = frame

    cache_path.parent.mkdir(parents=True, exist_ok=True)
    with cache_path.open("wb") as handle:
        pickle.dump(panels, handle, protocol=5)
    return panels


# ---------------------------------------------------------------------------
# 信号
# ---------------------------------------------------------------------------


def build_signal_table(
    panels: dict[str, pd.DataFrame], cfg: StudyConfig
) -> dict[str, Any]:
    symbols = np.array([str(value) for value in panels["close"].columns])
    excluded = np.array(
        [symbol.startswith(("4", "8", "92", "200", "900")) for symbol in symbols],
        dtype=bool,
    )
    keep = ~excluded
    symbols = symbols[keep]

    close = panels["close"].loc[:, keep].astype("float64")
    open_ = panels["open"].loc[:, keep].astype("float64")
    volume = panels["volume"].loc[:, keep].astype("float64")

    dates = close.index
    prev1 = close.shift(1)
    prev2 = close.shift(2)
    prev21 = close.shift(21)
    open_gap = open_ / prev1 - 1.0
    yest_ret = prev1 / prev2 - 1.0
    ret20 = prev1 / prev21 - 1.0

    history = close.notna().cumsum()
    valid = (
        open_.notna()
        & close.notna()
        & (volume > 0)
        & (history >= cfg.min_history)
    )
    mask = (
        valid
        & (open_gap <= cfg.max_gap + 1e-12)
        & (yest_ret <= cfg.max_yest + 1e-12)
        & (ret20 >= cfg.min_ret20 - 1e-12)
    )

    return {
        "dates": dates,
        "symbols": symbols,
        "open": open_,
        "close": close,
        "volume": volume,
        "open_gap": open_gap,
        "yest_ret": yest_ret,
        "ret20": ret20,
        "mask": mask.to_numpy(dtype=bool),
    }


def select_signals(signal: dict[str, Any], cfg: StudyConfig) -> pd.DataFrame:
    dates = signal["dates"]
    symbols = signal["symbols"]
    open_ = signal["open"].to_numpy(dtype="float64")
    open_gap = signal["open_gap"].to_numpy(dtype="float64")
    yest_ret = signal["yest_ret"].to_numpy(dtype="float64")
    ret20 = signal["ret20"].to_numpy(dtype="float64")
    mask = signal["mask"]

    records: list[dict[str, Any]] = []
    for index in range(len(dates)):
        candidates = np.flatnonzero(mask[index])
        if candidates.size == 0:
            continue
        order = np.lexsort((symbols[candidates], -open_gap[index, candidates]))
        for column in candidates[order[: cfg.max_picks]]:
            records.append(
                {
                    "entry_idx": int(index),
                    "entry_date": dates[index],
                    "symbol": str(symbols[column]),
                    "entry_open": float(open_[index, column]),
                    "open_gap": float(open_gap[index, column]),
                    "yest_ret": float(yest_ret[index, column]),
                    "ret20": float(ret20[index, column]),
                }
            )
    return pd.DataFrame(records)


def next_open_net(
    open_array: np.ndarray, slippage: float, cfg: StudyConfig
) -> np.ndarray:
    exit_open = np.full_like(open_array, np.nan)
    exit_open[:-1] = open_array[1:]
    return (
        exit_open
        * (1.0 - slippage)
        * (1.0 - cfg.commission - cfg.stamp_tax)
        / (open_array * (1.0 + slippage) * (1.0 + cfg.commission))
        - 1.0
    )


def candidate_diagnostics(
    signal: dict[str, Any], cfg: StudyConfig
) -> dict[str, Any]:
    open_ = signal["open"].to_numpy(dtype="float64")
    net = next_open_net(open_, cfg.slippage, cfg)
    ok = signal["mask"] & np.isfinite(net)
    values = net[ok]
    if values.size == 0:
        return {"candidate_rows": 0}
    per_day = signal["mask"].sum(axis=1)
    return {
        "candidate_rows": int(values.size),
        "candidate_days": int((per_day > 0).sum()),
        "avg_candidates_per_day": round(float(per_day.mean()), 2),
        "max_candidates_per_day": int(per_day.max()),
        "next_open_net_mean_pct": round(float(values.mean()) * 100.0, 4),
        "next_open_net_median_pct": round(float(np.median(values)) * 100.0, 4),
        "next_open_net_win_rate_pct": round(float((values > 0).mean()) * 100.0, 2),
    }


# ---------------------------------------------------------------------------
# 交易与绩效
# ---------------------------------------------------------------------------


def build_trades(
    selected: pd.DataFrame,
    signal: dict[str, Any],
    horizon: int,
    exit_mode: str,
    cfg: StudyConfig,
    slippage: float | None = None,
) -> pd.DataFrame:
    if selected.empty:
        return pd.DataFrame()

    dates = signal["dates"]
    open_ = signal["open"].to_numpy(dtype="float64")
    close = signal["close"].to_numpy(dtype="float64")
    column_index = {
        str(symbol): index for index, symbol in enumerate(signal["symbols"])
    }
    slip = cfg.slippage if slippage is None else slippage

    rows: list[dict[str, Any]] = []
    for record in selected.itertuples(index=False):
        exit_index = int(record.entry_idx) + horizon
        if exit_index >= len(dates):
            continue
        column = column_index[record.symbol]
        exit_price = (
            open_[exit_index, column]
            if exit_mode == "open"
            else close[exit_index, column]
        )
        if not math.isfinite(exit_price) or exit_price <= 0:
            continue
        entry_price = float(record.entry_open)
        gross = exit_price / entry_price - 1.0
        net = (
            exit_price
            * (1.0 - slip)
            * (1.0 - cfg.commission - cfg.stamp_tax)
            / (entry_price * (1.0 + slip) * (1.0 + cfg.commission))
            - 1.0
        )
        rows.append(
            {
                "entry_idx": int(record.entry_idx),
                "exit_idx": int(exit_index),
                "entry_date": dates[int(record.entry_idx)],
                "exit_date": dates[exit_index],
                "symbol": record.symbol,
                "entry_open": entry_price,
                "exit_price": float(exit_price),
                "open_gap_pct": float(record.open_gap) * 100.0,
                "yest_ret_pct": float(record.yest_ret) * 100.0,
                "ret20_pct": float(record.ret20) * 100.0,
                "hold_sessions": horizon,
                "exit_mode": exit_mode,
                "gross_return": gross,
                "net_return": net,
            }
        )
    return pd.DataFrame(rows)


def stats_from_trades(trades: pd.DataFrame, label: str) -> dict[str, Any]:
    if trades.empty:
        return {"label": label, "trades": 0}

    net = trades["net_return"].to_numpy(dtype="float64")
    gross = trades["gross_return"].to_numpy(dtype="float64")
    daily = trades.groupby("entry_date")["net_return"].mean().sort_index()
    curve = (1.0 + daily).cumprod()
    final = float(curve.iloc[-1])
    days = int(len(daily))
    total = final - 1.0
    cagr = (final ** (252.0 / days) - 1.0) if days and final > 0 else -1.0
    daily_std = float(daily.std(ddof=1)) if days > 1 else 0.0
    vol = daily_std * math.sqrt(252.0)
    sharpe = (
        float(daily.mean() / daily_std * math.sqrt(252.0)) if daily_std > 0 else 0.0
    )
    values = np.r_[1.0, curve.to_numpy(dtype="float64")]
    peaks = np.maximum.accumulate(values)
    drawdown = float((values / peaks - 1.0).min())
    t_daily = (
        float(daily.mean() / (daily_std / math.sqrt(days)))
        if days > 1 and daily_std > 0
        else 0.0
    )
    net_std = float(net.std(ddof=1)) if len(net) > 1 else 0.0
    t_trade = (
        float(net.mean() / (net_std / math.sqrt(len(net))))
        if len(net) > 1 and net_std > 0
        else 0.0
    )
    wins = net[net > 0]
    losses = net[net < 0]
    profit_factor = (
        float(wins.sum() / abs(losses.sum())) if losses.size and losses.sum() != 0 else None
    )

    return {
        "label": label,
        "trades": int(len(net)),
        "days": days,
        "avg_picks_per_day": round(float(len(net) / days), 3) if days else 0.0,
        "gross_mean_pct": round(float(gross.mean()) * 100.0, 4),
        "net_mean_pct": round(float(net.mean()) * 100.0, 4),
        "net_median_pct": round(float(np.median(net)) * 100.0, 4),
        "net_std_pct": round(net_std * 100.0, 4),
        "win_rate_pct": round(float((net > 0).mean()) * 100.0, 2),
        "avg_win_pct": round(float(wins.mean()) * 100.0, 4) if wins.size else 0.0,
        "avg_loss_pct": round(float(losses.mean()) * 100.0, 4) if losses.size else 0.0,
        "profit_factor": round(profit_factor, 4) if profit_factor is not None else None,
        "best_pct": round(float(net.max()) * 100.0, 4),
        "worst_pct": round(float(net.min()) * 100.0, 4),
        "t_stat_trade": round(t_trade, 3),
        "t_stat_daily": round(t_daily, 3),
        "total_return_pct": round(total * 100.0, 4),
        "cagr_pct": round(cagr * 100.0, 4),
        "annual_vol_pct": round(vol * 100.0, 4),
        "sharpe": round(sharpe, 4),
        "max_drawdown_pct": round(drawdown * 100.0, 4),
    }


def equity_stats(equity: pd.Series, label: str) -> dict[str, Any]:
    equity = equity.dropna()
    if len(equity) < 2:
        return {"label": label}
    daily = equity.pct_change().dropna()
    days = int(len(daily))
    final_ratio = float(equity.iloc[-1] / equity.iloc[0])
    total = final_ratio - 1.0
    cagr = (final_ratio ** (252.0 / days) - 1.0) if days and final_ratio > 0 else -1.0
    daily_std = float(daily.std(ddof=1)) if days > 1 else 0.0
    vol = daily_std * math.sqrt(252.0)
    sharpe = (
        float(daily.mean() / daily_std * math.sqrt(252.0)) if daily_std > 0 else 0.0
    )
    drawdown = float((equity / equity.cummax() - 1.0).min())
    return {
        "label": label,
        "days": days,
        "total_return_pct": round(total * 100.0, 4),
        "cagr_pct": round(cagr * 100.0, 4),
        "annual_vol_pct": round(vol * 100.0, 4),
        "sharpe": round(sharpe, 4),
        "max_drawdown_pct": round(drawdown * 100.0, 4),
    }


def yearly_table(trades: pd.DataFrame) -> pd.DataFrame:
    if trades.empty:
        return pd.DataFrame()
    frame = trades.copy()
    frame["year"] = frame["entry_date"].dt.year
    rows: list[dict[str, Any]] = []
    for year, group in frame.groupby("year"):
        net = group["net_return"].to_numpy(dtype="float64")
        rows.append(
            {
                "year": int(year),
                "trades": int(len(net)),
                "net_mean_pct": round(float(net.mean()) * 100.0, 4),
                "net_median_pct": round(float(np.median(net)) * 100.0, 4),
                "win_rate_pct": round(float((net > 0).mean()) * 100.0, 2),
            }
        )
    return pd.DataFrame(rows)


def board_table(trades: pd.DataFrame) -> pd.DataFrame:
    if trades.empty:
        return pd.DataFrame()
    frame = trades.copy()
    frame["board"] = frame["symbol"].str[:3].map(BOARD_LABELS).fillna("其他")
    rows: list[dict[str, Any]] = []
    for board, group in frame.groupby("board"):
        net = group["net_return"].to_numpy(dtype="float64")
        rows.append(
            {
                "board": board,
                "trades": int(len(net)),
                "net_mean_pct": round(float(net.mean()) * 100.0, 4),
                "win_rate_pct": round(float((net > 0).mean()) * 100.0, 2),
            }
        )
    return pd.DataFrame(rows).sort_values("trades", ascending=False)


def simulate_two_slot_portfolio(
    selected: pd.DataFrame,
    signal: dict[str, Any],
    horizon: int,
    cfg: StudyConfig,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """100 万、2 个半仓槽位的整手组合；开盘卖出/买入，收盘估值。"""

    dates = signal["dates"]
    open_ = signal["open"].to_numpy(dtype="float64")
    close = signal["close"].to_numpy(dtype="float64")
    column_index = {
        str(symbol): index for index, symbol in enumerate(signal["symbols"])
    }

    by_entry: dict[int, list[Any]] = {}
    for record in selected.itertuples(index=False):
        by_entry.setdefault(int(record.entry_idx), []).append(record)
    for records in by_entry.values():
        records.sort(key=lambda item: (-float(item.open_gap), str(item.symbol)))

    cash = float(cfg.initial_capital)
    positions: list[dict[str, Any]] = []
    rows: list[dict[str, Any]] = []
    trade_returns: list[float] = []
    start = min(by_entry) if by_entry else 0

    for index in range(start, len(dates)):
        keep: list[dict[str, Any]] = []
        for position in positions:
            if position["exit_idx"] == index:
                column = position["column"]
                price = open_[index, column]
                if not math.isfinite(price) or price <= 0:
                    price = position["last_price"]
                proceeds = (
                    position["qty"]
                    * price
                    * (1.0 - cfg.slippage)
                    * (1.0 - cfg.commission - cfg.stamp_tax)
                )
                cash += proceeds
                trade_returns.append(proceeds / position["cost"] - 1.0)
            else:
                keep.append(position)
        positions = keep

        equity_open = cash
        for position in positions:
            column = position["column"]
            price = open_[index, column]
            if not math.isfinite(price) or price <= 0:
                price = position["last_price"]
            equity_open += position["qty"] * price
            position["last_price"] = price

        held = {str(position["symbol"]) for position in positions}
        for record in by_entry.get(index, []):
            if len(positions) >= cfg.max_picks:
                break
            symbol = str(record.symbol)
            if symbol in held:
                continue
            column = column_index[symbol]
            fill = float(record.entry_open) * (1.0 + cfg.slippage)
            target = equity_open / cfg.max_picks
            qty = int(min(target, cash) // (fill * cfg.lot)) * cfg.lot
            if qty <= 0:
                continue
            cost = qty * fill * (1.0 + cfg.commission)
            if cost > cash:
                continue
            cash -= cost
            positions.append(
                {
                    "symbol": symbol,
                    "qty": qty,
                    "column": column,
                    "exit_idx": index + horizon,
                    "cost": cost,
                    "last_price": float(record.entry_open),
                }
            )
            held.add(symbol)

        equity = cash
        for position in positions:
            column = position["column"]
            price = close[index, column]
            if not math.isfinite(price) or price <= 0:
                price = position["last_price"]
            equity += position["qty"] * price
            position["last_price"] = price
        rows.append(
            {
                "date": dates[index],
                "equity": equity,
                "cash": cash,
                "positions": len(positions),
                "exposure_pct": (
                    100.0 * (equity - cash) / equity if equity > 0 else 0.0
                ),
            }
        )

    frame = pd.DataFrame(rows).set_index("date")
    if frame.empty:
        return frame, {}

    daily = frame["equity"].pct_change().dropna()
    final_equity = float(frame["equity"].iloc[-1])
    total = final_equity / cfg.initial_capital - 1.0
    days = int(len(daily))
    cagr = (
        (final_equity / cfg.initial_capital) ** (252.0 / days) - 1.0
        if days and final_equity > 0
        else -1.0
    )
    daily_std = float(daily.std(ddof=1)) if days > 1 else 0.0
    sharpe = (
        float(daily.mean() / daily_std * math.sqrt(252.0)) if daily_std > 0 else 0.0
    )
    drawdown = float((frame["equity"] / frame["equity"].cummax() - 1.0).min())
    ruin = frame.index[frame["equity"] < 10_000.0]

    stats = {
        "initial_capital": cfg.initial_capital,
        "final_equity": round(final_equity, 2),
        "total_return_pct": round(total * 100.0, 4),
        "cagr_pct": round(cagr * 100.0, 4),
        "max_drawdown_pct": round(drawdown * 100.0, 4),
        "sharpe": round(sharpe, 4),
        "trades": int(len(trade_returns)),
        "avg_trade_return_pct": (
            round(float(np.mean(trade_returns)) * 100.0, 4)
            if trade_returns
            else 0.0
        ),
        "avg_exposure_pct": round(float(frame["exposure_pct"].mean()), 2),
        "first_equity_below_10k": str(ruin[0].date()) if len(ruin) else None,
    }
    return frame, stats


# ---------------------------------------------------------------------------
# 基准与报告
# ---------------------------------------------------------------------------


def equal_weight_open_returns(
    panels: dict[str, pd.DataFrame],
    symbols: np.ndarray,
    start: pd.Timestamp,
    end: pd.Timestamp,
) -> pd.Series:
    columns = [str(symbol) for symbol in symbols if str(symbol) in panels["open"].columns]
    open_ = panels["open"].loc[:, columns]
    close = panels["close"].loc[:, columns]
    volume = panels["volume"].loc[:, columns]
    valid = open_.notna() & close.notna() & (volume > 0)
    returns = (open_ / open_.shift(1) - 1.0).where(valid)
    return returns.mean(axis=1).loc[start:end]


def index_curve(path: Path, dates: pd.DatetimeIndex) -> pd.Series:
    frame = pd.read_csv(path, parse_dates=["date"])
    series = frame.set_index("date")["close"].sort_index()
    return series.reindex(dates).ffill().bfill()


def md_table(headers: list[str], rows: list[list[Any]]) -> str:
    lines = [
        "| " + " | ".join(headers) + " |",
        "|" + "|".join(["---"] * len(headers)) + "|",
    ]
    for row in rows:
        lines.append("| " + " | ".join(str(value) for value in row) + " |")
    return "\n".join(lines)


def _pct(value: Any, digits: int = 2) -> str:
    if value is None:
        return "-"
    return f"{float(value):.{digits}f}%"


def render_memo(
    summary: dict[str, Any],
    horizons: pd.DataFrame,
    yearly: pd.DataFrame,
    boards: pd.DataFrame,
) -> str:
    primary = summary["primary"]
    stress = summary["stress"]
    ex_outlier = summary["ex_outlier"]
    portfolio = summary["portfolio"]
    signal = summary["signal"]

    horizon_rows = [
        [
            row["label"],
            row["trades"],
            _pct(row["gross_mean_pct"]),
            _pct(row["net_mean_pct"]),
            _pct(row["net_median_pct"]),
            _pct(row["win_rate_pct"], 1),
            f"{row['t_stat_daily']:.2f}",
            _pct(row["cagr_pct"]),
            _pct(row["max_drawdown_pct"], 1),
        ]
        for row in horizons.to_dict("records")
    ]
    yearly_rows = [
        [
            row["year"],
            row["trades"],
            _pct(row["net_mean_pct"]),
            _pct(row["net_median_pct"]),
            _pct(row["win_rate_pct"], 1),
        ]
        for row in yearly.to_dict("records")
    ]
    board_rows = [
        [row["board"], row["trades"], _pct(row["net_mean_pct"]), _pct(row["win_rate_pct"], 1)]
        for row in boards.to_dict("records")
    ]

    portfolio_rows = [
        ["整手 2 槽组合", f"{portfolio.get('final_equity', 0):,.0f}", _pct(portfolio.get("total_return_pct")), _pct(portfolio.get("cagr_pct")), _pct(portfolio.get("max_drawdown_pct"), 1), f"{portfolio.get('sharpe', 0):.2f}", portfolio.get("trades", 0)],
        ["分数股每日等权", "-", _pct(primary.get("total_return_pct")), _pct(primary.get("cagr_pct")), _pct(primary.get("max_drawdown_pct"), 1), f"{primary.get('sharpe', 0):.2f}", primary.get("trades", 0)],
    ]
    benchmark_rows = [
        [item["label"], _pct(item.get("total_return_pct")), _pct(item.get("cagr_pct")), _pct(item.get("max_drawdown_pct"), 1), f"{item.get('sharpe', 0):.2f}"]
        for item in summary["benchmarks"]
    ]

    return f"""# 开盘涨幅动量策略回测（2026-11-14）

## 结论

**在这份数据和成本假设下，该策略没有正期望。** 按“开盘涨幅 ≤4%、昨日涨幅 ≤6%、前20日涨幅 ≥0%，开盘涨幅降序每天取 2 只，当日开盘买入”的规则，主口径 T+1 次日开盘卖出：

- {primary['trades']:,} 笔交易，净收益均值 **{_pct(primary['net_mean_pct'])}/笔**，中位数 {_pct(primary['net_median_pct'])}，胜率 **{_pct(primary['win_rate_pct'], 1)}**，日度 t 值 **{primary['t_stat_daily']:.2f}**。
- 毛收益均值 {_pct(primary['gross_mean_pct'])}，即使把佣金、印花税和滑点全部归零，仍然是负期望。
- 2018-2025 每个完整年份以及 2026 年至 10 月的净收益均值都为负；1/3/5/10/20 日持有和次日/5日收盘卖出也全部为负。
- 100 万、2 个半仓槽位的整手组合最终只剩 **{portfolio.get('final_equity', 0):,.0f} 元**，首次低于 1 万元的时间为 {portfolio.get('first_equity_below_10k') or '-'}。
- 亏损主要不是“选股池没有信号”：候选池本身次日开盘净收益均值 {_pct(signal['next_open_net_mean_pct'])}、中位数 {_pct(signal['next_open_net_median_pct'])}；而“开盘涨幅降序取前 2”把亏损放大到 {_pct(primary['net_mean_pct'])}/笔。买开盘涨幅最大的那一档，是负 alpha 最集中的地方。

> 口径提醒：你没有指定卖出规则。主口径取 T+1 次日开盘卖出（可执行、不违反 T+1，且每天都能腾出槽位）。其余持有期见下表；结论不依赖某一个卖出日。

## 规则与口径

| 项目 | 设定 |
|---|---|
| 股票池 | A 股 qfq 日线；剔除北交所（4/8/92）与 B 股（200/900）；当日有成交、至少 21 根历史 K 线 |
| 开盘涨幅 | `open_t / close_{{t-1}} - 1 <= 4%` |
| 昨日涨幅 | `close_{{t-1}} / close_{{t-2}} - 1 <= 6%` |
| 前20日涨幅 | `close_{{t-1}} / close_{{t-21}} - 1 >= 0%` |
| 排序 | 开盘涨幅降序；同值按 symbol 升序；每天最多 2 只 |
| 买入 | 当日开盘价，滑点 10bp |
| 主卖出 | 次日开盘价（T+1） |
| 成本 | 佣金 3bp/边，卖出印花税 10bp，滑点 10bp/边 |
| 组合 | 100 万，2 个半仓槽位，100 股整手；主口径每日轮动 |
| ST | 历史 ST 标记不可得，未过滤 |
| 退市股 | 已并入本地退市价格，部分修正幸存者偏差 |

## 主结果：固定持有期

{md_table(["口径", "笔数", "毛均值", "净均值", "中位数", "胜率", "日度t", "CAGR", "最大回撤"], horizon_rows)}

- `open_N`：第 N 个交易日开盘卖出；`close_N`：第 N 个交易日收盘卖出。
- `close_1` 已是 T+1 之后，合规；同日开盘买、收盘卖不可行，未列入。
- 所有口径的净均值都为负，且持有 1-10 日的日度 t 值在 -5.8 到 -11.6 之间。
- CAGR/最大回撤按每日等权曲线计算；曲线接近归零后年化不再有经济意义，仅用于横向比较。

## 分年（主口径 open_1）

{md_table(["年份", "笔数", "净均值", "中位数", "胜率"], yearly_rows)}

## 组合曲线（主口径 open_1）

{md_table(["组合", "期末资金", "总收益", "CAGR", "最大回撤", "Sharpe", "交易笔数"], portfolio_rows)}

- “整手 2 槽组合”是实际 100 股整手、50% 仓位的资金曲线；亏到不足一手后不再交易，因此期末资金只是残值。
- “分数股每日等权”是同样每天 2 只等权、但允许分数股的纯策略曲线，用来分离整手约束和策略本身。
- 2 个槽位在 open_1 下每天开盘卖出旧仓、买入新仓，资金可以完全周转。

## 基准（同一区间）

{md_table(["基准", "总收益", "CAGR", "最大回撤", "Sharpe"], benchmark_rows)}

- `ew_all_a`：全 A 等权、每日再平衡、开盘到开盘，无成本；与策略执行时点一致。
- `index_399101`：中小板综指（399101）买入持有，收盘到收盘，仅作参照。

## 诊断

- **候选池整体为负**：满足三条过滤的候选共 {signal['candidate_rows']:,} 个（{signal['candidate_days']:,} 个交易日，日均 {signal['avg_candidates_per_day']:.0f} 个），次日开盘净收益均值 {_pct(signal['next_open_net_mean_pct'])}、中位数 {_pct(signal['next_open_net_median_pct'])}。不是“选错了两只”，而是这个条件组合本身没有短线正期望。
- **排序方向放大了亏损**：每天按开盘涨幅降序取前 2，实际选中的开盘涨幅中位数约 {summary['signal']['selected_median_open_gap_pct']:.2f}%，几乎贴着 4% 上限；这一档是负收益最集中的区域。反过来按开盘涨幅升序取 2 只，主口径净均值也只有 {_pct(summary['diagnostics']['ascending_net_mean_pct'])}，同样为负——不是简单的排序方向问题。
- **分板块**：所有主要板块的主口径净均值都为负，见下表；不存在“只在某个板块有效”的迹象。

{md_table(["板块", "笔数", "净均值", "胜率"], board_rows)}

- **亏损归因**：主口径毛收益 -0.74% 可拆成入场日开盘→收盘 -0.52%、隔夜 -0.25%、次日开盘→收盘 +0.27%；亏损主要来自买入当天追高后的日内回落。详见 `diagnostics.md`。
- **压力测试**：滑点提高到 20bp/边，主口径净均值 {_pct(stress['net_mean_pct'])}，结论不变。
- **剔除 2024-09-30**：该日是全市场极端上涨日，前 2 只次日开盘平均 +13.9%；剔除后主口径净均值 {_pct(ex_outlier['net_mean_pct'])}，仍为负。

## 风险与限制

1. **开盘价成交假设**：信号使用当日开盘价，回测假设能以该价格成交。A 股集合竞价 9:20 前截单、9:25 出价，9:30 后的市价单可能偏离开盘价；10bp 滑点是对此的补偿，但不保证。若实际成交更差，结果只会更差。
2. **ST 未过滤**：历史 ST 标记不可得。ST 的 5% 涨跌停会让“开盘涨幅 4%”更容易接近上限，可能放大反转；但分板块和价格分层的结果都未显示正期望。
3. **幸存者偏差**：2018-2024 的 baostock 面板以 2024 年仍在上市的公司为主，退市股只补到 244 只；剩余偏差通常会**高估**收益，而本策略已经是负收益，因此结论方向不受影响。
4. **停牌/涨跌停**：当日无成交、开盘无效的样本被跳过；开盘涨停无法买入的样本因开盘涨幅 ≤4% 基本被排除。跌停卖出/买入的极端情况未单独建模。
5. **数据复权**：2025-2026 扩展以 2024-12-31 为锚点缩放到 baostock 基期；退市股用 Eastmoney 原始日线，抽样核对其日收益与涨跌幅一致。
6. **研究结论不是实盘建议**：该规则在本地数据上没有正期望，不应直接用于实盘。

## 复现

```bash
cd quant-python/signal_system
../.venv/bin/python open_gap_momentum_study.py \\
  --out ../../docs/backtest-results/2026-11-14-open-gap-momentum
```

首次运行会构建 `cache/open_gap_study/panels_2018_2026.pkl`（约 1-2 分钟）；后续直接读缓存。加 `--rebuild-panels` 可强制重建。

## 文件

- `summary.json`：全部指标、口径和数据覆盖
- `horizons.csv`：固定持有期结果
- `yearly.csv`：分年结果
- `boards.csv`：分板块结果
- `trades_open_1.csv`：主口径逐笔交易
- `curves.csv`：策略、全 A 等权、399101 的日度曲线
- `portfolio_curve.csv`：整手 2 槽组合的逐日资金曲线
- `diagnostics.md`：亏损归因、变体扫描、择时诊断
- `variants.csv`：变体扫描明细
- `open_gap_momentum_study.py`：主回测脚本
- `open_gap_momentum_diagnostics.py`：诊断脚本
"""


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--rebuild-panels", action="store_true")
    parser.add_argument("--slippage", type=float, default=0.0010)
    parser.add_argument("--stress-slippage", type=float, default=0.0020)
    args = parser.parse_args()

    cfg = StudyConfig(
        slippage=args.slippage,
        stress_slippage=args.stress_slippage,
    )
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    print("loading panels ...", flush=True)
    panels = load_study_panels(PANEL_CACHE, rebuild=args.rebuild_panels)
    print(
        f"panels: {panels['close'].shape[0]} sessions x "
        f"{panels['close'].shape[1]} symbols "
        f"({panels['close'].index.min().date()} .. {panels['close'].index.max().date()})",
        flush=True,
    )

    print("building signals ...", flush=True)
    signal = build_signal_table(panels, cfg)
    selected = select_signals(signal, cfg)
    if selected.empty:
        raise SystemExit("no signals")

    print(f"selected {len(selected):,} signal rows", flush=True)

    specs: list[tuple[str, int, str]] = [("open_1", 1, "open")]
    specs.extend((f"open_{h}", h, "open") for h in cfg.horizons if h != 1)
    specs.extend((f"close_{h}", h, "close") for h in cfg.close_horizons)

    trades_by_label: dict[str, pd.DataFrame] = {}
    stats_by_label: dict[str, dict[str, Any]] = {}
    for label, horizon, exit_mode in specs:
        trades = build_trades(selected, signal, horizon, exit_mode, cfg)
        trades_by_label[label] = trades
        stats_by_label[label] = stats_from_trades(trades, label)
        print(
            f"{label}: {stats_by_label[label].get('trades', 0):,} trades, "
            f"net mean {stats_by_label[label].get('net_mean_pct', 0):.3f}%",
            flush=True,
        )

    primary = trades_by_label["open_1"]
    primary_stats = stats_by_label["open_1"]

    stress_trades = build_trades(
        selected, signal, 1, "open", cfg, slippage=cfg.stress_slippage
    )
    stress_stats = stats_from_trades(stress_trades, "open_1_stress_20bp")
    ex_outlier = primary[primary["entry_date"] != pd.Timestamp("2024-09-30")]
    ex_outlier_stats = stats_from_trades(ex_outlier, "open_1_ex_20240930")

    # 反向排序诊断：同一天按开盘涨幅升序取 2 只。
    ascending_rows: list[float] = []
    dates = signal["dates"]
    symbols = signal["symbols"]
    open_array = signal["open"].to_numpy(dtype="float64")
    open_gap_array = signal["open_gap"].to_numpy(dtype="float64")
    mask = signal["mask"]
    for index in range(len(dates) - 1):
        candidates = np.flatnonzero(mask[index] & np.isfinite(open_array[index + 1]))
        if candidates.size == 0:
            continue
        order = np.lexsort((symbols[candidates], open_gap_array[index, candidates]))
        for column in candidates[order[: cfg.max_picks]]:
            entry_price = open_array[index, column]
            exit_price = open_array[index + 1, column]
            net = (
                exit_price
                * (1.0 - cfg.slippage)
                * (1.0 - cfg.commission - cfg.stamp_tax)
                / (entry_price * (1.0 + cfg.slippage) * (1.0 + cfg.commission))
                - 1.0
            )
            ascending_rows.append(float(net))
    ascending_net_mean = (
        float(np.mean(ascending_rows)) * 100.0 if ascending_rows else 0.0
    )

    print("simulating portfolio ...", flush=True)
    portfolio_curve, portfolio_stats = simulate_two_slot_portfolio(
        selected, signal, 1, cfg
    )

    # 曲线：策略每日等权、全 A 等权开盘到开盘、399101。
    first_entry_idx = int(primary["entry_idx"].min())
    last_entry_idx = int(primary["entry_idx"].max())
    start_date = dates[first_entry_idx - 1]
    first_entry_date = dates[first_entry_idx]
    end_date = dates[last_entry_idx + 1]

    daily = primary.groupby("entry_date")["net_return"].mean().sort_index()
    strategy_curve = pd.concat(
        [
            pd.Series(1.0, index=[start_date]),
            (1.0 + daily).cumprod(),
        ]
    ).sort_index()

    ew_returns = equal_weight_open_returns(
        panels, signal["symbols"], first_entry_date, end_date
    )
    ew_curve = pd.concat(
        [pd.Series(1.0, index=[start_date]), (1.0 + ew_returns).cumprod()]
    )
    ew_curve = ew_curve.reindex(strategy_curve.index).ffill()

    index_series = index_curve(INDEX_399101, strategy_curve.index)
    index_normalized = index_series / float(index_series.loc[start_date])

    curves = pd.DataFrame(
        {
            "strategy": strategy_curve,
            "ew_all_a": ew_curve,
            "index_399101": index_normalized,
        }
    )
    benchmark_stats = [
        equity_stats(curves["ew_all_a"], "ew_all_a"),
        equity_stats(curves["index_399101"], "index_399101"),
    ]

    candidate_stats = candidate_diagnostics(signal, cfg)
    yearly = yearly_table(primary)
    boards = board_table(primary)

    summary: dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "strategy": {
            "name": "open_gap_momentum",
            "rules": {
                "open_gap_max_pct": cfg.max_gap * 100.0,
                "yest_ret_max_pct": cfg.max_yest * 100.0,
                "ret20_min_pct": cfg.min_ret20 * 100.0,
                "sort": "open_gap desc, symbol asc",
                "max_picks_per_day": cfg.max_picks,
                "entry": "same-day open",
                "primary_exit": "next open (T+1)",
            },
            "costs": {
                "commission_per_side": cfg.commission,
                "stamp_tax_sell": cfg.stamp_tax,
                "slippage_per_side": cfg.slippage,
                "stress_slippage_per_side": cfg.stress_slippage,
            },
            "portfolio": {
                "initial_capital": cfg.initial_capital,
                "slots": cfg.max_picks,
                "slot_weight": 1.0 / cfg.max_picks,
                "lot": cfg.lot,
            },
        },
        "data": {
            "panel_start": str(panels["close"].index.min().date()),
            "panel_end": str(panels["close"].index.max().date()),
            "sessions": int(len(panels["close"].index)),
            "symbols_after_exclusion": int(len(signal["symbols"])),
            "baostock_symbols": 4817,
            "delisted_symbols": 244,
            "new_listing_symbols": 37,
        },
        "signal": {
            **candidate_stats,
            "selected_trades": int(len(primary)),
            "selected_days": int(primary["entry_date"].nunique()),
            "selected_median_open_gap_pct": round(
                float(primary["open_gap_pct"].median()), 4
            ),
            "selected_median_yest_ret_pct": round(
                float(primary["yest_ret_pct"].median()), 4
            ),
            "selected_median_ret20_pct": round(
                float(primary["ret20_pct"].median()), 4
            ),
        },
        "primary": primary_stats,
        "stress": stress_stats,
        "ex_outlier": ex_outlier_stats,
        "portfolio": portfolio_stats,
        "benchmarks": benchmark_stats,
        "diagnostics": {
            "ascending_net_mean_pct": round(ascending_net_mean, 4),
        },
        "horizons": [stats_by_label[label] for label, _, _ in specs],
        "yearly": yearly.to_dict("records"),
        "boards": boards.to_dict("records"),
    }

    horizons_frame = pd.DataFrame(summary["horizons"])
    boards_frame = pd.DataFrame(summary["boards"])

    summary_path = out_dir / "summary.json"
    with summary_path.open("w", encoding="utf-8") as handle:
        json.dump(summary, handle, ensure_ascii=False, indent=2)

    horizons_frame.to_csv(out_dir / "horizons.csv", index=False, encoding="utf-8")
    yearly.to_csv(out_dir / "yearly.csv", index=False, encoding="utf-8")
    boards.to_csv(out_dir / "boards.csv", index=False, encoding="utf-8")
    primary.to_csv(out_dir / "trades_open_1.csv", index=False, encoding="utf-8")
    curves.to_csv(out_dir / "curves.csv", encoding="utf-8")
    portfolio_curve.to_csv(out_dir / "portfolio_curve.csv", encoding="utf-8")

    memo = render_memo(summary, horizons_frame, yearly, boards)
    (out_dir / "memo.md").write_text(memo, encoding="utf-8")

    print("\n=== summary ===")
    print(
        f"primary open_1: trades={primary_stats['trades']:,}, "
        f"net_mean={primary_stats['net_mean_pct']:.3f}%, "
        f"median={primary_stats['net_median_pct']:.3f}%, "
        f"win={primary_stats['win_rate_pct']:.1f}%, "
        f"t_daily={primary_stats['t_stat_daily']:.2f}"
    )
    print(
        f"portfolio: final={portfolio_stats.get('final_equity', 0):,.0f}, "
        f"max_dd={portfolio_stats.get('max_drawdown_pct', 0):.1f}%, "
        f"trades={portfolio_stats.get('trades', 0):,}"
    )
    for item in benchmark_stats:
        print(
            f"{item['label']}: total={item.get('total_return_pct', 0):.1f}%, "
            f"cagr={item.get('cagr_pct', 0):.2f}%, "
            f"max_dd={item.get('max_drawdown_pct', 0):.1f}%, "
            f"sharpe={item.get('sharpe', 0):.2f}"
        )
    print(f"\nwrote {out_dir}")


if __name__ == "__main__":
    main()
