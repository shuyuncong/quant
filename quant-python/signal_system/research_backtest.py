"""Local-only, funded daily replay of the versioned analysis strategies.

No market fetches, signal store, notification calls or real holdings writes.
"""
from __future__ import annotations
from datetime import date
import hashlib
import json
import math
import os
from pathlib import Path
from urllib.parse import urlparse

import numpy as np
import pandas as pd

from strategy.registry import STRATEGIES, VERSION, evaluate_strategies, evaluate_exits, exit_parameters
from strategy.macd import calculate_macd, find_golden_cross_entries
from strategy.macd_divergence import add_divergence_indicators, resolve_divergence_config
from strategy.yearline import prepare_yearline_bars, add_yearline_indicators
from backtest_winrate import _execution_values, _resolve_execution_config, _buy_cash, _sell_cash, _bar_price_limits, _resolve_sell_fill


def write_json(path, value):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, allow_nan=False, default=str), encoding="utf-8")
    temporary.replace(path)


def assert_local_research():
    target = urlparse(os.environ.get("DATABASE_URL", ""))
    if os.environ.get("QUANT_RESEARCH_LOCAL") != "1" or target.scheme not in {"postgres", "postgresql"} or target.hostname not in {"localhost", "127.0.0.1", "::1"} or (target.port or 5432) != 5432 or target.path != "/quant" or target.query or target.fragment:
        raise ValueError("回测仅允许在本地研究环境运行（loopback:5432/quant）")


def metrics(curve: list[dict], closed_trades: list[dict], initial: float) -> dict:
    equities = np.array([initial] + [float(point["equity"]) for point in curve], dtype=float)
    daily = equities[1:] / equities[:-1] - 1 if len(equities)>1 else np.array([])
    peak = np.maximum.accumulate(equities)
    deviation = float(np.std(daily, ddof=1)) if len(daily)>1 else 0.0
    profits = [float(trade["pnl_cash"]) for trade in closed_trades]
    wins, losses = [x for x in profits if x>0], [x for x in profits if x<0]
    final = float(equities[-1])
    return {"total_return_pct": (final/initial-1)*100,
            "annualized_return_pct": ((final/initial)**(252/len(daily))-1)*100 if len(daily) and final>0 else None,
            "max_drawdown_pct": float(np.max((peak-equities)/peak))*100,
            "sharpe_ratio": float(np.mean(daily))/deviation*math.sqrt(252) if deviation>1e-12 else None,
            "payoff_ratio": float(np.mean(wins))/abs(float(np.mean(losses))) if wins and losses else None,
            "win_rate_pct": len(wins)/len(profits)*100 if profits else None,
            "closed_trades": len(profits), "final_equity": final}


def entry_indices(frame: pd.DataFrame, config: dict) -> dict[str, set[int]]:
    """Preselect possible trigger days, then verify with the same live predicate."""
    possibilities = {key: set() for key in STRATEGIES}
    macd = config.get("signal_strategy", {}).get("macd", {})
    enriched = frame.join(calculate_macd(frame["close"], fast=macd.get("fast",12), slow=macd.get("slow",26), signal=macd.get("signal",9)))
    for item in find_golden_cross_entries(enriched, fast=macd.get("fast",12), slow=macd.get("slow",26), signal=macd.get("signal",9), zero_axis_tolerance=macd.get("zero_axis_tolerance",.005), confirmation_bars=macd.get("pullback_confirmation_bars",5)):
        possibilities["macd_zero_axis"].add(int(item["confirmation_index"]))
    yearline = add_yearline_indicators(frame)
    if len(yearline):
        mask = yearline["ma250_up"] & yearline["ma_align"] & yearline["above250_prev"] & yearline["low_in_zone"] & yearline["close_hold"] & yearline["close_ge_open"]
        possibilities["yearline_pullback"] = set(np.flatnonzero(mask))
    divergence = add_divergence_indicators(frame, resolve_divergence_config(config))
    if len(divergence): possibilities["macd_divergence"] = set(np.flatnonzero(divergence["golden_cross"]))
    confirmed = {key: set() for key in STRATEGIES}
    for strategy, indices in possibilities.items():
        for index in sorted(indices):
            result = evaluate_strategies(frame.iloc[:index+1], config, strategy_ids=[strategy])[0]
            if result["status"] == "ok" and result["buy"]:
                confirmed[strategy].add(int(index))
    return confirmed


def replay(histories: dict[str,pd.DataFrame], signals: dict[str,dict[str,set[int]]], calendar: list[str], config: dict, options: dict, strategy: str) -> dict:
    initial = float(options.get("initial_cash",100000))
    max_positions = int(options.get("max_positions",4))
    allocation = float(options.get("position_size_pct",.25))
    execution = _execution_values(_resolve_execution_config(config))
    rules = exit_parameters(config,strategy)
    locations = {symbol: {str(pd.Timestamp(value).date()): index for index,value in enumerate(frame.datetime)} for symbol,frame in histories.items()}
    candidates: dict[str,list[tuple[str,int]]] = {}
    for symbol, frame in histories.items():
        for index in signals[symbol][strategy]:
            if index+1 < len(frame):
                day = str(pd.Timestamp(frame.datetime.iloc[index+1]).date())
                # Signals formed before the selected start date may not open a position.
                if str(pd.Timestamp(frame.datetime.iloc[index]).date()) >= options["start"]:
                    candidates.setdefault(day,[]).append((symbol,index+1))
    cash = initial
    positions: dict[str,dict] = {}
    trades, curve, rejected = [], [], []
    def sell(symbol,index,price,reason,day,session):
        nonlocal cash
        position=positions[symbol]
        resolved=_resolve_sell_fill(symbol,histories[symbol],index,price,session,execution)
        if resolved is None:
            position["pending_exit"] = reason
            return False
        costs={**execution,"stamp_tax_pct":.0005 if day >= "2023-08-28" else .001}
        received=_sell_cash(resolved[0],position["quantity"],costs)["total"]
        cash+=received
        trades.append({"symbol":symbol,"entry_day":position["entry_day"],"exit_day":day,"entry_price":position["entry_price"],"exit_price":resolved[0],"quantity":position["quantity"],"pnl_cash":received-position["entry_cash"],"exit_reason":reason})
        del positions[symbol]
        return True
    for day in calendar:
        # Only opening exits release cash for orders executed at this opening.
        for symbol,position in list(positions.items()):
            index=locations[symbol].get(day)
            if index is not None and position.get("pending_exit") and day>position["entry_day"]:
                sell(symbol,index,float(histories[symbol].iloc[index].open),position["pending_exit"],day,"open")
        for symbol,index in sorted(candidates.get(day,[])):
            if symbol in positions or len(positions)>=max_positions: continue
            frame=histories[symbol]; row=frame.iloc[index]; price=float(row.open)
            if float(row.volume)<=0 or price<=0: continue
            limits=_bar_price_limits(symbol,frame,index,execution)
            if limits and price>=limits[0]-.0001:
                rejected.append({"symbol":symbol,"date":day,"reason":"涨停无法买入"}); continue
            equity=cash+sum(position["quantity"]*position["mark"] for position in positions.values())
            minimum=200 if symbol.startswith(("688","689")) else 100
            step=1 if minimum==200 or symbol.endswith(".BJ") else 100
            budget=min(cash,equity*allocation)
            quantity=int(budget/price/step)*step
            while quantity>=minimum and _buy_cash(price,quantity,execution)["total"]>budget: quantity-=step
            if quantity<minimum: continue
            spent=_buy_cash(price,quantity,execution)["total"]
            cash-=spent
            positions[symbol]={"quantity":quantity,"entry_price":price,"entry_cash":spent,"entry_day":day,"entry_index":index,"mark":price,"pending_exit":None}
        for symbol,position in list(positions.items()):
            index=locations[symbol].get(day)
            if index is None: continue
            frame=histories[symbol]; row=frame.iloc[index]
            position["mark"]=float(row.close)
            if day>position["entry_day"] and float(row.volume)>0:
                stop=position["entry_price"]*(1-rules["stop_loss_pct"])
                take=position["entry_price"]*(1+rules["take_profit_pct"]) if rules["take_profit_pct"] is not None else None
                if float(row.low)<=stop:
                    if sell(symbol,index,min(float(row.open),stop),"stop_loss",day,"intraday"): continue
                elif take is not None and float(row.high)>=take:
                    if sell(symbol,index,max(float(row.open),take),"take_profit",day,"intraday"): continue
            checks,_=evaluate_exits(frame.iloc[:index+1],config,strategy,{"shares":position["quantity"],"cost_price":position["entry_price"],"opened_on":position["entry_day"]})
            triggers=[check["name"] for check in checks if check["met"] is True]
            if triggers: position["pending_exit"]=" / ".join(triggers)
        equity=cash+sum(position["quantity"]*position["mark"] for position in positions.values())
        curve.append({"date":day,"equity":round(equity,6),"cash":round(cash,6),"positions":len(positions),"return_pct":(equity/initial-1)*100})
    return {"strategy_id":strategy,"name":STRATEGIES[strategy],"version":VERSION,"metrics":metrics(curve,trades,initial),"equity_curve":curve,"trades":trades,"open_positions":positions,"rejected":rejected}


def run_local_backtest(config: dict, options: dict) -> dict:
    assert_local_research()
    start,end=date.fromisoformat(options["start"]),date.fromisoformat(options["end"])
    if start>=end: raise ValueError("回测结束日期必须晚于开始日期")
    directory=Path(config.get("market_data",{}).get("cache_dir","./cache"))
    history_dir=directory/"daily_history"
    files=sorted(history_dir.glob("*_qfq.pkl"))
    requested=options.get("symbols") or []
    if requested: files=[file for file in files if file.stem[:6] in {str(code)[:6] for code in requested}]
    if not files: raise ValueError("本地没有对应的前复权行情缓存，请先准备本地历史行情")
    calendar_file=directory/(hashlib.sha256(b"trade_calendar").hexdigest()+".pkl")
    if not calendar_file.exists(): raise ValueError("本地缺少交易日历缓存，无法计算完整交易日收益")
    calendar_frame=pd.read_pickle(calendar_file)
    calendar_dates=sorted(set(pd.to_datetime(calendar_frame["trade_date"]).dt.date))
    if not calendar_dates or calendar_dates[-1]<end: raise ValueError("本地交易日历未覆盖结束日期")
    calendar=[str(day) for day in calendar_dates if start<=day<=end]
    if not calendar: raise ValueError("区间内没有交易日")
    task_dir=Path(options["output_dir"])
    task_dir.mkdir(parents=True,exist_ok=True)
    histories,signals,excluded,manifest={},{},[],[]
    config_hash=hashlib.sha256(json.dumps(config,sort_keys=True,default=str).encode()).hexdigest()
    from data.symbols import normalize_ts_code as normalize_symbol
    manifest=[{"symbol":normalize_symbol(file.stem[:6]),"sha256":hashlib.sha256(file.read_bytes()).hexdigest()} for file in files]
    snapshot={"files":manifest,"calendar_hash":hashlib.sha256(calendar_file.read_bytes()).hexdigest(),"config_hash":config_hash,"version":VERSION}
    snapshot_file=task_dir/"input_manifest.json"
    if snapshot_file.exists() and json.loads(snapshot_file.read_text(encoding="utf-8")) != snapshot:
        raise ValueError("本地行情、日历或参数已变化，不能混用旧检查点，请新建回测任务")
    if not snapshot_file.exists(): write_json(snapshot_file,snapshot)
    present={row["symbol"] for row in manifest}
    excluded.extend({"symbol":symbol,"reason":"本地没有该股票历史缓存"} for symbol in requested if normalize_symbol(symbol) not in present)
    for number,file in enumerate(files):
        symbol=normalize_symbol(file.stem[:6])
        fingerprint=manifest[number]["sha256"]
        frame=prepare_yearline_bars(pd.read_pickle(file))
        frame=frame[pd.to_datetime(frame.datetime).dt.date<=end].reset_index(drop=True)
        if len(frame)<270:
            excluded.append({"symbol":symbol,"reason":"少于 270 根已收盘日线"}); continue
        histories[symbol]=frame
        checkpoint=task_dir/f"{symbol}.signals.json"
        key=f"{VERSION}:{config_hash}:{fingerprint}:{end}"
        try: cached=json.loads(checkpoint.read_text(encoding="utf-8")) if checkpoint.exists() else {}
        except (ValueError, OSError): cached={}
        if cached.get("key")==key: signals[symbol]={name:set(indices) for name,indices in cached["signals"].items()}
        else:
            signals[symbol]=entry_indices(frame,config)
            write_json(checkpoint,{"key":key,"signals":{name:sorted(indices) for name,indices in signals[symbol].items()}})
        write_json(task_dir/"progress.json",{"stage":"策略计算","processed":number+1,"total":len(files),"excluded":len(excluded)})
    if not histories: raise ValueError("所选股票均没有足够历史数据")
    results=[]
    for strategy in options.get("strategies",list(STRATEGIES)):
        if strategy not in STRATEGIES: raise ValueError("未知策略")
        results.append(replay(histories,signals,calendar,config,options,strategy))
    report={"schema_version":1,"kind":"backtest","options":options,"strategy_version":VERSION,"data_manifest":manifest,"excluded":excluded,"calendar_days":len(calendar),"results":results,
            "warnings":["历史范围为本地缓存覆盖股票；退市股票和历史 ST 状态可能不完整，存在样本覆盖偏差。",
                        "采用前复权价格进行理论成交，分红送转与真实股份换算未逐笔复原；费用含佣金、滑点及按日期适用的印花税。",
                        "MACD 实时通知使用不复权行情，本回测统一用前复权历史；除权附近的信号日期可能不同。年线与底背离指标池也使用前复权。",
                        "三策略比较使用统一技术入场条件；实时入池的市值、流动性及市场闸门不属于本次回放的历史筛选范围。"]}
    output=task_dir/"report.json"
    write_json(output,report)
    write_json(task_dir/"progress.json",{"stage":"完成","processed":len(files),"total":len(files)})
    return {"output_file":str(output.resolve()),"strategies":len(results),"stocks":len(histories)}
