"""Per-event notification channels for the signal outbox."""

from __future__ import annotations

from email.mime.text import MIMEText
import json
import logging
import smtplib
import ssl
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener


logger = logging.getLogger(__name__)


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


class SignalNotifier:
    def __init__(self, config: dict[str, Any]):
        self.config = config.get("notification", {})
        self.timeout = float(self.config.get("timeout_seconds", 10))

    def active_channels(self) -> list[str]:
        result = []
        for name in ("wechat", "email", "webhook", "bark"):
            if self.config.get(name, {}).get("enabled", False):
                result.append(name)
        return result

    @staticmethod
    def _markdown(payload: dict[str, Any]) -> str:
        evidence = payload.get("evidence", {})
        kind = evidence.get("notification_kind", "trade_signal")
        if kind == "ai_analysis":
            report_path = evidence.get("report_path") or "未记录"
            return (
                f"# {payload.get('name', 'AI自动解读')}\n\n"
                f"{evidence.get('content', '')}\n\n"
                f"> 报告：{report_path}\n"
                f"> {payload['risk_notice']}\n"
                f"> event_id: `{payload['event_id']}`"
            )
        if kind == "candidate":
            confirmations = evidence.get("confirmation_items", [])
            return (
                f"# MACD 金叉候选\n\n"
                f"> **{payload.get('name', '')} ({payload['symbol']})**\n\n"
                f"- 位置：{evidence.get('golden_cross_zone_label', '未识别')}\n"
                f"- 价格：{payload['price']:.3f}\n"
                f"- 评分：{payload['score']}\n"
                f"- DIF / DEA：{float(evidence.get('dif') or 0):.4f} / {float(evidence.get('dea') or 0):.4f}\n"
                f"- 量比：{float(evidence.get('volume_ratio') or 0):.2f}\n"
                f"- 确认条件：{'；'.join(confirmations) if confirmations else '暂无额外确认'}\n"
                f"- 风险：{evidence.get('risk_text', '需结合趋势复核')}\n"
                f"- 确认时间：{payload['confirmed_at']}\n\n"
                f"> {payload['risk_notice']}\n"
                f"> event_id: `{payload['event_id']}`"
            )
        side = "🟢 买入观察" if payload["side"] == "buy" else "🔴 卖出观察"
        level = "强共振" if evidence.get("strong_signal") else "缠论结构"
        reasons = evidence.get("score_reasons", [])
        center = evidence.get("latest_center")
        center_text = "无已确认中枢"
        if center:
            center_text = f"ZD={center['zd']:.3f}, ZG={center['zg']:.3f}"
        return (
            f"# {side}\n\n"
            f"> **{payload.get('name', '')} ({payload['symbol']})**\n\n"
            f"- 周期：{payload['timeframe']}\n"
            f"- 信号：{payload['signal_type']}\n"
            f"- 级别：{level}\n"
            f"- 价格：{payload['price']:.3f}\n"
            f"- 评分：{payload['score']}\n"
            f"- 确认时间：{payload['confirmed_at']}\n"
            f"- 中枢：{center_text}\n"
            f"- 依据：{'；'.join(reasons) if reasons else '见结构化 evidence'}\n\n"
            f"> {payload['risk_notice']}\n"
            f"> event_id: `{payload['event_id']}`"
        )

    def _post_json(
        self,
        url: str,
        payload: dict[str, Any],
        headers: dict[str, str] | None = None,
        response_validator: Callable[[bytes], tuple[bool, str]] | None = None,
    ) -> tuple[bool, str]:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request_headers = {"Content-Type": "application/json; charset=utf-8"}
        request_headers.update(headers or {})
        request = Request(url=url, data=body, headers=request_headers, method="POST")
        opener = build_opener(_NoRedirect())
        try:
            with opener.open(request, timeout=self.timeout) as response:
                response_body = response.read(1024 * 1024 + 1)
                if len(response_body) > 1024 * 1024:
                    return False, "响应体超过1MB"
                if 200 <= response.status < 300:
                    if response_validator:
                        return response_validator(response_body)
                    return True, "ok"
                return False, f"HTTP {response.status}"
        except HTTPError as exc:
            return False, f"HTTP {exc.code}"
        except (URLError, TimeoutError, OSError) as exc:
            return False, f"{type(exc).__name__}: {exc}"

    def _send_wechat(self, payload: dict[str, Any]) -> tuple[bool, str]:
        config = self.config.get("wechat", {})
        url = str(config.get("webhook_url", ""))
        if not url or "YOUR_KEY" in url:
            return False, "企业微信 webhook 未配置"
        body = {"msgtype": "markdown", "markdown": {"content": self._markdown(payload)}}

        def validate(response_body: bytes) -> tuple[bool, str]:
            try:
                result = json.loads(response_body.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                return False, f"企业微信响应无法解析: {exc}"
            if result.get("errcode") == 0:
                return True, "ok"
            return False, f"企业微信错误 {result.get('errcode')}: {result.get('errmsg', '')}"

        return self._post_json(url, body, response_validator=validate)

    def _send_webhook(self, payload: dict[str, Any]) -> tuple[bool, str]:
        config = self.config.get("webhook", {})
        url = str(config.get("url", ""))
        if not url:
            return False, "通用 webhook URL 未配置"
        headers = {str(key): str(value) for key, value in config.get("headers", {}).items()}
        headers.setdefault("Idempotency-Key", payload["event_id"])
        return self._post_json(url, payload, headers=headers)

    def _send_bark(self, payload: dict[str, Any]) -> tuple[bool, str]:
        config = self.config.get("bark", {})
        device_key = str(config.get("device_key", ""))
        if not device_key:
            return False, "Bark device_key 未配置"
        url = str(config.get("url", "") or "https://api.day.app/push")
        evidence = payload.get("evidence", {})
        kind = evidence.get("notification_kind", "trade_signal")
        if kind == "ai_analysis":
            title = payload.get("name", "AI自动解读")
            body = str(evidence.get("content", ""))[:3500]
        elif kind == "candidate":
            title = f"金叉候选 {payload.get('name', '')} {payload['symbol']}".strip()
            confirmations = evidence.get("confirmation_items", [])
            body = (
                f"位置: {evidence.get('golden_cross_zone_label', '未识别')}\n"
                f"价格: {payload['price']:.3f}\n"
                f"评分: {payload['score']}\n"
                f"确认: {('、'.join(confirmations) if confirmations else '暂无额外确认')}\n"
                f"风险: {evidence.get('risk_text', '需结合趋势复核')}"
            )
        else:
            side = "买入观察" if payload["side"] == "buy" else "卖出观察"
            title = f"{side} {payload.get('name', '')} {payload['symbol']} {payload['timeframe']}".strip()
            reasons = evidence.get("score_reasons", [])
            level = "强共振" if evidence.get("strong_signal") else "缠论结构"
            body = (
                f"信号: {payload['signal_type']}\n"
                f"级别: {level}\n"
                f"价格: {payload['price']:.3f}\n"
                f"评分: {payload['score']}\n"
                f"确认: {payload['confirmed_at']}\n"
                f"依据: {('、'.join(reasons) if reasons else '见结构化 evidence')}"
            )
        body_data = {
            "device_key": device_key,
            "title": title,
            "body": body,
            "group": "chan-signal",
            "level": "active",
        }

        def validate(response_body: bytes) -> tuple[bool, str]:
            try:
                result = json.loads(response_body.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                return False, f"Bark 响应无法解析: {exc}"
            if result.get("code") == 200:
                return True, "ok"
            return False, f"Bark 错误 code={result.get('code')}: {result.get('message', '')}"

        return self._post_json(url, body_data, response_validator=validate)

    def _send_email(self, payload: dict[str, Any]) -> tuple[bool, str]:
        config = self.config.get("email", {})
        required = ["smtp_server", "smtp_port", "sender", "password", "receiver"]
        missing = [key for key in required if not config.get(key)]
        if missing:
            return False, f"邮件配置缺少: {', '.join(missing)}"
        kind = payload.get("evidence", {}).get("notification_kind", "trade_signal")
        if kind == "ai_analysis":
            subject_side = "AI自动解读"
        elif kind == "candidate":
            subject_side = "MACD金叉候选"
        else:
            subject_side = "买入观察" if payload["side"] == "buy" else "卖出观察"
        message = MIMEText(self._markdown(payload), "plain", "utf-8")
        message["Subject"] = f"{subject_side} {payload.get('name', '')} {payload['timeframe']}"
        message["From"] = str(config["sender"])
        message["To"] = str(config["receiver"])
        try:
            context = ssl.create_default_context()
            with smtplib.SMTP_SSL(
                str(config["smtp_server"]),
                int(config["smtp_port"]),
                timeout=self.timeout,
                context=context,
            ) as smtp:
                smtp.login(str(config["sender"]), str(config["password"]))
                smtp.send_message(message)
            return True, "ok"
        except (smtplib.SMTPException, OSError) as exc:
            return False, f"{type(exc).__name__}: {exc}"

    def send(self, channel: str, payload: dict[str, Any]) -> tuple[bool, str]:
        if channel == "wechat":
            return self._send_wechat(payload)
        if channel == "webhook":
            return self._send_webhook(payload)
        if channel == "bark":
            return self._send_bark(payload)
        if channel == "email":
            return self._send_email(payload)
        return False, f"未知通知通道: {channel}"
