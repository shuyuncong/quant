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
import { Badge } from "@/components/ui/badge";
import { Skeleton } from "@/components/ui/skeleton";
import {
  BellRing,
  CheckCircle2,
  Copy,
  Info,
  Mail,
  MessageSquare,
  Save,
  Send,
  Smartphone,
  Webhook,
} from "lucide-react";

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

const ENV_VARIABLES = [
  "WECHAT_WEBHOOK_URL",
  "SIGNAL_WEBHOOK_URL",
  "SIGNAL_WEBHOOK_AUTH",
  "SIGNAL_EMAIL_SENDER",
  "SIGNAL_EMAIL_PASSWORD",
  "SIGNAL_EMAIL_RECEIVER",
  "SIGNAL_BARK_DEVICE_KEY",
];

export default function NotificationsPage() {
  const [form, setForm] = useState<NotificationForm>(DEFAULT_FORM);
  const [loading, setLoading] = useState(true);
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
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
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
      const data = (await response.json().catch(() => ({}))) as {
        error?: string;
        errors?: string[];
      };
      if (!response.ok)
        throw new Error(data.error || (data.errors ?? []).join("；") || "保存失败");
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
      const data = (await response.json().catch(() => ({}))) as {
        error?: string;
        jobId?: number;
      };
      if (!response.ok) throw new Error(data.error || "启动测试通知失败");
      toast.success(`测试通知任务已启动 #${data.jobId}`);
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "启动测试通知失败");
    } finally {
      setTesting(false);
    }
  };

  const copyEnvVar = (name: string) => {
    navigator.clipboard.writeText(name);
    toast.success(`已复制环境变量名: ${name}`);
  };

  const enabledCount = [
    form.wechat_enabled,
    form.webhook_enabled,
    form.email_enabled,
    form.bark_enabled,
  ].filter(Boolean).length;

  if (loading) {
    return (
      <div className="flex max-w-4xl flex-col gap-6">
        <div className="flex items-center justify-between">
          <div className="space-y-2">
            <Skeleton className="h-7 w-36" />
            <Skeleton className="h-4 w-72" />
          </div>
          <div className="flex gap-2">
            <Skeleton className="h-9 w-28" />
            <Skeleton className="h-9 w-24" />
          </div>
        </div>
        <div className="grid gap-4 md:grid-cols-2">
          <Skeleton className="h-40 rounded-xl" />
          <Skeleton className="h-40 rounded-xl" />
          <Skeleton className="h-48 rounded-xl" />
          <Skeleton className="h-40 rounded-xl" />
        </div>
      </div>
    );
  }

  return (
    <div className="flex max-w-4xl flex-col gap-6">
      {/* 顶部标题与控制栏 */}
      <div className="flex flex-col gap-4 sm:flex-row sm:items-center sm:justify-between">
        <div>
          <div className="flex items-center gap-3">
            <h1 className="text-xl font-semibold tracking-tight">推送通知配置</h1>
            <Badge
              variant={enabledCount > 0 ? "default" : "secondary"}
              className="px-2.5 py-0.5 text-xs font-medium"
            >
              {enabledCount > 0 ? `${enabledCount} 个通道已启用` : "未启用推送通道"}
            </Badge>
          </div>
          <p className="mt-1 text-sm text-muted-foreground">
            设置交易信号、全市场候选池及 AI 解读的即时通知通道。密钥回显自动脱敏。
          </p>
        </div>
        <div className="flex shrink-0 items-center gap-2">
          <Button variant="outline" onClick={() => void test()} disabled={testing}>
            <Send className="mr-1.5 size-4" />
            {testing ? "测试中..." : "测试发送"}
          </Button>
          <Button onClick={() => void save()} disabled={saving}>
            <Save className="mr-1.5 size-4" />
            {saving ? "保存中..." : "保存配置"}
          </Button>
        </div>
      </div>

      {/* 渠道卡片网格 */}
      <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
        {/* 企业微信机器人 */}
        <Card
          className={`transition-colors ${
            form.wechat_enabled ? "border-primary/40 bg-card shadow-sm" : "bg-muted/10 opacity-90"
          }`}
        >
          <CardHeader className="pb-3">
            <div className="flex items-start justify-between gap-2">
              <div className="flex items-center gap-2.5">
                <div className="flex size-9 items-center justify-center rounded-lg bg-emerald-500/10 text-emerald-600 dark:bg-emerald-500/20 dark:text-emerald-400">
                  <MessageSquare className="size-5" />
                </div>
                <div>
                  <CardTitle className="text-base font-semibold">企业微信群机器人</CardTitle>
                  <CardDescription className="text-xs">
                    推送 Markdown 富文本卡片与交易指令
                  </CardDescription>
                </div>
              </div>
              <Switch
                checked={form.wechat_enabled}
                onCheckedChange={(val) => set("wechat_enabled", val)}
              />
            </div>
          </CardHeader>
          {form.wechat_enabled && (
            <CardContent className="space-y-3 pt-0 text-sm">
              <div className="flex flex-col gap-1.5">
                <Label className="text-xs text-muted-foreground">群机器人 Webhook URL</Label>
                <Input
                  className="font-mono text-xs"
                  value={form.wechat_webhook_url}
                  onChange={(e) => set("wechat_webhook_url", e.target.value)}
                  placeholder="https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=..."
                />
              </div>
              <p className="text-[11px] text-muted-foreground">
                支持 @所有人，在企微群设置中添加机器人即可获取 Webhook 地址。
              </p>
            </CardContent>
          )}
        </Card>

        {/* 通用 Webhook */}
        <Card
          className={`transition-colors ${
            form.webhook_enabled ? "border-primary/40 bg-card shadow-sm" : "bg-muted/10 opacity-90"
          }`}
        >
          <CardHeader className="pb-3">
            <div className="flex items-start justify-between gap-2">
              <div className="flex items-center gap-2.5">
                <div className="flex size-9 items-center justify-center rounded-lg bg-sky-500/10 text-sky-600 dark:bg-sky-500/20 dark:text-sky-400">
                  <Webhook className="size-5" />
                </div>
                <div>
                  <CardTitle className="text-base font-semibold">自定义 Webhook</CardTitle>
                  <CardDescription className="text-xs">
                    POST JSON 格式到自定义服务端点
                  </CardDescription>
                </div>
              </div>
              <Switch
                checked={form.webhook_enabled}
                onCheckedChange={(val) => set("webhook_enabled", val)}
              />
            </div>
          </CardHeader>
          {form.webhook_enabled && (
            <CardContent className="space-y-3 pt-0 text-sm">
              <div className="flex flex-col gap-1.5">
                <Label className="text-xs text-muted-foreground">目标 Endpoint URL</Label>
                <Input
                  className="font-mono text-xs"
                  value={form.webhook_url}
                  onChange={(e) => set("webhook_url", e.target.value)}
                  placeholder="https://api.yourdomain.com/trading/hook"
                />
              </div>
              <div className="flex flex-col gap-1.5">
                <Label className="text-xs text-muted-foreground">Authorization 请求头（可选）</Label>
                <Input
                  className="font-mono text-xs"
                  value={form.webhook_auth}
                  onChange={(e) => set("webhook_auth", e.target.value)}
                  placeholder="Bearer your-secret-token"
                />
              </div>
            </CardContent>
          )}
        </Card>

        {/* Bark (iOS) */}
        <Card
          className={`transition-colors ${
            form.bark_enabled ? "border-primary/40 bg-card shadow-sm" : "bg-muted/10 opacity-90"
          }`}
        >
          <CardHeader className="pb-3">
            <div className="flex items-start justify-between gap-2">
              <div className="flex items-center gap-2.5">
                <div className="flex size-9 items-center justify-center rounded-lg bg-amber-500/10 text-amber-600 dark:bg-amber-500/20 dark:text-amber-400">
                  <Smartphone className="size-5" />
                </div>
                <div>
                  <CardTitle className="text-base font-semibold">Bark (iOS 移动推送)</CardTitle>
                  <CardDescription className="text-xs">
                    即时推送至 iPhone / iPad 锁屏
                  </CardDescription>
                </div>
              </div>
              <Switch
                checked={form.bark_enabled}
                onCheckedChange={(val) => set("bark_enabled", val)}
              />
            </div>
          </CardHeader>
          {form.bark_enabled && (
            <CardContent className="space-y-3 pt-0 text-sm">
              <div className="flex flex-col gap-1.5">
                <Label className="text-xs text-muted-foreground">Bark 服务端地址</Label>
                <Input
                  className="font-mono text-xs"
                  value={form.bark_url}
                  onChange={(e) => set("bark_url", e.target.value)}
                  placeholder="https://api.day.app/push"
                />
              </div>
              <div className="flex flex-col gap-1.5">
                <Label className="text-xs text-muted-foreground">设备 Key (Device Key)</Label>
                <Input
                  className="font-mono text-xs"
                  value={form.bark_device_key}
                  onChange={(e) => set("bark_device_key", e.target.value)}
                  placeholder="填入 Bark App 首页展示的密钥"
                />
              </div>
            </CardContent>
          )}
        </Card>

        {/* 邮件 SMTP */}
        <Card
          className={`transition-colors ${
            form.email_enabled ? "border-primary/40 bg-card shadow-sm" : "bg-muted/10 opacity-90"
          }`}
        >
          <CardHeader className="pb-3">
            <div className="flex items-start justify-between gap-2">
              <div className="flex items-center gap-2.5">
                <div className="flex size-9 items-center justify-center rounded-lg bg-indigo-500/10 text-indigo-600 dark:bg-indigo-500/20 dark:text-indigo-400">
                  <Mail className="size-5" />
                </div>
                <div>
                  <CardTitle className="text-base font-semibold">邮件 (SMTP 推送)</CardTitle>
                  <CardDescription className="text-xs">
                    发送详尽交易日报与买卖提醒
                  </CardDescription>
                </div>
              </div>
              <Switch
                checked={form.email_enabled}
                onCheckedChange={(val) => set("email_enabled", val)}
              />
            </div>
          </CardHeader>
          {form.email_enabled && (
            <CardContent className="space-y-3 pt-0 text-sm">
              <div className="grid grid-cols-3 gap-2">
                <div className="col-span-2 flex flex-col gap-1">
                  <Label className="text-xs text-muted-foreground">SMTP 服务器</Label>
                  <Input
                    className="h-8 text-xs"
                    value={form.smtp_server}
                    onChange={(e) => set("smtp_server", e.target.value)}
                    placeholder="smtp.qq.com / smtp.163.com"
                  />
                </div>
                <div className="flex flex-col gap-1">
                  <Label className="text-xs text-muted-foreground">端口</Label>
                  <Input
                    className="h-8 text-xs font-mono"
                    type="number"
                    value={form.smtp_port}
                    onChange={(e) => set("smtp_port", e.target.value)}
                  />
                </div>
              </div>
              <div className="grid grid-cols-2 gap-2">
                <div className="flex flex-col gap-1">
                  <Label className="text-xs text-muted-foreground">发件人邮箱</Label>
                  <Input
                    className="h-8 text-xs"
                    value={form.email_sender}
                    onChange={(e) => set("email_sender", e.target.value)}
                    placeholder="sender@example.com"
                  />
                </div>
                <div className="flex flex-col gap-1">
                  <Label className="text-xs text-muted-foreground">SMTP 授权码</Label>
                  <Input
                    className="h-8 text-xs font-mono"
                    type="password"
                    value={form.email_password}
                    onChange={(e) => set("email_password", e.target.value)}
                    placeholder="****"
                  />
                </div>
              </div>
              <div className="flex flex-col gap-1">
                <Label className="text-xs text-muted-foreground">收件人邮箱</Label>
                <Input
                  className="h-8 text-xs"
                  value={form.email_receiver}
                  onChange={(e) => set("email_receiver", e.target.value)}
                  placeholder="receiver@example.com"
                />
              </div>
            </CardContent>
          )}
        </Card>
      </div>

      {/* 推送触发事件与全局配置 */}
      <Card>
        <CardHeader className="pb-3">
          <div className="flex items-center gap-2">
            <BellRing className="size-4 text-primary" />
            <CardTitle className="text-base font-semibold">通知策略与触发事件</CardTitle>
          </div>
          <CardDescription className="text-xs">
            精准控制各类策略分析结果是否向上述通道推送
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="divide-y rounded-lg border">
            <div className="flex items-center justify-between p-3.5 transition-colors hover:bg-muted/30">
              <div className="space-y-0.5">
                <div className="flex items-center gap-2">
                  <span className="text-sm font-medium">缠论交易信号</span>
                  <Badge variant="outline" className="text-[10px]">即时</Badge>
                </div>
                <p className="text-xs text-muted-foreground">
                  监测到一买、二买、三买、顶背驰离场及强共振信号时触发推送。
                </p>
              </div>
              <Switch
                checked={form.push_trade_signal}
                onCheckedChange={(val) => set("push_trade_signal", val)}
              />
            </div>

            <div className="flex items-center justify-between p-3.5 transition-colors hover:bg-muted/30">
              <div className="space-y-0.5">
                <div className="flex items-center gap-2">
                  <span className="text-sm font-medium">MACD 金叉候选池汇总</span>
                  <Badge variant="outline" className="text-[10px]">汇总</Badge>
                </div>
                <p className="text-xs text-muted-foreground">
                  盘后或每日定时扫描完成后发送一份候选标的汇总清单，避免逐股刷屏。
                </p>
              </div>
              <Switch
                checked={form.push_candidate_pool}
                onCheckedChange={(val) => set("push_candidate_pool", val)}
              />
            </div>

            <div className="flex items-center justify-between p-3.5 transition-colors hover:bg-muted/30">
              <div className="space-y-0.5">
                <div className="flex items-center gap-2">
                  <span className="text-sm font-medium">AI 自动解读完成摘要</span>
                  <Badge variant="outline" className="text-[10px]">智能</Badge>
                </div>
                <p className="text-xs text-muted-foreground">
                  多模型解读保存后自动提取核心操作建议并推送，完整分析留存报告面板。
                </p>
              </div>
              <Switch
                checked={form.push_ai_analysis}
                onCheckedChange={(val) => set("push_ai_analysis", val)}
              />
            </div>
          </div>

          <div className="flex items-center justify-between rounded-lg bg-muted/40 p-3">
            <div>
              <Label className="text-sm font-medium">请求超时时间</Label>
              <p className="text-xs text-muted-foreground">各推送通道 HTTP 请求最大等待时限</p>
            </div>
            <div className="flex items-center gap-2">
              <Input
                type="number"
                className="w-20 font-mono text-center text-sm"
                value={form.timeout_seconds}
                onChange={(e) => set("timeout_seconds", e.target.value)}
                min={1}
                max={60}
              />
              <span className="text-xs text-muted-foreground">秒</span>
            </div>
          </div>
        </CardContent>
      </Card>

      {/* 环境变量提示卡片 */}
      <div className="rounded-xl border border-muted bg-muted/20 p-4">
        <div className="flex items-start gap-2.5">
          <Info className="mt-0.5 size-4 text-muted-foreground shrink-0" />
          <div className="space-y-2 text-xs text-muted-foreground">
            <p className="font-medium text-foreground">
              支持环境变量优先覆盖（安全规范）
            </p>
            <p>
              如在生产部署中无需通过前端明文保存密码密钥，可直接在服务端环境变量中提供，系统将自动优先取用：
            </p>
            <div className="flex flex-wrap gap-1.5 pt-1">
              {ENV_VARIABLES.map((env) => (
                <button
                  key={env}
                  type="button"
                  onClick={() => copyEnvVar(env)}
                  className="inline-flex items-center gap-1 rounded bg-muted px-2 py-0.5 font-mono text-[11px] text-foreground transition-colors hover:bg-accent hover:text-accent-foreground"
                  title="点击复制"
                >
                  {env}
                  <Copy className="size-2.5 opacity-50" />
                </button>
              ))}
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
