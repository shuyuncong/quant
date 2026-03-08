"""Symbol conversion helpers for different market data providers."""

from __future__ import annotations

from typing import Tuple


def normalize_ts_code(symbol: str) -> str:
    """Normalize symbol into the internal `000001.SZ` / `600000.SH` format."""
    if not symbol:
        return ""

    raw = str(symbol).strip().upper()
    if "." in raw:
        code, exchange = raw.split(".", 1)
        exchange = exchange.upper()
        if exchange in {"SH", "SZ", "BJ"}:
            return f"{code}.{exchange}"

    if raw.startswith(("SH", "SZ", "BJ")) and len(raw) > 2:
        exchange = raw[:2]
        code = raw[2:]
        return f"{code}.{exchange}"

    if raw.isdigit():
        if raw.startswith(("6", "9")):
            exchange = "SH"
        elif raw.startswith(("4", "8")):
            exchange = "BJ"
        else:
            exchange = "SZ"
        return f"{raw}.{exchange}"

    return raw


def split_ts_code(symbol: str) -> Tuple[str, str]:
    """Split normalized ts_code into `(code, exchange)`."""
    normalized = normalize_ts_code(symbol)
    if "." not in normalized:
        return normalized, ""
    code, exchange = normalized.split(".", 1)
    return code, exchange


def to_akshare_symbol(symbol: str, with_exchange_prefix: bool = False) -> str:
    """Convert ts_code into the code format commonly used by AKShare."""
    code, exchange = split_ts_code(symbol)
    if with_exchange_prefix:
        prefix = exchange.lower()
        return f"{prefix}{code}" if prefix else code
    return code


def to_pytdx_params(symbol: str) -> Tuple[int, str]:
    """Convert ts_code into `(market, code)` for pytdx."""
    code, exchange = split_ts_code(symbol)
    market = 1 if exchange == "SH" else 0
    return market, code
