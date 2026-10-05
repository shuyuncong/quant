"""Checkpoint ownership for a Web scan. Published pools are never reset here."""
import json
import pandas as pd


def prepare_scan_run(monitor, strategy, run_id):
    prefix = {"macd_zero_axis": "daily", "yearline_pullback": "yearline", "macd_divergence": "macd_divergence"}[strategy]
    market = monitor.market if strategy == "macd_zero_axis" else monitor._yearline_market_client() if strategy == "yearline_pullback" else monitor._divergence_market_client()
    key = f"web_scan_run:{strategy}"
    stored = json.loads(monitor.store.get_state(key, "{}") or "{}")
    day = market.latest_expected_trade_date().isoformat()
    batch_key = f"web_scan_batch:{run_id}"
    batch = json.loads(monitor.store.get_state(batch_key, "{}") or "{}")
    if batch and batch["day"] != day:
        raise ValueError("该轮三策略扫描的行情日期已变化，请新建筛选任务")
    if not batch:
        batch = {"day": day, "rows": market.get_stock_list()[["code", "name"]].to_dict("records")}
        monitor.store.set_state(batch_key, json.dumps(batch, ensure_ascii=False))
    if stored.get("id") == run_id and stored.get("day") != day:
        raise ValueError("扫描行情日期已变化，请新建全市场筛选任务，避免混合两天的候选结果")
    if stored.get("id") != run_id:
        rows = batch["rows"]
        monitor.store.set_state(f"{prefix}_bootstrap_success", "[]")
        monitor.store.set_state(f"{prefix}_bootstrap_deferred", "{}")
        monitor.store.set_state(f"{prefix}_pool_cache", "[]")
        monitor.store.set_state(f"{prefix}_bootstrap_complete", "false")
        monitor.store.set_state(f"{prefix}_scan_day", day)
        stored = {"id": run_id, "day": day, "rows": rows}
        # The owner marker is written last; a preparation crash can safely start over.
        monitor.store.set_state(key, json.dumps(stored, ensure_ascii=False))
    market.get_stock_list = lambda: pd.DataFrame(stored["rows"], columns=["code", "name"])
