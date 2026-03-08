"""Network diagnostic script for AKShare and pytdx data sources."""

from __future__ import annotations

import argparse
import json
import os
import socket
from dataclasses import asdict, dataclass
from typing import Dict, List, Optional

import requests


DEFAULT_HTTP_TARGETS = [
    {
        "name": "eastmoney_kline",
        "url": "https://push2his.eastmoney.com/api/qt/stock/kline/get",
        "params": {
            "secid": "1.600276",
            "klt": "101",
            "fqt": "1",
            "beg": "20260107",
            "end": "20260308",
            "fields1": "f1,f2,f3,f4,f5,f6",
            "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f116",
            "ut": "7eea3edcaed734bea9cbfc24409ed989",
        },
    },
    {
        "name": "eastmoney_spot",
        "url": "https://82.push2.eastmoney.com/api/qt/clist/get",
        "params": {
            "pn": "1",
            "pz": "5",
            "po": "1",
            "np": "1",
            "ut": "bd1d9ddb04089700cf9c27f6f7426281",
            "fltt": "2",
            "invt": "2",
            "fid": "f12",
            "fs": "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81+s:2048",
            "fields": "f12,f14",
        },
    },
]

DEFAULT_TDX_HOSTS = [
    "119.147.212.81",
    "119.147.212.83",
    "119.147.212.84",
    "61.152.249.56",
    "218.108.98.244",
    "113.105.73.88",
]


@dataclass
class CheckResult:
    name: str
    ok: bool
    detail: str


def socket_check(host: str, port: int, timeout: float = 3.0) -> CheckResult:
    sock = socket.socket()
    sock.settimeout(timeout)
    try:
        sock.connect((host, port))
        return CheckResult(name=f"{host}:{port}", ok=True, detail="tcp_connect_ok")
    except Exception as exc:
        return CheckResult(name=f"{host}:{port}", ok=False, detail=f"{type(exc).__name__}: {exc}")
    finally:
        sock.close()


def http_check(
    name: str,
    url: str,
    params: Optional[Dict] = None,
    trust_env: bool = True,
    timeout: float = 15.0,
) -> CheckResult:
    session = requests.Session()
    session.trust_env = trust_env
    try:
        response = session.get(url, params=params, timeout=timeout, headers={"User-Agent": "Mozilla/5.0"})
        prefix = response.text[:80].replace("\n", " ")
        return CheckResult(
            name=f"{name}:{'env' if trust_env else 'no_env'}",
            ok=response.ok,
            detail=f"status={response.status_code} prefix={prefix}",
        )
    except Exception as exc:
        return CheckResult(
            name=f"{name}:{'env' if trust_env else 'no_env'}",
            ok=False,
            detail=f"{type(exc).__name__}: {exc}",
        )


def collect_proxy_env() -> Dict[str, str]:
    return {
        key: value
        for key, value in os.environ.items()
        if "proxy" in key.lower()
    }


def check_pytdx_import() -> CheckResult:
    try:
        import pytdx  # noqa: F401

        return CheckResult(name="pytdx_import", ok=True, detail="import_ok")
    except Exception as exc:
        return CheckResult(name="pytdx_import", ok=False, detail=f"{type(exc).__name__}: {exc}")


def build_summary(results: List[CheckResult], proxy_env: Dict[str, str]) -> Dict:
    failures = [result for result in results if not result.ok]
    summary = {
        "proxy_env": proxy_env,
        "results": [asdict(result) for result in results],
        "all_ok": not failures,
        "failed_count": len(failures),
        "diagnosis": [],
    }

    if proxy_env:
        summary["diagnosis"].append("检测到代理环境变量，requests 默认会优先走代理。")
    if any(result.name == "pytdx_import" and not result.ok for result in results):
        summary["diagnosis"].append("pytdx 未安装或导入失败，分钟级链路无法启动。")
    if any("eastmoney" in result.name and not result.ok for result in results):
        summary["diagnosis"].append("AKShare 依赖的东财 HTTP 链路当前不可用。")
    if any(":7709" in result.name and not result.ok for result in results):
        summary["diagnosis"].append("当前机器无法连到 TDX 行情端口 7709。")
    if not summary["diagnosis"]:
        summary["diagnosis"].append("AKShare 和 pytdx 的基础网络链路看起来都正常。")

    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description="Network diagnostic for AKShare and pytdx")
    parser.add_argument("--output", type=str, default="", help="Optional JSON output path")
    args = parser.parse_args()

    results: List[CheckResult] = []
    proxy_env = collect_proxy_env()

    for target in DEFAULT_HTTP_TARGETS:
        results.append(http_check(target["name"], target["url"], params=target["params"], trust_env=True))
        results.append(http_check(target["name"], target["url"], params=target["params"], trust_env=False))

    results.append(check_pytdx_import())

    for host in DEFAULT_TDX_HOSTS:
        results.append(socket_check(host, 7709))

    summary = build_summary(results, proxy_env)

    print("=== Proxy Environment ===")
    if proxy_env:
        for key, value in proxy_env.items():
            print(f"{key}={value}")
    else:
        print("none")

    print("\n=== Checks ===")
    for result in results:
        status = "OK" if result.ok else "FAIL"
        print(f"[{status}] {result.name}: {result.detail}")

    print("\n=== Diagnosis ===")
    for item in summary["diagnosis"]:
        print(f"- {item}")

    if args.output:
        with open(args.output, "w", encoding="utf-8") as handle:
            json.dump(summary, handle, ensure_ascii=False, indent=2)
        print(f"\nJSON written to {args.output}")

    return 0 if summary["all_ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
