"""
量化交易信号系统 - 主程序
整合所有模块，实现每日扫描和信号推送
"""

import sys
import os
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from data.data_fetcher import DataFetcher
from strategy.indicators import TechnicalIndicators
from strategy.strategy_engine import StrategyEngine
from notification.notifier import NotificationService
from utils.helpers import load_config, setup_logging, save_signal_history, load_positions
import logging
import argparse
from datetime import datetime

logger = logging.getLogger(__name__)


class SignalSystem:
    """量化交易信号系统"""

    def __init__(self, config_path='config/config.yaml'):
        """
        初始化系统

        Args:
            config_path: str, 配置文件路径
        """
        self.config = load_config(config_path)
        if self.config is None:
            raise Exception("配置文件加载失败")

        setup_logging(self.config)

        logger.info("=" * 60)
        logger.info("量化交易信号系统启动")
        logger.info("=" * 60)

        tushare_token = self.config['data_source']['tushare_token']
        use_cache = self.config['data_source']['use_cache']
        cache_dir = self.config['data_source']['cache_dir']

        self.data_fetcher = DataFetcher(
            tushare_token=tushare_token,
            use_cache=use_cache,
            cache_dir=cache_dir,
            config=self.config,
        )

        self.technical = TechnicalIndicators()

        self.strategy_engine = StrategyEngine(
            config=self.config,
            data_fetcher=self.data_fetcher,
            technical_indicators=self.technical
        )

        self.notifier = NotificationService(self.config)

        logger.info("系统初始化完成")

    def run_daily_scan(self, send_notification=True):
        """
        执行每日扫描

        Args:
            send_notification: bool, 是否发送通知

        Returns:
            dict: 扫描结果
        """
        logger.info("开始执行每日扫描...")

        positions = load_positions()
        logger.info(f"当前持仓: {len(positions)} 只")

        scan_result = self.strategy_engine.run_daily_scan(positions=positions)

        if scan_result is None:
            logger.error("扫描失败")
            return None

        save_signal_history(scan_result)

        if send_notification:
            logger.info("发送通知...")
            self.notifier.send_daily_report(scan_result)

        self._print_summary(scan_result)

        return scan_result

    def _print_summary(self, scan_result):
        """
        打印扫描摘要

        Args:
            scan_result: dict, 扫描结果
        """
        print("\n" + "=" * 60)
        print("📊 扫描摘要")
        print("=" * 60)
        print(f"扫描时间: {scan_result['scan_time']}")
        print(f"市场状态: {scan_result['market_status']}")
        print(f"\n统计信息:")
        print(f"  - 总股票数: {scan_result['stats']['total_stocks']}")
        print(f"  - 基本面通过: {scan_result['stats']['fundamental_passed']}")
        print(f"  - 成交量通过: {scan_result['stats']['volume_passed']}")
        print(f"  - 技术分析: {scan_result['stats']['technical_analyzed']}")
        print(f"  - 候选池: {scan_result['stats'].get('candidate_pool_count', 0)}")
        print(f"  - 买入信号: {scan_result['stats']['buy_signals_count']}")
        print(f"  - 卖出信号: {scan_result['stats']['sell_signals_count']}")
        print(f"  - 做T信号: {scan_result['stats'].get('t_signals_count', 0)}")
        print(f"  - 风险提示: {scan_result['stats'].get('risk_alerts_count', 0)}")

        if scan_result['buy_signals']:
            print(f"\n🟢 买入信号 (前5个):")
            for i, signal in enumerate(scan_result['buy_signals'][:5], 1):
                print(f"  {i}. {signal['name']} ({signal['ts_code']})")
                print(f"     类型: {signal.get('signal_type', 'BUY')} | 价格: ¥{signal['price']:.2f} | 评分: {signal['score']}")
                print(f"     变化: {signal.get('suggested_position_change', 0):+.0%} | 原因: {signal['reason']}")
                print(f"     信号: {', '.join(signal['signals'])}")

        if scan_result['sell_signals']:
            print(f"\n🔴 卖出信号:")
            for i, signal in enumerate(scan_result['sell_signals'], 1):
                profit_emoji = '📈' if signal['profit_pct'] > 0 else '📉'
                print(f"  {i}. {signal['name']} ({signal['ts_code']})")
                print(f"     类型: {signal.get('signal_type', 'SELL')} | 建议变化: {signal.get('suggested_position_change', 0):+.0%}")
                print(f"     买入: ¥{signal['buy_price']:.2f} | 当前: ¥{signal['current_price']:.2f}")
                print(f"     盈亏: {profit_emoji} {signal['profit_pct']*100:.2f}%")
                print(f"     原因: {', '.join(signal['reasons'])}")

        if scan_result.get('t_signals'):
            print(f"\n🔁 做T信号:")
            for i, signal in enumerate(scan_result['t_signals'][:5], 1):
                print(f"  {i}. {signal.get('name', '')} ({signal['ts_code']})")
                print(f"     类型: {signal['signal_type']} | 建议变化: {signal.get('suggested_position_change', 0):+.0%}")
                print(f"     原因: {signal['reason']}")

        if scan_result.get('risk_alerts'):
            print(f"\n⚠️ 风险提示:")
            for i, signal in enumerate(scan_result['risk_alerts'][:5], 1):
                print(f"  {i}. {signal.get('name', '')} ({signal['ts_code']})")
                print(f"     类型: {signal['signal_type']} | 建议变化: {signal.get('suggested_position_change', 0):+.0%}")
                print(f"     原因: {', '.join(signal['reasons'])}")

        print("=" * 60 + "\n")

    def test_notification(self):
        """
        测试通知功能
        """
        logger.info("测试通知功能...")

        test_result = {
            'scan_time': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'market_status': 'bull',
            'buy_signals': [
                {
                    'ts_code': '000001.SZ',
                    'name': '平安银行',
                    'price': 12.50,
                    'score': 88,
                    'signal_type': 'BUY',
                    'action': '买入',
                    'signals': ['年线向上', '底背离', 'MACD金叉'],
                    'reason': '战略买入点',
                    'explanation': '买入依据: 年线向上 + 底背离 + MACD金叉',
                    'suggested_position_change': 0.25,
                    'roe': 12.5,
                    'pe': 5.8,
                    'market_cap': 2400
                }
            ],
            'sell_signals': [],
            'stats': {
                'total_stocks': 5000,
                'fundamental_passed': 500,
                'volume_passed': 200,
                'technical_analyzed': 150,
                'candidate_pool_count': 42,
                'buy_signals_count': 1,
                'sell_signals_count': 0,
                't_signals_count': 0,
                'risk_alerts_count': 0,
            }
        }

        success = self.notifier.send_daily_report(test_result)

        if success:
            logger.info("✅ 通知测试成功")
        else:
            logger.warning("⚠️ 通知测试失败，请检查配置")

        return success


def main():
    """主函数"""
    parser = argparse.ArgumentParser(description='量化交易信号系统')
    parser.add_argument('--config', type=str, default='config/config.yaml',
                        help='配置文件路径')
    parser.add_argument('--no-notify', action='store_true',
                        help='不发送通知')
    parser.add_argument('--test-notify', action='store_true',
                        help='测试通知功能')

    args = parser.parse_args()

    try:
        system = SignalSystem(config_path=args.config)

        if args.test_notify:
            system.test_notification()
        else:
            system.run_daily_scan(send_notification=not args.no_notify)

        logger.info("系统运行完成")

    except KeyboardInterrupt:
        logger.info("用户中断")
    except Exception as e:
        logger.error(f"系统运行异常: {e}", exc_info=True)
        sys.exit(1)


if __name__ == '__main__':
    main()
