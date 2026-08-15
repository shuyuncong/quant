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

      <Card>
        <CardHeader>
          <CardTitle className="text-base">通用</CardTitle>
          <CardDescription>请求超时（秒）</CardDescription>
        </CardHeader>
        <CardContent>
          <Input
            type="number"
            className="w-32"
            value={form.timeout_seconds}
            onChange={(event) => set("timeout_seconds", event.target.value)}
          />
        </CardContent>
      </Card>

      <div className="flex items-center gap-2 text-sm text-muted-foreground">
        <BellRing className="size-4" />
        环境变量（可选）：WECHAT_WEBHOOK_URL、SIGNAL_WEBHOOK_URL、SIGNAL_WEBHOOK_AUTH、SIGNAL_EMAIL_SENDER、SIGNAL_EMAIL_PASSWORD、SIGNAL_EMAIL_RECEIVER
      </div>
    </div>
  );
}
