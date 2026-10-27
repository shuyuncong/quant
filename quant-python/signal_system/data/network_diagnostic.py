"""Network diagnostic for the market-data endpoints (Tencent / Eastmoney / TDX).

Run this on the machine that actually fetches quotes (the container, not the
laptop) when scans keep logging "501 Not Implemented" or "Remote end closed
connection without response".  It separates the variables that matter:

* direct egress vs. HTTP(S)_PROXY env vs. an explicit MARKET_DATA_PROXY
* the public IP each path exits from (source-side blocks are IP based)
* DNS resolution for each quote host
* whether the failure is an HTTP refusal (501) or a TCP-level drop

The quote endpoints are *not* blocked by geography: a single request from
Tokyo/Hong Kong/US nodes reaches both Tencent and Eastmoney.  A server that
fails every request is being rejected by the source edge because of its
egress IP / proxy / request rate, so the egress IP is printed explicitly.
"""

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
        "name": "tencent_kline",
        "url": "https://web.ifzq.gtimg.cn/appstock/app/kline/kline",
        "params": {"param": "sh600276,day,,,5"},
    },
    {
        "name": "tencent_fqkline",
        "url": "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get",
        "params": {"param": "sh600276,day,,,5,qfq"},
    },
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


def dns_check(host: str) -> CheckResult:
    try:
        infos = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
        addresses = sorted({info[4][0] for info in infos})
        return CheckResult(name=f"dns:{host}", ok=bool(addresses), detail=", ".join(addresses))
    except Exception as exc:
        return CheckResult(name=f"dns:{host}", ok=False, detail=f"{type(exc).__name__}: {exc}")


def http_check(
    name: str,
    url: str,
    params: Optional[Dict] = None,
    mode: str = "direct",
    proxy: str = "",
    timeout: float = 15.0,
) -> CheckResult:
    session = requests.Session()
    # direct = ignore env proxies; env = honour them; proxy = explicit candidate.
    session.trust_env = mode == "env"
    if proxy:
        session.proxies = {"http": proxy, "https": proxy}
    try:
        response = session.get(
            url,
            params=params,
            timeout=timeout,
            headers={
                "User-Agent": "Mozilla/5.0 quant-signal-monitor/1.0",
                "Referer": "https://quote.eastmoney.com/",
            },
        )
        prefix = response.text[:80].replace("\n", " ")
        return CheckResult(
            name=f"{name}:{mode}",
            ok=response.ok,
            detail=f"status={response.status_code} prefix={prefix}",
        )
    except Exception as exc:
        return CheckResult(
            name=f"{name}:{mode}",
            ok=False,
            detail=f"{type(exc).__name__}: {exc}",
        )


def egress_check(mode: str, proxy: str = "", timeout: float = 10.0) -> CheckResult:
    """Print the public IP (and country when available) for one egress path."""
    session = requests.Session()
    session.trust_env = mode == "env"
    if proxy:
        session.proxies = {"http": proxy, "https": proxy}
    try:
        response = session.get(
            "https://www.cloudflare.com/cdn-cgi/trace", timeout=timeout
        )
        fields = dict(
            line.split("=", 1)
            for line in response.text.splitlines()
            if "=" in line
        )
        ip = fields.get("ip", "")
        location = fields.get("loc", "")
        return CheckResult(
            name=f"egress:{mode}",
            ok=bool(ip),
            detail=f"ip={ip or 'unknown'} loc={location or 'unknown'}",
        )
    except Exception as exc:
        return CheckResult(
            name=f"egress:{mode}",
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


def _lookup(results: List[CheckResult], name: str) -> Optional[CheckResult]:
    return next((result for result in results if result.name == name), None)


def build_summary(
    results: List[CheckResult],
    proxy_env: Dict[str, str],
    market_proxy: str = "",
) -> Dict:
    failures = [result for result in results if not result.ok]
    summary = {
        "proxy_env": proxy_env,
        "market_data_proxy": market_proxy,
        "results": [asdict(result) for result in results],
        "all_ok": not failures,
        "failed_count": len(failures),
        "diagnosis": [],
    }

    if proxy_env:
        summary["diagnosis"].append(
            "检测到代理环境变量；requests/urllib 默认会走它。行情层用 "
            "data.disable_system_proxy=true 直连，或只用 MARKET_DATA_PROXY 指定行情代理。"
        )
    if market_proxy:
        summary["diagnosis"].append(f"行情专用代理 MARKET_DATA_PROXY={market_proxy}。")

    for target in DEFAULT_HTTP_TARGETS:
        name = target["name"]
        direct = _lookup(results, f"{name}:direct")
        env = _lookup(results, f"{name}:env")
        explicit = _lookup(results, f"{name}:proxy") if market_proxy else None

        if direct is None:
            continue
        if direct.ok and env is not None and not env.ok:
            summary["diagnosis"].append(
                f"{name}: 直连成功、系统代理失败 → 代理环境变量在改写行情请求，"
                "需要直连或改为 MARKET_DATA_PROXY。"
            )
        if direct.ok:
            if explicit is not None and not explicit.ok:
                summary["diagnosis"].append(
                    f"{name}: 直连成功、MARKET_DATA_PROXY 失败 → 该代理出口不可用于行情，"
                    "不要设置或换一个出口。"
                )
            continue

        detail = direct.detail
        if "501" in detail:
            summary["diagnosis"].append(
                f"{name}: 源站返回 501（请求已到达边缘后被拒），是出口 IP/代理被源站拦截，"
                "不是 DNS 或地理位置封锁。"
            )
        elif "RemoteDisconnected" in detail or "Remote end closed" in detail:
            summary["diagnosis"].append(
                f"{name}: 对端直接断开 TCP（反爬/限流/出口 IP 被拦），换出口 IP 或降低请求频率。"
            )
        if explicit is not None and explicit.ok:
            summary["diagnosis"].append(
                f"{name}: 经 MARKET_DATA_PROXY 可访问 → 保留该代理，不要把全局 "
                "HTTPS_PROXY 一起绕进行情请求。"
            )
        if env is not None and env.ok and not (explicit is not None and explicit.ok):
            summary["diagnosis"].append(
                f"{name}: 经系统代理可访问 → 当前直连出口被拦；可恢复系统代理，"
                "但更推荐只给行情设 MARKET_DATA_PROXY。"
            )

    direct_egress = _lookup(results, "egress:direct")
    proxy_egress = _lookup(results, "egress:proxy") if market_proxy else None
    if direct_egress is not None and proxy_egress is not None:
        summary["diagnosis"].append(
            f"直连出口 {direct_egress.detail}；行情代理出口 {proxy_egress.detail}。"
            "源站按出口 IP 判定，两个出口不同即可解释“本机可、服务器不可”。"
        )

    if any(result.name == "pytdx_import" and not result.ok for result in results):
        summary["diagnosis"].append("pytdx 未安装或导入失败，分钟级链路无法启动。")
    if any(":7709" in result.name and not result.ok for result in results):
        summary["diagnosis"].append("当前机器无法连到 TDX 行情端口 7709。")
    if not summary["diagnosis"]:
        summary["diagnosis"].append(
            "行情源与测试的出口均可达。若生产仍失败，核对容器内的出口"
            "（TUN/透明代理不会被 disable_system_proxy 绕过）。"
        )

    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description="Market-data network diagnostic")
    parser.add_argument("--output", type=str, default="", help="Optional JSON output path")
    parser.add_argument(
        "--proxy",
        type=str,
        default="",
        help="Explicit proxy to test, e.g. http://host:7890 (default: MARKET_DATA_PROXY)",
    )
    parser.add_argument("--timeout", type=float, default=15.0)
    parser.add_argument("--skip-pytdx", action="store_true", help="Skip TDX socket checks")
    args = parser.parse_args()

    results: List[CheckResult] = []
    proxy_env = collect_proxy_env()
    market_proxy = (args.proxy or os.getenv("MARKET_DATA_PROXY", "")).strip()

    results.append(egress_check("direct", timeout=args.timeout))
    if proxy_env:
        results.append(egress_check("env", timeout=args.timeout))
    if market_proxy:
        results.append(egress_check("proxy", proxy=market_proxy, timeout=args.timeout))

    for target in DEFAULT_HTTP_TARGETS:
        results.append(
            http_check(
                target["name"], target["url"], target["params"], "direct", timeout=args.timeout
            )
        )
        if proxy_env:
            results.append(
                http_check(
                    target["name"], target["url"], target["params"], "env", timeout=args.timeout
                )
            )
        if market_proxy:
            results.append(
                http_check(
                    target["name"],
                    target["url"],
                    target["params"],
                    "proxy",
                    proxy=market_proxy,
                    timeout=args.timeout,
                )
            )

    hostnames = {target["url"].split("://", 1)[1].split("/", 1)[0] for target in DEFAULT_HTTP_TARGETS}
    for host in sorted(hostnames):
        results.append(dns_check(host))

    if not args.skip_pytdx:
        results.append(check_pytdx_import())
        for host in DEFAULT_TDX_HOSTS:
            results.append(socket_check(host, 7709))

    summary = build_summary(results, proxy_env, market_proxy)

    print("=== Proxy Environment ===")
    if proxy_env:
        for key, value in proxy_env.items():
            print(f"{key}={value}")
    else:
        print("none")
    print(f"MARKET_DATA_PROXY={market_proxy or 'none'}")

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
    return 0 if summary["all_ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
