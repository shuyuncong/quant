"""纸面账本定时任务的失败报警（由 scripts/ledger_cron.sh 调用）。

通知渠道沿用信号系统的配置：config.yaml 打底，Web 设置页保存在数据库
``quant.settings`` 里的 ``notification.*`` 覆盖其上，再用同一个 ``SignalNotifier``
发出。宿主机的 cron 拿不到 Web 进程下发的配置，所以这里直接读数据库。

用法::

    echo "错误正文" | python ledger_alert.py --title "ETF 纸面账本推进失败"

至少一个渠道发送成功返回 0；没有启用任何渠道、或全部发送失败返回 1。
"""

from __future__ import annotations

import argparse
import json
import socket
import subprocess
import sys
from pathlib import Path
from typing import Any

from notification.signal_notifier import SignalNotifier
from utils.helpers import load_config
from utils.time_utils import now_shanghai

BASE_DIR = Path(__file__).resolve().parent
DB_CONTAINER = "quant-db"
DB_USER = "quant_owner"
DB_NAME = "quant"
SETTINGS_QUERY = (
    "select coalesce(json_object_agg(key, value), '{}') "
    "from quant.settings where key like 'notification.%'"
)


def read_db_settings() -> dict[str, str]:
    """``quant.settings`` 里的 ``notification.*``：点号路径 → Web 端写入的 JSON 文本。"""

    result = subprocess.run(
        ["docker", "exec", DB_CONTAINER, "psql", "-U", DB_USER, "-d", DB_NAME, "-Atc", SETTINGS_QUERY],
        capture_output=True, text=True, timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(f"读取通知设置失败: {result.stderr.strip()}")
    return json.loads(result.stdout)


def apply_settings(config: dict[str, Any], settings: dict[str, str]) -> dict[str, Any]:
    """把点号路径的设置写进配置。值是 JSON 文本；解析不了的按原字符串处理（与 Web 端一致）。"""

    for key, raw in settings.items():
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            value = raw
        *parents, leaf = key.split(".")
        node = config
        for part in parents:
            child = node.get(part)
            if not isinstance(child, dict):
                child = node[part] = {}
            node = child
        node[leaf] = value
    return config


def push(title: str, content: str, config: dict[str, Any], event: str) -> dict[str, str]:
    """向每个已启用的渠道发一条"标题 + 正文"，返回 渠道 → "ok" 或失败原因。"""

    notifier = SignalNotifier(config)
    payload = {
        "event_id": f"{event}-{now_shanghai().isoformat(timespec='seconds')}",
        "symbol": "SYSTEM",
        "name": title,
        "timeframe": "report",
        "side": "info",
        "risk_notice": "纸面账本，不涉及实盘资金",
        # 借用扫描汇总的排版（标题 + 正文），四个渠道都已支持。
        "evidence": {"notification_kind": "scan_summary", "content": content},
    }
    results: dict[str, str] = {}
    for channel in notifier.active_channels():
        ok, detail = notifier.send(channel, payload)
        results[channel] = "ok" if ok else detail
    return results


def send(title: str, body: str, config: dict[str, Any]) -> dict[str, str]:
    """发一条报警：正文前面加上主机名和时间。"""

    stamp = now_shanghai().isoformat(timespec="seconds")
    return push(title, f"{socket.gethostname()} {stamp}\n{body}", config, "ledger-alert")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="纸面账本失败报警；正文从 stdin 读")
    parser.add_argument("--title", required=True)
    parser.add_argument("--config", default=str(BASE_DIR / "config" / "config.yaml"))
    args = parser.parse_args(argv)
    body = sys.stdin.buffer.read().decode("utf-8", "replace").strip()
    try:
        config = load_config(args.config)
        if config is None:
            raise RuntimeError(f"配置文件加载失败: {args.config}")
        results = send(args.title, body, apply_settings(config, read_db_settings()))
    except Exception as exc:  # noqa: BLE001 - CLI boundary
        print(json.dumps({"alert": args.title, "error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False))
        return 1
    print(json.dumps({"alert": args.title, "channels": results}, ensure_ascii=False))
    return 0 if "ok" in results.values() else 1


if __name__ == "__main__":
    raise SystemExit(main())
