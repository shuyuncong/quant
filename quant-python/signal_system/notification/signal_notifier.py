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
        for name in ("wechat", "email", "webhook"):
            if self.config.get(name, {}).get("enabled", False):
                result.append(name)
        return result

    @staticmethod
    def _markdown(payload: dict[str, Any]) -> str:
        side = "🟢 买入观察" if payload["side"] == "buy" else "🔴 卖出观察"
        reasons = payload.get("evidence", {}).get("score_reasons", [])
        center = payload.get("evidence", {}).get("latest_center")
        center_text = "无已确认中枢"
        if center:
            center_text = f"ZD={center['zd']:.3f}, ZG={center['zg']:.3f}"
        return (
            f"# {side}\n\n"
            f"> **{payload.get('name', '')} ({payload['symbol']})**\n\n"
            f"- 周期：{payload['timeframe']}\n"
            f"- 信号：{payload['signal_type']}\n"
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

    def _send_email(self, payload: dict[str, Any]) -> tuple[bool, str]:
        config = self.config.get("email", {})
        required = ["smtp_server", "smtp_port", "sender", "password", "receiver"]
        missing = [key for key in required if not config.get(key)]
        if missing:
            return False, f"邮件配置缺少: {', '.join(missing)}"
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
        if channel == "email":
            return self._send_email(payload)
        return False, f"未知通知通道: {channel}"
