"""Probe JQData permissions and historical coverage.

Credentials are read from environment variables only; never hard-code them:

    PowerShell:
        $env:JQDATA_USER="<your JQData user>"
        $env:JQDATA_PASSWORD="<your password>"
        python jqdata_probe.py

The trial account documented by JQData has a short historical window
(roughly 15 months ago to 3 months ago).  This probe makes that explicit by
testing the dates the backtest actually needs.
"""

from __future__ import annotations

import os
import traceback

import jqdatasdk as jq


DATES = ["2020-01-02", "2022-01-04", "2024-12-31", "2025-08-01", "2026-08-03"]


def main() -> None:
    user = os.environ.get("JQDATA_USER", "").strip()
    password = os.environ.get("JQDATA_PASSWORD", "")
    if not user or not password:
        raise SystemExit("set JQDATA_USER and JQDATA_PASSWORD in the environment")
    jq.auth(user, password)
    print("auth success")
    try:
        print("account_info:", jq.get_account_info())
    except Exception as exc:
        print("account_info failed:", type(exc).__name__, exc)

    for date in DATES:
        try:
            stocks = jq.get_index_stocks("000852.XSHG", date=date)
            print(f"index 000852 {date}: {len(stocks)} stocks; first={stocks[:5]}")
        except Exception as exc:
            print(f"index 000852 {date}: FAIL {type(exc).__name__}: {exc}")

    try:
        all_securities = jq.get_all_securities(types=["stock"], date="2024-12-31")
        print("all securities 2024-12-31:", len(all_securities))
    except Exception as exc:
        print("all securities 2024-12-31: FAIL", type(exc).__name__, exc)

    # A known delisted stock: 000004.XSHE (delisted 2026-07).
    try:
        frame = jq.get_price(
            "000004.XSHE",
            start_date="2020-01-01",
            end_date="2020-12-31",
            frequency="daily",
            fields=["open", "close", "volume", "money"],
        )
        print("delisted 000004 2020 rows:", 0 if frame is None else len(frame))
        if frame is not None and len(frame):
            print(frame.head(2).to_string())
    except Exception as exc:
        print("delisted 000004 2020: FAIL", type(exc).__name__, exc)

    # Check whether the trial can read historical fundamentals.
    try:
        from jqdatasdk import query, valuation

        frame = jq.get_fundamentals(
            query(valuation.code, valuation.market_cap).limit(5), date="2024-12-31"
        )
        print("fundamentals 2024-12-31 rows:", len(frame))
        print(frame.to_string())
    except Exception as exc:
        print("fundamentals 2024-12-31: FAIL", type(exc).__name__, exc)
        traceback.print_exc(limit=1)
    finally:
        jq.logout()


if __name__ == "__main__":
    main()
