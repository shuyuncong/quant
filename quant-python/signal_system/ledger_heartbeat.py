"""纸面账本心跳检查：账本没推进到最新交易日就报警。

ledger_cron.sh 只在推进命令失败时报警；cron 没在跑、crontab 被清空、Web 的定时
任务被关掉时，什么都不会发生，也就没有报警。这个脚本从另一头查：每个交易日晚上
看各账本记录的最后一个交易日，落后于最新交易日就推送到同一组通知渠道。

它由 systemd 用户定时器运行（scripts/systemd/），不走 cron，所以 cron 出问题时
它还在。管不到的情况：整台机器关机或断网。

用法::

    python ledger_heartbeat.py            # 有落后就报警，退出码 1
    python ledger_heartbeat.py --dry-run  # 只打印，不发通知
"""

from __future__ import annotations

import argparse
import json
from datetime import date
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parent
TITLE = "纸面账本没有推进"
# 名称、账本文件、记录最后一个交易日的字段。
LEDGERS: list[tuple[str, Path, str]] = [
    ("首板低开 100 万", BASE_DIR / "state" / "forward_low_open_1m" / "ledger.json", "last_session"),
    ("ETF 50 万", BASE_DIR / "state" / "forward_paper_500k" / "etf_paper_ledger.json", "as_of"),
    ("ETF 25 万（Web）", Path("/opt/docker/quant/state/etf_paper_ledger.json"), "as_of"),
]


def find_stale(ledgers: list[tuple[str, Path, str]], expected: date) -> list[str]:
    """没推进到 ``expected`` 的账本各一行说明；读不了的账本也算。"""

    problems: list[str] = []
    for name, path, field in ledgers:
        try:
            last = date.fromisoformat(str(json.loads(path.read_text(encoding="utf-8"))[field])[:10])
        except Exception as exc:  # noqa: BLE001 - 读不了本身就是要报的问题
            problems.append(f"{name}：账本读不了（{type(exc).__name__}: {exc}）")
            continue
        if last < expected:
            problems.append(f"{name}：停在 {last}，应到 {expected}")
    return problems


def expected_session(config: dict[str, Any]) -> date:
    from data.market_data import MarketDataClient

    return MarketDataClient(config).latest_expected_trade_date()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="纸面账本心跳检查")
    parser.add_argument("--config", default=str(BASE_DIR / "config" / "config.yaml"))
    parser.add_argument("--dry-run", action="store_true", help="只打印，不发通知")
    args = parser.parse_args(argv)

    import ledger_alert
    from utils.helpers import load_config

    config = load_config(args.config)
    result: dict[str, Any] = {"heartbeat": TITLE}
    try:
        if config is None:
            raise RuntimeError(f"配置文件加载失败: {args.config}")
        expected = expected_session(config)
        result["expected"] = expected.isoformat()
        problems = find_stale(LEDGERS, expected)
    except Exception as exc:  # noqa: BLE001 - 检查本身失败也要报出来
        problems = [f"心跳检查本身失败：{type(exc).__name__}: {exc}"]
    result["problems"] = problems
    if problems and not args.dry_run:
        try:
            settings = ledger_alert.apply_settings(config or {}, ledger_alert.read_db_settings())
            result["channels"] = ledger_alert.send(TITLE, "\n".join(problems), settings)
        except Exception as exc:  # noqa: BLE001 - 报警发不出去只能留在日志里
            result["alert_error"] = f"{type(exc).__name__}: {exc}"
    print(json.dumps(result, ensure_ascii=False))
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
