"""Generate deterministic baseline artifacts for regression checks."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backtest.engine.bt_engine import BacktestEngine
from backtest.strategies.trend_following_bt import TrendFollowingBacktestStrategy


BASELINE_DIR = Path(__file__).resolve().parent
DATA_PATH = BASELINE_DIR / "sample_price_data.csv"
RESULT_PATH = BASELINE_DIR / "trend_following_baseline_result.json"
PARAM_SCAN_PATH = BASELINE_DIR / "trend_following_parameter_scan.json"


def build_config() -> dict:
    return {
        "strategy": {
            "technical": {
                "ma_period": 5,
            }
        },
        "risk": {
            "stop_loss_pct": 0.08,
            "stop_profit_pct": 0.20,
        },
        "backtest": {
            "strategy_name": "trend_following",
            "initial_cash": 100000,
            "commission_pct": 0.0003,
            "stamp_tax_pct": 0.001,
            "slippage_pct": 0.0005,
            "lot_size": 100,
            "t_plus_one": True,
            "price_limit_model": "conservative",
            "max_parameter_combinations": 4,
            "max_in_out_sample_gap": 0.20,
            "config_version": "baseline-v1",
        },
    }


def load_price_data() -> pd.DataFrame:
    frame = pd.read_csv(DATA_PATH)
    frame["datetime"] = pd.to_datetime(frame["datetime"])
    frame.attrs["ts_code"] = "000001.SZ"
    return frame


def main() -> None:
    config = build_config()
    price_data = load_price_data()

    engine = BacktestEngine(config=config)
    strategy = TrendFollowingBacktestStrategy(config)
    signals = strategy.generate_signals(price_data)
    result = engine.run(
        price_data=price_data,
        signals=signals,
        output_path=str(RESULT_PATH),
        strategy_name="trend_following",
        regime_scope="bull",
        config_snapshot=config,
    )

    parameter_scan = engine.scan_parameters(
        price_data=price_data,
        strategy_cls=TrendFollowingBacktestStrategy,
        param_grid={
            "strategy.technical.ma_period": [4, 5],
            "risk.stop_loss_pct": [0.06, 0.08],
        },
        base_config=config,
        strategy_name="trend_following",
        split_ratio=0.7,
        score_field="annual_return",
        regime_scope="bull",
    )
    PARAM_SCAN_PATH.write_text(
        json.dumps(parameter_scan, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )

    manifest = {
        "result_json": str(RESULT_PATH.name),
        "report_json": str(Path(result["output_files"]["report_json"]).name),
        "trades_csv": str(Path(result["output_files"]["trades_csv"]).name),
        "signals_json": str(Path(result["output_files"]["signals_json"]).name),
        "positions_json": str(Path(result["output_files"]["positions_json"]).name),
        "parameter_scan_json": PARAM_SCAN_PATH.name,
        "signal_count": len(signals),
        "trade_count": result["summary"]["trade_count"],
    }
    (BASELINE_DIR / "baseline_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
