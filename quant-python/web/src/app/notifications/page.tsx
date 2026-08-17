"use client";

import { useCallback, useEffect, useState } from "react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import { BellRing, Save, Send } from "lucide-react";

interface NotificationForm {
  wechat_enabled: boolean;
  wechat_webhook_url: string;
  webhook_enabled: boolean;
  webhook_url: string;
  webhook_auth: string;
  email_enabled: boolean;
  smtp_server: string;
  smtp_port: string;
  email_sender: string;
  email_password: string;
  email_receiver: string;
  timeout_seconds: string;
  bark_enabled: boolean;
  bark_url: string;
  bark_device_key: string;
  push_trade_signal: boolean;
  push_candidate_pool: boolean;
  push_ai_analysis: boolean;
}

const DEFAULT_FORM: NotificationForm = {
  wechat_enabled: false,
  wechat_webhook_url: "",
  webhook_enabled: false,
  webhook_url: "",
  webhook_auth: "",
  email_enabled: false,
  smtp_server: "",
  smtp_port: "465",
  email_sender: "",
  email_password: "",
  email_receiver: "",
  timeout_seconds: "10",
  bark_enabled: false,
  bark_url: "https://api.day.app/push",
  bark_device_key: "",
  push_trade_signal: true,
  push_candidate_pool: true,
  push_ai_analysis: true,
};

export default function NotificationsPage() {
  const [form, setForm] = useState<NotificationForm>(DEFAULT_FORM);
  const [saving, setSaving] = useState(false);
  const [testing, setTesting] = useState(false);

  const load = useCallback(async () => {
    try {
      const response = await fetch("/api/config/notification");
      if (!response.ok) throw new Error("加载推送配置失败");
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      const data = (await response.json()) as { config: Record<string, any> };
      const n = data.config.notification ?? {};
      setForm({
        wechat_enabled: Boolean(n.wechat?.enabled),
        wechat_webhook_url: String(n.wechat?.webhook_url ?? ""),
        webhook_enabled: Boolean(n.webhook?.enabled),
        webhook_url: String(n.webhook?.url ?? ""),
        webhook_auth: String(n.webhook?.headers?.Authorization ?? ""),
        email_enabled: Boolean(n.email?.enabled),
        smtp_server: String(n.email?.smtp_server ?? ""),
        smtp_port: String(n.email?.smtp_port ?? "465"),
        email_sender: String(n.email?.sender ?? ""),
        email_password: String(n.email?.password ?? ""),
        email_receiver: String(n.email?.receiver ?? ""),
        timeout_seconds: String(n.timeout_seconds ?? "10"),
        bark_enabled: Boolean(n.bark?.enabled),
        bark_url: String(n.bark?.url ?? "https://api.day.app/push"),
        bark_device_key: String(n.bark?.device_key ?? ""),
        push_trade_signal: n.push_trade_signal !== false,
        push_candidate_pool: n.push_candidate_pool !== false,
        push_ai_analysis: n.push_ai_analysis !== false,
      });
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "加载推送配置失败");
    }
  }, []);

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect
    void load();
  }, [load]);

  const set = (key: keyof NotificationForm, value: string | boolean) =>
    setForm((prev) => ({ ...prev, [key]: value }));

  const save = async () => {
    setSaving(true);
    const values = {
      "notification.wechat.enabled": form.wechat_enabled,
      "notification.wechat.webhook_url": form.wechat_webhook_url || "****",
      "notification.webhook.enabled": form.webhook_enabled,
      "notification.webhook.url": form.webhook_url || "****",
      "notification.webhook.headers.Authorization": form.webhook_auth || "****",
      "notification.email.enabled": form.email_enabled,
      "notification.email.smtp_server": form.smtp_server,
      "notification.email.smtp_port": Number(form.smtp_port),
      "notification.email.sender": form.email_sender || "****",
      "notification.email.password": form.email_password || "****",
      "notification.email.receiver": form.email_receiver || "****",
      "notification.timeout_seconds": Number(form.timeout_seconds),
      "notification.bark.enabled": form.bark_enabled,
      "notification.bark.url": form.bark_url || "https://api.day.app/push",
      "notification.bark.device_key": form.bark_device_key || "****",
      "notification.push_trade_signal": form.push_trade_signal,
      "notification.push_candidate_pool": form.push_candidate_pool,
      "notification.push_ai_analysis": form.push_ai_analysis,
    };
    try {
      const response = await fetch("/api/config/notification", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(values),
      });
      const data = (await response.json().catch(() => ({}))) as { error?: string; errors?: string[] };
      if (!response.ok) throw new Error(data.error || (data.errors ?? []).join("；") || "保存失败");
      toast.success("推送配置已保存");
      void load();
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "保存失败");
    } finally {
      setSaving(false);
    }
  };

  const test = async () => {
    setTesting(true);
    try {
      const response = await fetch("/api/run", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ kind: "test-notify", notify: true }),
      });
      const data = (await response.json().catch(() => ({}))) as { error?: string; jobId?: number };
      if (!response.ok) throw new Error(data.error || "启动测试通知失败");
      toast.success(`测试通知任务已启动 #${data.jobId}`);
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "启动测试通知失败");
    } finally {
      setTesting(false);
    }
  };

  const channelCard = (
    title: string,
    description: string,
    enabled: boolean,
    onEnabled: (value: boolean) => void,
    fields: React.ReactNode
  ) => (
    <Card>
      <CardHeader>
        <div className="flex items-center justify-between">
          <CardTitle className="text-base">{title}</CardTitle>
          <Switch checked={enabled} onCheckedChange={onEnabled} />
        </div>
        <CardDescription>{description}</CardDescription>
      </CardHeader>
      {enabled && <CardContent className="flex flex-col gap-3">{fields}</CardContent>}
    </Card>
  );

  return (
    <div className="flex max-w-3xl flex-col gap-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-xl font-semibold">推送配置</h1>
          <p className="text-sm text-muted-foreground">
            密钥可填在页面，也可通过环境变量提供（环境变量优先）。密钥回显一律脱敏。
          </p>
        </div>
        <div className="flex gap-2">
          <Button variant="outline" onClick={() => void test()} disabled={testing}>
            <Send className="size-4" /> {testing ? "启动中..." : "发送测试通知"}
          </Button>
          <Button onClick={() => void save()} disabled={saving}>
            <Save className="size-4" /> {saving ? "保存中..." : "保存配置"}
          </Button>
        </div>
      </div>

      {channelCard(
        "企业微信",
        "通过群机器人 Webhook 推送 Markdown 信号卡片。",
        form.wechat_enabled,
        (value) => set("wechat_enabled", value),
        <>
          <div className="flex flex-col gap-1.5">
            <Label>Webhook URL</Label>
            <Input
              value={form.wechat_webhook_url}
              onChange={(event) => set("wechat_webhook_url", event.target.value)}
              placeholder="https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=..."
            />
          </div>
        </>
      )}

      {channelCard(
        "通用 Webhook",
        "POST JSON 到自定义地址，支持自定义请求头。",
        form.webhook_enabled,
        (value) => set("webhook_enabled", value),
        <>
          <div className="flex flex-col gap-1.5">
            <Label>URL</Label>
            <Input value={form.webhook_url} onChange={(event) => set("webhook_url", event.target.value)} placeholder="https://example.com/hook" />
          </div>
          <div className="flex flex-col gap-1.5">
            <Label>Authorization（可选）</Label>
            <Input value={form.webhook_auth} onChange={(event) => set("webhook_auth", event.target.value)} placeholder="Bearer xxx" />
          </div>
        </>
      )}

      {channelCard(
        "邮件",
        "通过 SMTP 发送信号邮件（SSL）。",
        form.email_enabled,
        (value) => set("email_enabled", value),
        <>
          <div className="grid grid-cols-2 gap-3">
            <div className="flex flex-col gap-1.5">
              <Label>SMTP 服务器</Label>
              <Input value={form.smtp_server} onChange={(event) => set("smtp_server", event.target.value)} placeholder="smtp.qq.com" />
            </div>
            <div className="flex flex-col gap-1.5">
              <Label>端口</Label>
              <Input type="number" value={form.smtp_port} onChange={(event) => set("smtp_port", event.target.value)} />
            </div>
            <div className="flex flex-col gap-1.5">
              <Label>发件人</Label>
              <Input value={form.email_sender} onChange={(event) => set("email_sender", event.target.value)} placeholder="you@qq.com" />
            </div>
            <div className="flex flex-col gap-1.5">
              <Label>SMTP 密码/授权码</Label>
              <Input type="password" value={form.email_password} onChange={(event) => set("email_password", event.target.value)} placeholder="****" />
            </div>
            <div className="flex flex-col gap-1.5">
              <Label>收件人</Label>
              <Input value={form.email_receiver} onChange={(event) => set("email_receiver", event.target.value)} placeholder="you@qq.com" />
            </div>
          </div>
        </>
      )}

      {channelCard(
        "Bark（iOS）",
        "推送到 iPhone 的 Bark App（官方服务器 api.day.app）。",
        form.bark_enabled,
        (value) => set("bark_enabled", value),
        <>
          <div className="flex flex-col gap-1.5">
            <Label>服务器 URL</Label>
            <Input value={form.bark_url} onChange={(event) => set("bark_url", event.target.value)} placeholder="https://api.day.app/push" />
          </div>
          <div className="flex flex-col gap-1.5">
            <Label>Device Key</Label>
            <Input value={form.bark_device_key} onChange={(event) => set("bark_device_key", event.target.value)} placeholder="Bark App 首页的 device key" />
          </div>
        </>
      )}

      <Card>
        <CardHeader>
          <CardTitle className="text-base">通用</CardTitle>
          <CardDescription>控制推送内容和请求超时。</CardDescription>
        </CardHeader>
        <CardContent className="flex flex-col gap-4">
          <div className="flex items-center justify-between border-b pb-3">
            <div><Label>缠论交易信号</Label><p className="text-xs text-muted-foreground">推送一、二、三类买卖点及强共振标记。</p></div>
            <Switch checked={form.push_trade_signal} onCheckedChange={(value) => set("push_trade_signal", value)} />
          </div>
          <div className="flex items-center justify-between border-b pb-3">
            <div><Label>MACD 金叉候选</Label><p className="text-xs text-muted-foreground">推送日线金叉位置、确认条件和风险。</p></div>
            <Switch checked={form.push_candidate_pool} onCheckedChange={(value) => set("push_candidate_pool", value)} />
          </div>
          <div className="flex items-center justify-between border-b pb-3">
            <div><Label>AI 自动解读</Label><p className="text-xs text-muted-foreground">保存解读后再推送摘要；完整内容保留在解读页。</p></div>
            <Switch checked={form.push_ai_analysis} onCheckedChange={(value) => set("push_ai_analysis", value)} />
          </div>
          <div className="flex flex-col gap-1.5">
            <Label>请求超时（秒）</Label>
            <Input
              type="number"
              className="w-32"
              value={form.timeout_seconds}
              onChange={(event) => set("timeout_seconds", event.target.value)}
            />
          </div>
        </CardContent>
      </Card>

      <div className="flex items-center gap-2 text-sm text-muted-foreground">
        <BellRing className="size-4" />
        环境变量（可选）：WECHAT_WEBHOOK_URL、SIGNAL_WEBHOOK_URL、SIGNAL_WEBHOOK_AUTH、SIGNAL_EMAIL_SENDER、SIGNAL_EMAIL_PASSWORD、SIGNAL_EMAIL_RECEIVER、SIGNAL_BARK_DEVICE_KEY
      </div>
    </div>
  );
}
