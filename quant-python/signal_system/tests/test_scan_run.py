from types import SimpleNamespace
from datetime import date
from unittest.mock import Mock
import pandas as pd
import pytest
from monitor.scan_run import prepare_scan_run


@pytest.fixture
def scan():
    state = {}
    store = SimpleNamespace(get_state=lambda key, default=None: state.get(key, default), set_state=lambda key,value: state.__setitem__(key,value))
    market = SimpleNamespace(latest_expected_trade_date=lambda: date(2026,9,14),get_stock_list=Mock(return_value=pd.DataFrame([{"code":"600036","name":"test"}])))
    return SimpleNamespace(store=store,market=market),state


def test_resume_retains_progress_and_frozen_universe(scan):
    monitor,state=scan
    prepare_scan_run(monitor,"macd_zero_axis","1")
    state['daily_bootstrap_success']='["600036.SH"]'
    prepare_scan_run(monitor,"macd_zero_axis","1")
    assert state['daily_bootstrap_success']=='["600036.SH"]'
    assert list(monitor.market.get_stock_list().code)==['600036']


def test_new_run_resets_only_checkpoints_and_different_day_cannot_resume(scan):
    monitor,state=scan
    prepare_scan_run(monitor,"macd_zero_axis","1")
    monitor.market.latest_expected_trade_date=lambda: date(2026,9,15)
    with pytest.raises(ValueError): prepare_scan_run(monitor,"macd_zero_axis","1")
    prepare_scan_run(monitor,"macd_zero_axis","2")
    assert state['daily_bootstrap_complete']=='false'
    assert state['daily_pool_cache']=='[]'
