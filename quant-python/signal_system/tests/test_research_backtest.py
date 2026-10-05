import unittest
from unittest.mock import patch
import pandas as pd
from research_backtest import metrics, replay, assert_local_research, entry_indices
from strategy.registry import STRATEGIES, evaluate_strategies


class ResearchBacktestTests(unittest.TestCase):
    def test_metrics_include_initial_drawdown_and_cash_days(self):
        curve=[{"equity":100},{"equity":90},{"equity":110}]
        result=metrics(curve,[{"pnl_cash":20},{"pnl_cash":-10},{"pnl_cash":0}],100)
        self.assertAlmostEqual(result["max_drawdown_pct"],10)
        self.assertAlmostEqual(result["win_rate_pct"],100/3)
        self.assertEqual(result["payoff_ratio"],2)
        self.assertIsNone(metrics([{"equity":100}]*3,[],100)["sharpe_ratio"])

    def test_replay_does_not_sell_on_entry_day_and_keeps_full_calendar(self):
        days=pd.bdate_range("2024-01-01",periods=280)
        frame=pd.DataFrame({"datetime":days,"open":10.,"high":10.,"low":10.,"close":10.,"volume":10000.,"is_closed":True})
        frame.loc[271,"low"]=8
        frame.loc[272,"low"]=8
        symbol="600000.SH"
        signals={symbol:{key:set() for key in STRATEGIES}}
        signals[symbol]["macd_zero_axis"]={270}
        calendar=[str(day.date()) for day in days[269:275]]
        options={"start":calendar[0],"initial_cash":100000,"max_positions":4,"position_size_pct":.25}
        result=replay({symbol:frame},signals,calendar,{"backtest":{"price_limit_model":"none"}},options,"macd_zero_axis")
        self.assertEqual(len(result["equity_curve"]),6)
        self.assertEqual(result["equity_curve"][0]["equity"],100000)
        self.assertEqual(result["trades"][0]["entry_day"],str(days[271].date()))
        self.assertEqual(result["trades"][0]["exit_day"],str(days[272].date()))

    def test_production_tunnel_is_rejected(self):
        with patch.dict("os.environ",{"QUANT_RESEARCH_LOCAL":"1","DATABASE_URL":"postgresql://user:pass@127.0.0.1:15432/quant"}):
            with self.assertRaises(ValueError): assert_local_research()

    def test_fast_candidate_selection_agrees_with_canonical_predicate(self):
        import numpy as np
        days=pd.bdate_range("2024-01-01",periods=290)
        prices=np.linspace(10,20,290)+np.sin(np.arange(290)/5)
        frame=pd.DataFrame({"datetime":days,"open":prices,"high":prices+.2,"low":prices-.2,"close":prices+.1,"volume":10000.,"is_closed":True})
        config={"macd_divergence":{"enabled":True}}
        signals=entry_indices(frame,config)
        for index in range(275,290):
            for result in evaluate_strategies(frame.iloc[:index+1],config):
                self.assertEqual(index in signals[result["strategy_id"]],result["buy"],(index,result))
