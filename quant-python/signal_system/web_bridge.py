"""JSON bridge between the Next.js web console and the signal_system engine.

Usage:
    python web_bridge.py <command> [--config PATH] [--payload -|@file|json]

Commands:
    config          返回合并 overrides 后的有效配置（密钥脱敏）与密钥来源
    normalize       解析混合分隔符/带名称的股票列表文本
    analyze         分析指定股票（可多周期）
    scan            全市场/自选股日线扫描
    monitor-once    执行一次盘中监控循环
    dispatch-outbox 尝试投递待发送队列
    test-notify     发送测试通知
    outbox-status   返回 outbox 摘要
    calendar        返回交易日/交易时段/当前时间

Exit codes: 0 success, 1 business failure, 2 argument/usage error.
Output: single JSON object on stdout: {"ok": true, "data": ...} or
        {"ok": false, "error": "...", "code": 1|2}.
"""

from __future__ import annotations

import argparse
from copy import deepcopy
import json
import os
import re
import sys
from typing import Any


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)
QUANT_ROOT = os.path.dirname(BASE_DIR)
if QUANT_ROOT not in sys.path:
    sys.path.insert(0, QUANT_ROOT)

from models import RISK_NOTICE, SignalEvent  # noqa: E402
from monitor.service import SignalMonitor  # noqa: E402
from data.symbols import normalize_ts_code  # noqa: E402
from utils.helpers import load_config  # noqa: E402
from utils.time_utils import now_shanghai  # noqa: E402


SECRET_PATHS: list[tuple[str, ...]] = [
    ("market_data", "tushare_token"),
    ("data_source", "tushare_token"),
    ("notification", "wechat", "webhook_url"),
    ("notification", "webhook", "url"),
    ("notification", "webhook", "headers", "Authorization"),
    ("notification", "email", "sender"),
    ("notification", "email", "password"),
    ("notification", "email", "receiver"),
    ("notification", "bark", "device_key"),
]

ENV_MARKER = "__env__"
TOKEN_RE = re.compile(r"(?:SH|SZ|BJ)?(\d{6})(?:\.(SH|SZ|BJ))?")


def _default_config_path() -> str:
    return os.path.join(BASE_DIR, "config", "config.yaml")


def _deep_get(config: dict, path: tuple[str, ...]) -> Any:
    current: Any = config
    for part in path:
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return current


def _deep_set(config: dict, path: tuple[str, ...], value: Any) -> None:
    current = config
    for part in path[:-1]:
        current = current.setdefault(part, {})
    current[path[-1]] = value


def _deep_merge(base: dict, override: dict) -> dict:
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value
    return base


def _read_payload(raw: str) -> dict[str, Any]:
    if raw == "-":
        text = sys.stdin.read()
    elif raw.startswith("@"):
        with open(raw[1:], "r", encoding="utf-8") as handle:
            text = handle.read()
    else:
        text = raw
    try:
        parsed = json.loads(text or "{}")
    except json.JSONDecodeError as exc:
        raise ValueError(f"payload 不是合法 JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ValueError("payload 必须是 JSON 对象")
    return parsed


def _resolve_env_markers(overrides: dict[str, Any]) -> None:
    for path in SECRET_PATHS:
        current = _deep_get(overrides, path)
        if isinstance(current, dict) and set(current) == {ENV_MARKER}:
            env_name = str(current[ENV_MARKER])
            _deep_set(overrides, path, os.getenv(env_name, ""))


def _mask_config(config: dict[str, Any]) -> dict[str, Any]:
    masked = deepcopy(config)
    for path in SECRET_PATHS:
        value = _deep_get(masked, path)
        if value:
            _deep_set(masked, path, "****")
    return masked


def _secret_sources(base: dict, overrides: dict) -> dict[str, str]:
    sources: dict[str, str] = {}
    for path in SECRET_PATHS:
        dot = ".".join(path)
        raw = _deep_get(overrides, path)
        if isinstance(raw, dict) and set(raw) == {ENV_MARKER}:
            sources[dot] = "env"
        elif raw is not None:
            sources[dot] = "db"
        elif _deep_get(base, path):
            sources[dot] = "yaml"
        else:
            sources[dot] = ""
    return sources


def _load_effective_config(config_path: str, overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    overrides = deepcopy(overrides or {})
    _resolve_env_markers(overrides)
    config = load_config(config_path)
    if config is None:
        raise RuntimeError(f"配置文件加载失败: {config_path}")
    if overrides:
        config = _deep_merge(config, overrides)
    return config


def _make_monitor(config_path: str, overrides: dict[str, Any] | None = None) -> SignalMonitor:
    config = _load_effective_config(config_path, overrides)
    return SignalMonitor(config)


def _emit(data: Any, code: int = 0) -> int:
    print(json.dumps({"ok": True, "data": data}, ensure_ascii=False, default=str))
    return code


def _emit_error(message: str, code: int = 1) -> int:
    print(json.dumps({"ok": False, "error": message, "code": code}, ensure_ascii=False))
    return code


def _cmd_config(config_path: str, payload: dict[str, Any]) -> int:
    raw = payload.get("overrides")
    overrides = raw if isinstance(raw, dict) else payload
    base = load_config(config_path)
    if base is None:
        return _emit_error(f"配置文件加载失败: {config_path}", 1)
    merged = _load_effective_config(config_path, overrides)
    masked = _mask_config(merged)
    sources = _secret_sources(base, overrides)
    return _emit(
        {
            "config": masked,
            "secret_sources": sources,
            "runtime": {
                "output_dir": merged.get("runtime", {}).get("output_dir"),
                "database_path": merged.get("runtime", {}).get("database_path"),
                "config_path": merged.get("_config_path"),
                "base_dir": merged.get("_base_dir"),
            },
        }
    )


def _parse_normalize(text: str) -> dict[str, Any]:
    symbols: list[dict[str, str]] = []
    unknown: list[str] = []
    records = re.split(r"[\n,;，；、]+", text.strip()) if text and text.strip() else []
    for record in records:
        record = record.strip()
        if not record:
            continue
        matches = list(TOKEN_RE.finditer(record.upper()))
        if not matches:
            unknown.append(record)
            continue
        if len(matches) == 1:
            match = matches[0]
            name = record.replace(match.group(0), "").strip("()（） \t")
            symbols.append({"symbol": normalize_ts_code(match.group(1)), "name": name})
        else:
            for match in matches:
                symbols.append({"symbol": normalize_ts_code(match.group(1)), "name": ""})
    seen: set[str] = set()
    unique: list[dict[str, str]] = []
    for item in symbols:
        if item["symbol"] in seen:
            continue
        seen.add(item["symbol"])
        unique.append(item)
    return {
        "symbols": unique,
        "unknown": unknown,
        "raw_lines": [line for line in text.splitlines()] if text else [],
    }


def _cmd_normalize(text: str) -> int:
    return _emit(_parse_normalize(text))


def _cmd_analyze(config_path: str, payload: dict[str, Any]) -> int:
    symbols = payload.get("symbols") or []
    if not symbols:
        return _emit_error("analyze 需要 symbols", 2)
    notify = bool(payload.get("notify", True))
    monitor = _make_monitor(config_path, payload.get("overrides"))
    report = monitor.analyze_symbols(symbols, notify=notify)
    return _emit({"report": report})


def _cmd_scan(config_path: str, payload: dict[str, Any]) -> int:
    notify = bool(payload.get("notify", True))
    monitor = _make_monitor(config_path, payload.get("overrides"))
    report = monitor.scan_zero_axis(notify=notify)
    return _emit({"report": report})


def _cmd_monitor_once(config_path: str, payload: dict[str, Any]) -> int:
    notify = bool(payload.get("notify", True))
    # 我的持仓由 Web 端随任务 payload 携带，并入监控范围
    holdings = payload.get("holdings") or []
    extra_symbols: list[str] = []
    if isinstance(holdings, list):
        for item in holdings:
            if isinstance(item, dict):
                code = item.get("symbol") or item.get("ts_code") or ""
                if code:
                    extra_symbols.append(str(code))
            elif isinstance(item, str) and item.strip():
                extra_symbols.append(item.strip())
    monitor = _make_monitor(config_path, payload.get("overrides"))
    report = monitor.run_monitor_cycle(notify=notify, extra_symbols=extra_symbols)
    return _emit({"report": report})


def _cmd_dispatch(config_path: str, payload: dict[str, Any]) -> int:
    monitor = _make_monitor(config_path, payload.get("overrides"))
    return _emit(
        monitor.dispatch_outbox(requeue_failed=bool(payload.get("requeue_failed", False)))
    )


def _cmd_test_notify(config_path: str, payload: dict[str, Any]) -> int:
    monitor = _make_monitor(config_path, payload.get("overrides"))
    channels = monitor.notifier.active_channels()
    if not channels:
        return _emit_error("没有启用通知通道，请先在推送配置中启用微信/webhook/邮件", 1)
    now = now_shanghai().isoformat(timespec="seconds")
    event = SignalEvent(
        symbol="000001",
        name="通知测试",
        timeframe="5m",
        signal_type="buy_3+zero_axis_golden_cross",
        side="buy",
        price=10.0,
        structure_time=now,
        confirmed_at=now,
        score=80,
        evidence={
            "score_reasons": ["缠论buy_3 +30", "MACD 0轴附近金叉 +30", "大周期DIF向上 +15"],
            "latest_center": {"zd": 9.6, "zg": 9.8},
        },
        risk_notice=RISK_NOTICE,
    )
    results: dict[str, dict[str, Any]] = {}
    all_ok = True
    for channel in channels:
        success, detail = monitor.notifier.send(channel, event.to_payload())
        results[channel] = {"success": success, "detail": detail}
        all_ok = all_ok and success
    return _emit(results, 0 if all_ok else 1)


def _cmd_outbox_status(config_path: str, payload: dict[str, Any]) -> int:
    monitor = _make_monitor(config_path, payload.get("overrides"))
    return _emit(monitor.store.outbox_summary())


def _cmd_outbox_log(config_path: str, payload: dict[str, Any]) -> int:
    monitor = _make_monitor(config_path, payload.get("overrides"))
    limit = max(1, min(int(payload.get("limit", 200)), 500))
    return _emit({"records": monitor.store.list_outbox_log(limit)})


def _cmd_notify_summary(config_path: str, payload: dict[str, Any]) -> int:
    content = str(payload.get("content", "")).strip()
    if not content:
        return _emit_error("notify-summary 需要 content", 2)
    monitor = _make_monitor(config_path, payload.get("overrides"))
    return _emit(
        monitor.notify_ai_analysis(
            title=str(payload.get("title", "AI自动解读")),
            content=content,
            report_path=str(payload.get("report_path", "")),
            confirmed_at=str(payload.get("confirmed_at", "")) or None,
        )
    )


def _cmd_candidates(config_path: str, payload: dict[str, Any]) -> int:
    """返回日线零轴金叉指标股票池（候选股，含 TTL 与容量）及失效/过期池。"""
    monitor = _make_monitor(config_path, payload.get("overrides"))
    candidates = monitor.store.active_candidates(limit=monitor.candidate_limit)
    limit = max(1, min(int(payload.get("expired_limit", 100)), 500))
    return _emit(
        {
            "candidates": candidates,
            "ttl_business_days": monitor.candidate_ttl,
            "capacity": monitor.candidate_limit,
            "expired_candidates": monitor.store.list_expired_candidates(limit=limit),
            "expired_count": monitor.store.expired_candidate_count(),
        }
    )


def _cmd_calendar(config_path: str, payload: dict[str, Any]) -> int:
    try:
        monitor = _make_monitor(config_path, payload.get("overrides"))
        is_trading_day = monitor.is_trading_day()
        is_trading_session = monitor.is_trading_session()
    except Exception:
        is_trading_day = now_shanghai().weekday() < 5
        is_trading_session = False
    return _emit(
        {
            "is_trading_day": bool(is_trading_day),
            "is_trading_session": bool(is_trading_session),
            "now": now_shanghai().isoformat(timespec="seconds"),
        }
    )


COMMANDS = {
    "config": lambda p, o: _cmd_config(p, o),
    "normalize": lambda p, o: _cmd_normalize((o or {}).get("text", "")),
    "analyze": lambda p, o: _cmd_analyze(p, o),
    "scan": lambda p, o: _cmd_scan(p, o),
    "monitor-once": lambda p, o: _cmd_monitor_once(p, o),
    "dispatch-outbox": lambda p, o: _cmd_dispatch(p, o),
    "test-notify": lambda p, o: _cmd_test_notify(p, o),
    "outbox-status": lambda p, o: _cmd_outbox_status(p, o),
    "outbox-log": lambda p, o: _cmd_outbox_log(p, o),
    "notify-summary": lambda p, o: _cmd_notify_summary(p, o),
    "candidates": lambda p, o: _cmd_candidates(p, o),
    "calendar": lambda p, o: _cmd_calendar(p, o),
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="signal_system web bridge")
    parser.add_argument("command", choices=sorted(COMMANDS))
    parser.add_argument("--config", default=_default_config_path(), help="配置文件路径")
    parser.add_argument("--payload", default="-", help="JSON 载荷；'-' 表示从 stdin 读取，'@path' 表示从文件读取")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        payload = _read_payload(args.payload)
        return COMMANDS[args.command](args.config, payload)
    except (ValueError, TypeError) as exc:
        return _emit_error(str(exc), 2)
    except KeyboardInterrupt:
        return _emit_error("用户中断", 130)
    except Exception as exc:  # noqa: BLE001 - bridge boundary must never crash
        print(json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False), file=sys.stderr)
        return _emit_error(f"{type(exc).__name__}: {exc}", 1)


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")
    raise SystemExit(main())
