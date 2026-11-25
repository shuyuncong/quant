"""首板低开看板：把纸面账本和下一个交易日的观察名单渲染成一页静态 HTML。

页面是给人在开盘前看的，所以最上面是"下一个交易日怎么做"。所有数字都来自
``forward_low_open`` 写好的账本，这里不选股、不记账。
"""

from __future__ import annotations

from datetime import datetime
from fractions import Fraction
from html import escape
from typing import Any

import pandas as pd

RECENT_TRADES = 30
ACTIONS = {"bought": "已买入", "holding": "有持仓，没买", "unfilled": "没买到"}
# 研究面板 2025-01-02 → 2026-10-30 共 441 个交易日：64 个交易日有信号，共 93 只次，
# 占名单只次的 2.27%。只用来说明"看完没有一只触发"是常态。
TRIGGER_NOTE = "2025-01 至 2026-10 的回测里，名单内单只的触发率约 2.3%，约 15% 的交易日有信号。"

TEMPLATE = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>首板低开 · 每日看板</title>
<style>
:root{--bg:#0a0e14;--panel:#111821;--fg:#e6edf5;--muted:#8b9bb0;--border:#263244;--accent:#5b9cf5;--good:#34d399;--bad:#fb7185}
@media (prefers-color-scheme: light){:root{--bg:#f5f7fb;--panel:#fff;--fg:#17202b;--muted:#5c6b7c;--border:#dbe3ec;--accent:#2f6fd0;--good:#059669;--bad:#e11d48}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font-family:system-ui,-apple-system,"Segoe UI","PingFang SC",sans-serif;line-height:1.55}
.wrap{max-width:760px;margin:0 auto;padding:24px 14px 48px}
a{color:var(--accent);text-decoration:none}
.back{font-size:13px;color:var(--muted)}
h1{font-size:24px;margin:10px 0 4px;letter-spacing:-.02em}
.sub{color:var(--muted);font-size:13px;margin:0}
.card{background:var(--panel);border:1px solid var(--border);border-radius:14px;padding:16px;margin:14px 0}
.stale{border-color:var(--bad);color:var(--bad);font-weight:600}
h2{font-size:15px;margin:0 0 10px}
p{margin:6px 0}
ol{margin:6px 0;padding-left:20px}
li{margin:4px 0}
.note{color:var(--muted);font-size:12.5px}
.scroll{overflow-x:auto}
.scroll+.scroll{margin-top:12px}
table{width:100%;border-collapse:collapse;font-size:14px;font-variant-numeric:tabular-nums}
th,td{padding:7px 4px;border-bottom:1px solid var(--border);text-align:right;white-space:nowrap}
th{font-size:12px;color:var(--muted);font-weight:600}
th:first-child,td:first-child{text-align:left}
tr:last-child td{border-bottom:0}
td small{display:block;color:var(--muted);font-size:11.5px}
.band{font-weight:650}
.tag{border:1px solid var(--bad);color:var(--bad);border-radius:6px;padding:0 5px;font-size:11px;margin-left:5px}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(104px,1fr));gap:10px;margin-bottom:10px}
.stats div{border:1px solid var(--border);border-radius:10px;padding:9px 10px}
.stats div>span{display:block;color:var(--muted);font-size:11.5px}
.stats b{font-size:17px;font-variant-numeric:tabular-nums}
.pos{color:var(--good)}
.neg{color:var(--bad)}
</style>
</head>
<body><main class="wrap">
<a class="back" href="../">← 回测中心</a>
<h1>首板低开 · 每日看板</h1>
<p class="sub">依据 __AS_OF__ 收盘 · 用于 __SESSION__ 开盘，遇休市顺延 · 生成于 __GENERATED__</p>
<div class="card stale" id="stale" hidden>这份名单原定用于 __SESSION__。如果那天开过市，名单已经过期：当晚的更新没有成功，不要再用。</div>
<div class="card"><h2>下一个交易日怎么做</h2>__PLAN__</div>
__WATCH__
<div class="card"><h2>__LAST__ 账本记了什么</h2>__TODAY__</div>
<div class="card"><h2>账本概况</h2>__SUMMARY__</div>
__TRADES__
<div class="card"><h2>规则和口径</h2>__RULE__</div>
</main>
<script>
const today = new Intl.DateTimeFormat("en-CA", {timeZone: "Asia/Shanghai"}).format(new Date());
if (today > "__SESSION_ISO__") document.getElementById("stale").hidden = false;
</script>
</body></html>
"""


def _day(value: str) -> str:
    stamp = pd.Timestamp(value)
    return f"{stamp:%Y-%m-%d}（周{'一二三四五六日'[stamp.weekday()]}）"


def _pct(value: float) -> str:
    sign = "pos" if value > 0 else "neg" if value < 0 else ""
    return f'<span class="{sign}">{value * 100:+.2f}%</span>'


def _stock(symbol: str, named: dict[str, dict[str, Any]]) -> str:
    """名称在上、代码在下；名称没取到时只有代码。"""

    row = named.get(symbol)
    if row is None:
        return escape(symbol)
    tag = '<span class="tag">ST</span>' if row["is_st"] else ""
    return f"{escape(row['name'])}{tag}<small>{escape(symbol)}</small>"


def _table(head: list[str], rows: list[list[str]]) -> str:
    def line(tag: str, cells: list[str]) -> str:
        return "<tr>" + "".join(f"<{tag}>{cell}</{tag}>" for cell in cells) + "</tr>"

    return f'<div class="scroll"><table>{line("th", head)}{"".join(line("td", row) for row in rows)}</table></div>'


def _sizing(rule: dict[str, Any]) -> str:
    weight = Fraction(rule["max_weight"]).limit_denominator(100)
    return f"每只 = 总资产 ÷ 信号数，且不超过总资产的 {weight}，按 100 股取整"


def _plan(ledger: dict[str, Any], named: dict[str, dict[str, Any]]) -> str:
    positions = ledger["positions"]
    if positions:
        rows = [
            [
                _stock(row["symbol"], named),
                row["entry_date"][5:],
                f"{row['entry_price']:.2f}",
                f"{row['mark_price']:.2f}",
                _pct(row["mark_price"] / row["entry_price"] - 1.0),
            ]
            for row in positions
        ]
        return (
            f"<p>账本持有 {len(positions)} 只，<b>收盘卖出</b>；收盘涨停的继续拿，跌停卖不掉的顺延。"
            "有持仓时不开新仓，下面的名单只做记录。</p>"
            + _table(["股票", "买入日", "成本", "现价", "浮动盈亏"], rows)
        )
    if not ledger["watchlist"]["rows"]:
        return "<p>账本空仓，名单也是空的：没有符合条件的首板，这一天不会有信号。</p>"
    return (
        "<p>账本空仓。</p><ol>"
        "<li>9:25 竞价结束后，看名单里每只的开盘价。</li>"
        "<li>开盘价落在「买入区间」内（含两端）的就是信号。</li>"
        f"<li>9:30 买入全部信号：{_sizing(ledger['rule'])}。</li>"
        "<li>再下一个交易日收盘卖出；收盘涨停的继续拿。</li></ol>"
    )


def _watch(watch: dict[str, Any], named: dict[str, dict[str, Any]]) -> str:
    rows = watch["rows"]
    if not rows:
        return ""
    low, high = watch["band"]
    notes = [
        f"名单里的股票已经满足除“低开”以外的全部条件。买入区间 = 昨收 × {low:g} ~ {high:g}；"
        "遇除权除息，以行情软件显示的昨收为准。",
        TRIGGER_NOTE,
    ]
    if any(row["symbol"] not in named for row in rows):
        notes.append("部分名称没取到，只显示代码，也没有 ST 标记。")
    if any(named.get(row["symbol"], {}).get("is_st") for row in rows):
        notes.append("带 ST 标记的账本照常记录；历史复测里去掉 ST 后略好，实盘建议跳过。")
    body = [
        [
            _stock(row["symbol"], named),
            f"{row['prev_close']:.2f}",
            f'<span class="band">{row["low"]:.2f} – {row["high"]:.2f}</span>',
            f"{row['rp60'] * 100:.0f}%",
        ]
        for row in rows
    ]
    return (
        f'<div class="card"><h2>观察名单（{len(rows)} 只）</h2>'
        + _table(["股票", "昨收", "买入区间", "60 日位置"], body)
        + "".join(f'<p class="note">{note}</p>' for note in notes)
        + "</div>"
    )


def _today(ledger: dict[str, Any], named: dict[str, dict[str, Any]]) -> str:
    day = ledger["last_session"]
    signals = [
        [
            _stock(row["symbol"], named),
            f"{row['open']:.2f}",
            f"{(1.0 - row['open_ratio']) * 100:.2f}%",
            ACTIONS[row["action"]],
        ]
        for row in ledger["signals"]
        if row["date"] == day
    ]
    sold = [
        [
            _stock(row["symbol"], named),
            row["entry_date"][5:],
            f"{row['entry_price']:.2f}",
            f"{row['exit_price']:.2f}",
            _pct(row["net_return"]),
        ]
        for row in ledger["trades"]
        if row["exit_date"] == day
    ]
    parts = []
    if signals:
        parts.append(_table(["信号", "开盘", "低开", "账本"], signals))
    if sold:
        parts.append(_table(["卖出", "买入日", "买入价", "卖出价", "净收益"], sold))
    return "".join(parts) or "<p>当天没有信号，也没有卖出。</p>"


def _summary(ledger: dict[str, Any], summary: dict[str, Any]) -> str:
    closed = summary["trades"]
    cells = [
        ("权益", f"{summary['equity']:,.0f}"),
        ("累计收益", _pct(summary["pnl_pct"] / 100.0)),
        ("已平仓", f"{closed} 笔"),
        ("胜率", f"{summary['win_rate_pct']:.0f}%" if closed else "—"),
        ("单笔净收益均值", _pct(summary["mean_net_return_pct"] / 100.0) if closed else "—"),
    ]
    nav = ledger["nav_history"]
    since = f"从 {nav[0]['date']} 起；" if nav else ""
    return (
        '<div class="stats">'
        + "".join(f"<div><span>{label}</span><b>{value}</b></div>" for label, value in cells)
        + "</div>"
        f'<p class="note">样本外纸面账本，{since}其中 {summary["backfilled_trades"]} 笔是事后补记的。'
        "满 30 笔做第一次复盘，之前不下结论。</p>"
    )


def _trades(ledger: dict[str, Any], named: dict[str, dict[str, Any]]) -> str:
    rows = ledger["trades"][::-1][:RECENT_TRADES]
    if not rows:
        return ""
    body = [
        [
            _stock(row["symbol"], named),
            row["entry_date"][5:],
            row["exit_date"][5:],
            _pct(row["net_return"]) + ("<small>补记</small>" if row["backfilled"] else ""),
        ]
        for row in rows
    ]
    return (
        f'<div class="card"><h2>成交记录（最近 {len(rows)} 笔）</h2>'
        + _table(["股票", "买入日", "卖出日", "净收益"], body)
        + "</div>"
    )


def _rule(rule: dict[str, Any]) -> str:
    lines = [
        f"信号：{rule['signal']}。",
        f"买入：空仓时开盘买入当日全部信号，{_sizing(rule)}；有持仓时出现的信号只记录、不买。",
        f"卖出：{rule['sell']}。",
        f"纸面账本，不接券商、不下单。买入价按开盘价加 {rule['slippage'] * 100:g}% 滑点记；"
        "9:30 之后实际能买到的价格通常更高。",
    ]
    return "".join(f'<p class="note">{escape(line)}</p>' for line in lines)


def render(ledger: dict[str, Any], summary: dict[str, Any], generated: datetime) -> str:
    """``summary`` 是 ``forward_low_open.status(ledger)``；``ledger`` 里必须已有 ``watchlist``。"""

    watch = ledger["watchlist"]
    named = {
        row["symbol"]: row for row in (*ledger["signals"], *watch["rows"]) if "name" in row
    }
    parts = {
        "__AS_OF__": _day(watch["as_of"]),
        "__LAST__": _day(ledger["last_session"]),
        "__SESSION_ISO__": watch["session"],
        "__SESSION__": _day(watch["session"]),
        "__GENERATED__": f"{generated:%m-%d %H:%M}",
        "__PLAN__": _plan(ledger, named),
        "__WATCH__": _watch(watch, named),
        "__TODAY__": _today(ledger, named),
        "__SUMMARY__": _summary(ledger, summary),
        "__TRADES__": _trades(ledger, named),
        "__RULE__": _rule(ledger["rule"]),
    }
    page = TEMPLATE
    for key, value in parts.items():
        page = page.replace(key, value)
    return page
