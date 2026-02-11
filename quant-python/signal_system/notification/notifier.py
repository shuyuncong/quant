"""
通知推送模块
支持企业微信、邮件通知
"""

import requests
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import logging
from datetime import datetime

logger = logging.getLogger(__name__)


class NotificationService:
    """通知服务"""

    def __init__(self, config):
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
        if not self.wechat_config.get('enabled', False):
            logger.info("企业微信通知未启用")
            return False

        webhook_url = self.wechat_config.get('webhook_url')
        if not webhook_url or 'YOUR_KEY' in webhook_url:
            logger.warning("企业微信 webhook 未配置")
            return False

        try:
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
        market_status_map = {
            'bull': '🐂 牛市',
            'sideways': '📊 震荡',
            'bear': '🐻 熊市'
        }

        market_status = market_status_map.get(
            scan_result['market_status'],
            scan_result['market_status']
        )

        content = f"""# 📈 量化交易信号报告

**扫描时间**: {scan_result['scan_time']}
**市场状态**: {market_status}

---

## 📊 扫描统计

- 总股票数: {scan_result['stats']['total_stocks']}
- 基本面通过: {scan_result['stats']['fundamental_passed']}
- 成交量通过: {scan_result['stats']['volume_passed']}
- 技术分析: {scan_result['stats']['technical_analyzed']}
- **买入信号**: {scan_result['stats']['buy_signals_count']}
- **卖出信号**: {scan_result['stats']['sell_signals_count']}

---
"""

        if scan_result['buy_signals']:
            content += "\n## 🟢 买入信号\n\n"
            for i, signal in enumerate(scan_result['buy_signals'][:5], 1):
                content += f"""### {i}. {signal['name']} ({signal['ts_code']})

- **价格**: ¥{signal['price']:.2f}
- **评分**: {signal['score']} 分
- **原因**: {signal['reason']}
- **信号**: {', '.join(signal['signals'])}
- **ROE**: {signal['roe']:.2f}% | **PE**: {signal['pe']:.2f} | **市值**: {signal['market_cap']:.0f}亿

"""
        else:
            content += "\n## 🟢 买入信号\n\n暂无买入信号\n\n"

        if scan_result['sell_signals']:
            content += "\n## 🔴 卖出信号\n\n"
            for i, signal in enumerate(scan_result['sell_signals'], 1):
                profit_emoji = '📈' if signal['profit_pct'] > 0 else '📉'
                content += f"""### {i}. {signal['name']} ({signal['ts_code']})

- **买入价**: ¥{signal['buy_price']:.2f}
- **当前价**: ¥{signal['current_price']:.2f}
- **盈亏**: {profit_emoji} {signal['profit_pct']*100:.2f}%
- **原因**: {', '.join(signal['reasons'])}

"""
        else:
            content += "\n## 🔴 卖出信号\n\n暂无卖出信号\n\n"

        content += "\n---\n\n> 🤖 由量化交易信号系统自动生成"

        return content

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

        content = self.format_signal_report(scan_result)

        wechat_success = self.send_wechat(content, msg_type='markdown')

        email_success = False
        if self.email_config.get('enabled', False):
            subject = f"量化交易信号 - {scan_result['scan_time']}"
            html_content = content.replace('\n', '<br>').replace('# ', '<h1>').replace('## ', '<h2>')
            email_success = self.send_email(subject, html_content, content_type='html')

        return wechat_success or email_success
