"""Network diagnostic for the market-data endpoints (Tencent / Eastmoney / TDX).

Run this on the machine that actually fetches quotes (the container, not the
laptop) when scans keep logging "501 Not Implemented" or "Remote end closed
connection without response".  It separates the variables that matter:

* direct egress vs. HTTP(S)_PROXY env vs. an explicit MARKET_DATA_PROXY
* the public IP each path exits from (source-side blocks are IP based)
* DNS resolution for each quote host
* whether the failure is an HTTP refusal (501) or a TCP-level drop

The quote endpoints are *not* blocked by geography: a single request from
Tokyo/Hong Kong/US nodes reaches Sina, and TDX is a separate TCP path.  Tencent
and Eastmoney, however, do reject some cloud egress IPs: Tencent answers 501,
Eastmoney drops the connection.  The diagnostic prints the egress IP and tests
Sina as the independent source, so a blocked Tencent/Eastmoney can be told apart
from a dead network.
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
        # 生产路径：web.ifzq.gtimg.cn 自 2026-10-30 起对 fqkline/get 返回 501，
        # 日线/复权/日历统一改走 ifzq.gtimg.cn。
        "url": "https://ifzq.gtimg.cn/appstock/app/kline/kline",
        "params": {"param": "sh600276,day,,,5"},
    },
    {
        "name": "tencent_fqkline",
        "url": "https://ifzq.gtimg.cn/appstock/app/fqkline/get",
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
    {
        "name": "sina_kline",
        "url": "https://finance.sina.com.cn/realstock/company/sh600276/hisdata/klc_kl.js",
        "params": {},
    },
    {
        "name": "sina_quote_list",
        "url": (
            "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
            "Market_Center.getHQNodeData"
        ),
        "params": {
            "page": "1",
            "num": "5",
            "sort": "symbol",
            "asc": "1",
            "node": "hs_a",
            "symbol": "",
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


# requests/urllib 只认这些标准变量；空值和行情专用 MARKET_DATA_PROXY
# 不属于“系统代理”，否则空的 MARKET_DATA_PROXY= 会被误报成检测到代理。
_PROXY_ENV_KEYS = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
    "no_proxy",
)


def collect_proxy_env() -> Dict[str, str]:
    return {
        key: value
        for key in _PROXY_ENV_KEYS
        if (value := os.environ.get(key, "")).strip()
    }


def collect_market_config() -> Dict[str, str]:
    keys = ("MARKET_DATA_PROVIDER", "MARKET_DATA_FALLBACKS")
    return {
        key: value.strip()
        for key in keys
        if (value := os.environ.get(key, "")).strip()
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
    market_config: Optional[Dict[str, str]] = None,
) -> Dict:
    market_config = dict(market_config or {})
    provider = str(market_config.get("MARKET_DATA_PROVIDER", "")).strip().lower()
    fallbacks = [
        item.strip().lower()
        for item in str(market_config.get("MARKET_DATA_FALLBACKS", "")).split(",")
        if item.strip()
    ]
    failures = [result for result in results if not result.ok]
    summary = {
        "proxy_env": proxy_env,
        "market_data_proxy": market_proxy,
        "market_config": market_config,
        "market_provider": provider,
        "market_fallbacks": fallbacks,
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

    blocked_http = [
        name
        for name in (
            "tencent_kline",
            "tencent_fqkline",
            "eastmoney_kline",
            "eastmoney_spot",
        )
        if (result := _lookup(results, f"{name}:direct")) is not None and not result.ok
    ]
    tencent_blocked = any(name.startswith("tencent_") for name in blocked_http)
    eastmoney_blocked = any(name.startswith("eastmoney_") for name in blocked_http)
    sina_ok = all(
        (result := _lookup(results, f"{name}:direct")) is not None and result.ok
        for name in ("sina_kline", "sina_quote_list")
    )
    if tencent_blocked and sina_ok:
        if provider == "sina":
            summary["diagnosis"].append(
                "腾讯直连被拒；Sina 已是主源（MARKET_DATA_PROVIDER=sina），无需处理。"
            )
        elif "sina" in fallbacks:
            summary["diagnosis"].append(
                "腾讯直连被拒；当前 MARKET_DATA_FALLBACKS 已含 sina，备用链会自动接管，"
                "无需再改。"
            )
        else:
            summary["diagnosis"].append(
                "腾讯直连被拒，但新浪 K 线与股票列表可访问 → 在 .env 设 "
                "MARKET_DATA_PROVIDER=sina（Compose 已透传），K 线/日历/列表/指数改走新浪；"
                "MARKET_DATA_FALLBACKS 可留空或设为 tencent。"
            )
    elif eastmoney_blocked and sina_ok:
        if provider == "sina":
            summary["diagnosis"].append(
                "东财直连被拒；Sina 已是主源（MARKET_DATA_PROVIDER=sina），"
                "东财不在 K 线链中，无需处理。"
            )
        elif "sina" in fallbacks:
            summary["diagnosis"].append(
                "东财直连被拒；当前 MARKET_DATA_FALLBACKS 已含 sina，K 线链会跳过东财，"
                "股票列表/指数/股票池历史也已跳过，无需再改。"
            )
        else:
            summary["diagnosis"].append(
                "东财直连被拒（腾讯可用）→ 设 MARKET_DATA_FALLBACKS=sina 让备用链接管，"
                "避免每只股票对东财重试；若腾讯也出现间歇 501，改用 MARKET_DATA_PROVIDER=sina。"
            )
    elif blocked_http and not sina_ok:
        summary["diagnosis"].append(
            "腾讯/东财/新浪直连均被拒 → 需要国内出口：给行情单独设 MARKET_DATA_PROXY，"
            "不要把全局 HTTPS_PROXY 复用到行情。"
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
    market_config = collect_market_config()
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

    summary = build_summary(results, proxy_env, market_proxy, market_config)

    print("=== Proxy Environment ===")
    if proxy_env:
        for key, value in proxy_env.items():
            print(f"{key}={value}")
    else:
        print("none")
    print(f"MARKET_DATA_PROXY={market_proxy or 'none'}")

    print("\n=== Market Data Config ===")
    if market_config:
        for key, value in market_config.items():
            print(f"{key}={value}")
    else:
        print("MARKET_DATA_PROVIDER=(config default)")
        print("MARKET_DATA_FALLBACKS=(config default)")

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
