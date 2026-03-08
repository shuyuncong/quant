"""
通知推送模块
支持企业微信、邮件通知
"""

import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import logging
from datetime import datetime

logger = logging.getLogger(__name__)


class NotificationService:
    """通知服务"""

    def __init__(self, config):
        """初始化通知服务配置。"""
        self.config = config
        self.wechat_config = config.get('notification', {}).get('wechat', {})
        self.email_config = config.get('notification', {}).get('email', {})

    def send_wechat(self, content, msg_type='markdown'):
        """
        发送企业微信通知

        Args:
            content: str, 消息内容
            msg_type: str, 消息类型 ('text', 'markdown')

        Returns:
            bool: 是否发送成功
        """
        # 企业微信是首选通知出口，未启用时直接短路。
        if not self.wechat_config.get('enabled', False):
            logger.info("企业微信通知未启用")
            return False

        webhook_url = self.wechat_config.get('webhook_url')
        if not webhook_url or 'YOUR_KEY' in webhook_url:
            logger.warning("企业微信 webhook 未配置")
            return False

        try:
            import requests

            # 同一份内容按不同消息类型封装，避免上层关心 webhook payload 细节。
            if msg_type == 'markdown':
                data = {
                    "msgtype": "markdown",
                    "markdown": {
                        "content": content
                    }
                }
            else:
                data = {
                    "msgtype": "text",
                    "text": {
                        "content": content
                    }
                }

            response = requests.post(webhook_url, json=data, timeout=10)
            result = response.json()

            if result.get('errcode') == 0:
                logger.info("企业微信通知发送成功")
                return True
            else:
                logger.error(f"企业微信通知发送失败: {result}")
                return False

        except Exception as e:
            logger.error(f"发送企业微信通知异常: {e}")
            return False

    def send_email(self, subject, content, content_type='html'):
        """
        发送邮件通知

        Args:
            subject: str, 邮件主题
            content: str, 邮件内容
            content_type: str, 内容类型 ('plain', 'html')

        Returns:
            bool: 是否发送成功
        """
        # 邮件是补充通知渠道，通常用于保存更完整的格式化报告。
        if not self.email_config.get('enabled', False):
            logger.info("邮件通知未启用")
            return False

        try:
            msg = MIMEMultipart()
            msg['From'] = self.email_config['sender']
            msg['To'] = self.email_config['receiver']
            msg['Subject'] = subject

            msg.attach(MIMEText(content, content_type, 'utf-8'))

            smtp = smtplib.SMTP_SSL(
                self.email_config['smtp_server'],
                self.email_config['smtp_port']
            )
            smtp.login(
                self.email_config['sender'],
                self.email_config['password']
            )
            smtp.send_message(msg)
            smtp.quit()

            logger.info("邮件通知发送成功")
            return True

        except Exception as e:
            logger.error(f"发送邮件通知异常: {e}")
            return False

    def format_signal_report(self, scan_result):
        """
        格式化信号报告

        Args:
            scan_result: dict, 扫描结果

        Returns:
            str: 格式化的报告内容
        """
        # 先整理成统一 payload，再做文案渲染，减少对 scan_result 内部结构的直接耦合。
        payload = self.build_daily_payload(scan_result)
        market_status_map = {
            'bull': '🐂 牛市',
            'range': '📊 震荡',
            'sideways': '📊 震荡',
            'bear': '🐻 熊市'
        }

        market_status = market_status_map.get(
            payload['market_status'],
            payload['market_status']
        )

        content = f"""# 📈 量化交易信号报告

**扫描时间**: {payload['scan_time']}
**市场状态**: {market_status}

---

## 📊 扫描统计

- 总股票数: {payload['stats']['total_stocks']}
- 基本面通过: {payload['stats']['fundamental_passed']}
- 成交量通过: {payload['stats']['volume_passed']}
- 技术分析: {payload['stats']['technical_analyzed']}
- 候选池: {payload['stats'].get('candidate_pool_count', 0)}
- **买入信号**: {payload['stats']['buy_signals_count']}
- **卖出信号**: {payload['stats']['sell_signals_count']}
- **做T信号**: {payload['stats'].get('t_signals_count', 0)}
- **风险提示**: {payload['stats'].get('risk_alerts_count', 0)}

---
"""

        # 候选池和信号都只展示前几项，避免通知正文过长。
        if payload['candidate_pool']:
            content += "\n## 📋 候选池\n\n"
            for i, candidate in enumerate(payload['candidate_pool'], 1):
                content += f"""### {i}. {candidate['name']} ({candidate['ts_code']})

- **选股评分**: {candidate.get('score', 0)}
- **通过项**: {', '.join(candidate.get('passed_checks', []))}
- **过滤说明**: {candidate.get('summary', '三条腿条件通过')}

"""
        else:
            content += "\n## 📋 候选池\n\n暂无候选池\n\n"

        if payload['buy_signals']:
            content += "\n## 🟢 买入信号\n\n"
            for i, signal in enumerate(payload['buy_signals'][:5], 1):
                content += f"""### {i}. {signal['name']} ({signal['ts_code']})

- **类型**: {signal.get('signal_type', 'BUY')} | **动作**: {signal.get('action', '买入')}
- **价格**: ¥{signal['price']:.2f}
- **评分**: {signal['score']} 分
- **原因**: {signal['reason']}
- **仓位变化建议**: {signal.get('suggested_position_change', 0):+.0%}
- **信号**: {', '.join(signal['signals'])}
- **解释**: {signal.get('explanation', signal['reason'])}
- **ROE**: {signal['roe']:.2f}% | **PE**: {signal['pe']:.2f} | **市值**: {signal['market_cap']:.0f}亿

"""
        else:
            content += "\n## 🟢 买入信号\n\n暂无买入信号\n\n"

        if payload['sell_signals']:
            content += "\n## 🔴 卖出信号\n\n"
            for i, signal in enumerate(payload['sell_signals'], 1):
                profit_emoji = '📈' if signal['profit_pct'] > 0 else '📉'
                content += f"""### {i}. {signal['name']} ({signal['ts_code']})

- **类型**: {signal.get('signal_type', 'SELL')} | **动作**: {signal.get('action', '卖出')}
- **买入价**: ¥{signal['buy_price']:.2f}
- **当前价**: ¥{signal['current_price']:.2f}
- **盈亏**: {profit_emoji} {signal['profit_pct']*100:.2f}%
- **仓位变化建议**: {signal.get('suggested_position_change', 0):+.0%}
- **原因**: {', '.join(signal['reasons'])}
- **解释**: {signal.get('explanation', ', '.join(signal['reasons']))}

"""
        else:
            content += "\n## 🔴 卖出信号\n\n暂无卖出信号\n\n"

        # 高优先级信号把买卖、做T、风险提示统一排序后再截断展示。
        if payload['high_priority_trade_signals']:
            content += "\n## 🚨 高优先级交易信号\n\n"
            for i, signal in enumerate(payload['high_priority_trade_signals'], 1):
                content += f"""### {i}. {signal['name']} ({signal['ts_code']})

- **类型**: {signal.get('signal_type', '')} | **动作**: {signal.get('action', '')}
- **评分**: {signal.get('score', 0)}
- **仓位建议**: {signal.get('suggested_position_change', 0):+.0%}
- **解释**: {signal.get('explanation', signal.get('reason', ''))}

"""

        if scan_result.get('portfolio_risk'):
            portfolio_risk = scan_result['portfolio_risk']
            content += f"""
## 🛡️ 组合风险

- **新增仓位**: {portfolio_risk.get('action', 'UNKNOWN')}
- **风险标记**: {', '.join(portfolio_risk.get('risk_flags', [])) or '无'}
- **说明**: {', '.join(portfolio_risk.get('reasons', [])) or '当前组合可正常运行'}

"""

        content += "\n---\n\n> 🤖 由量化交易信号系统自动生成"

        return content

    def build_daily_payload(self, scan_result):
        """构建统一通知消息结构。"""
        # 通知层只保留稳定字段，避免直接透传整份扫描结果导致格式漂移。
        candidate_pool = []
        for item in scan_result.get('candidate_pool', [])[:5]:
            candidate_pool.append({
                'ts_code': item.get('ts_code'),
                'name': item.get('name'),
                'score': item.get('score', 0),
                'passed_checks': item.get('passed_checks', []),
                'summary': ' / '.join(item.get('passed_checks', [])) or '候选池入选',
            })

        trade_signals = scan_result.get('trade_signals', [])
        # 这里用 score 做一次粗筛，只把最值得先处理的信号放到顶部摘要里。
        high_priority_trade_signals = [
            signal for signal in trade_signals
            if signal.get('score', 0) >= 85
        ][:5]

        return {
            'scan_time': scan_result['scan_time'],
            'market_status': scan_result['market_status'],
            'stats': scan_result['stats'],
            'candidate_pool': candidate_pool,
            'buy_signals': scan_result.get('buy_signals', []),
            'sell_signals': scan_result.get('sell_signals', []),
            'trade_signals': trade_signals,
            'high_priority_trade_signals': high_priority_trade_signals,
        }

    def send_daily_report(self, scan_result):
        """
        发送每日报告

        Args:
            scan_result: dict, 扫描结果

        Returns:
            bool: 是否发送成功
        """
        if scan_result is None:
            logger.error("扫描结果为空，无法发送报告")
            return False

        # 报告只渲染一次，再分发到不同渠道，避免两套文案长期漂移。
        content = self.format_signal_report(scan_result)

        wechat_success = self.send_wechat(content, msg_type='markdown')

        email_success = False
        if self.email_config.get('enabled', False):
            subject = f"量化交易信号 - {scan_result['scan_time']}"
            html_content = content.replace('\n', '<br>').replace('# ', '<h1>').replace('## ', '<h2>')
            email_success = self.send_email(subject, html_content, content_type='html')

        return wechat_success or email_success
