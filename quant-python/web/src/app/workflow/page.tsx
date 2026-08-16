import {
  ArrowDown,
  ArrowRight,
  BellRing,
  BrainCircuit,
  Database,
  FileText,
  Filter,
  MonitorPlay,
  Settings2,
  Workflow,
} from "lucide-react";
import { Badge } from "@/components/ui/badge";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";

const STEPS = [
  {
    icon: Database,
    title: "第 1 步：维护自选股票池",
    entry: "「股票池」页",
    description:
      "用文本或图片方式导入你关注的股票。导入/删除后自动同步为引擎的 watchlist，是后续所有分析的基础。",
  },
  {
    icon: Filter,
    title: "第 2 步：日线零轴金叉筛选",
    entry: "「股票池」页按钮 / 定时任务",
    description:
      "对全市场（all_a）或自选池（watchlist）做日线 MACD 零轴金叉筛选。全市场是分批执行的，首次需要多轮跑完整个市场；也可以等每日定时扫描自动执行。",
  },
  {
    icon: MonitorPlay,
    title: "第 3 步：指标股票池（候选股）",
    entry: "「股票池」页指标股票池卡片",
    description:
      "筛选命中的股票进入指标股票池，展示评分、零轴距离、确认时间。候选保留 5 个交易日，最多 100 只，过期自动清除。",
  },
  {
    icon: BrainCircuit,
    title: "第 4 步：缠论多周期买卖点分析",
    entry: "「结果」页手动触发 / 盘中监控循环",
    description:
      "交易时段内监控循环会定时对「自选池 + 指标股票池」做 1m/5m/15m/30m/60m/120m/1d 多周期缠论分析，识别一买、二买、三买等买卖点并打分。执行方式支持按间隔（如每 60 秒）或固定时点（10:30 / 13:30 / 14:30，可在定时任务页勾选）。",
  },
  {
    icon: BellRing,
    title: "第 5 步：信号通知推送",
    entry: "「推送配置」页",
    description:
      "新信号先进入通知队列（outbox），再投递到 Bark / 企业微信 / 邮件 / Webhook；投递失败会自动重试，不丢信号。",
  },
  {
    icon: FileText,
    title: "第 6 步：结果查看与 AI 解读",
    entry: "「结果」页",
    description:
      "每次分析/扫描生成 JSON 报告存在 output 目录，结果页按时间倒序展示；对单份报告可以点「AI 解读」，用配置的模型生成文字解读并保存为笔记。",
  },
];

const CONFIG_PAGES = [
  { page: "策略配置", path: "/strategies", purpose: "缠论参数、MACD 参数、扫描范围（自选/全市场）、监控周期" },
  { page: "推送配置", path: "/notifications", purpose: "Bark、企业微信、邮件、Webhook 开关与密钥" },
  { page: "模型配置", path: "/models", purpose: "LLM 接口（地址/模型/Key/代理），用于 AI 解读与图片识别" },
  { page: "定时任务", path: "/schedule", purpose: "每日扫描时间、盘中监控间隔/固定时点、是否仅交易日" },
  { page: "股票池", path: "/pool", purpose: "自选股导入、指标股票池（候选）查看与手动筛选" },
];

const DATA_STORES = [
  { location: "web/data/app.db", content: "Web 配置、自选股票池、模型/推送/定时设置、AI 解读笔记", persist: "必须持久化" },
  { location: "signal_system/data/signal_monitor.db", content: "指标股票池候选、信号事件、通知队列（outbox）", persist: "必须持久化" },
  { location: "signal_system/output/*.json", content: "分析/扫描结果报告", persist: "必须持久化" },
  { location: "signal_system/cache、logs", content: "行情缓存、运行日志", persist: "可清空，自动重建" },
];

export default function WorkflowPage() {
  return (
    <div className="flex max-w-4xl flex-col gap-6">
      <div>
        <h1 className="text-xl font-semibold">系统流程说明</h1>
        <p className="text-sm text-muted-foreground">
          完整链路：先把市场/自选数据用日线零轴金叉筛一遍形成指标股票池，再对筛选结果做缠论买卖点分析，最后把信号推送给你。
        </p>
      </div>

      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <Workflow className="size-4" /> 总览
          </CardTitle>
          <CardDescription>一图记住主流程：</CardDescription>
        </CardHeader>
        <CardContent>
          <div className="flex flex-wrap items-center gap-2 text-sm">
            <Badge variant="secondary">自选股票池</Badge>
            <Badge variant="outline" className="gap-0.5"><Database className="size-3" /> 导入维护</Badge>
            <ArrowRight className="size-4 text-muted-foreground" />
            <Badge variant="secondary">全市场行情</Badge>
            <ArrowRight className="size-4 text-muted-foreground" />
            <Badge>日线零轴金叉筛选</Badge>
            <ArrowRight className="size-4 text-muted-foreground" />
            <Badge variant="secondary">指标股票池</Badge>
            <ArrowRight className="size-4 text-muted-foreground" />
            <Badge>缠论买卖点分析</Badge>
            <ArrowRight className="size-4 text-muted-foreground" />
            <Badge variant="secondary">信号推送</Badge>
          </div>
          <p className="mt-3 text-xs text-muted-foreground">
            自选池和全市场都可以作为筛选输入；筛选出的指标股票池会与自选池一起进入缠论分析；推送支持 Bark/企业微信/邮件/Webhook。
          </p>
        </CardContent>
      </Card>

      <div className="flex flex-col gap-0">
        {STEPS.map((step, index) => {
          const Icon = step.icon;
          return (
            <div key={step.title} className="flex flex-col">
              <Card>
                <CardHeader className="flex flex-row items-start gap-3">
                  <div className="flex size-9 shrink-0 items-center justify-center rounded-lg bg-primary/10 text-primary">
                    <Icon className="size-4.5" />
                  </div>
                  <div>
                    <CardTitle className="text-base">{step.title}</CardTitle>
                    <CardDescription className="mt-1">{step.description}</CardDescription>
                    <div className="mt-2">
                      <Badge variant="outline">入口：{step.entry}</Badge>
                    </div>
                  </div>
                </CardHeader>
              </Card>
              {index < STEPS.length - 1 && (
                <div className="flex justify-center py-1 text-muted-foreground">
                  <ArrowDown className="size-4" />
                </div>
              )}
            </div>
          );
        })}
      </div>

      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <Settings2 className="size-4" /> 配置入口对照
          </CardTitle>
          <CardDescription>每类配置在哪个页面改：</CardDescription>
        </CardHeader>
        <CardContent className="flex flex-col gap-2">
          {CONFIG_PAGES.map((item) => (
            <div key={item.path} className="flex items-start justify-between gap-3 rounded-lg border p-3">
              <div>
                <div className="text-sm font-medium">{item.page}</div>
                <div className="text-xs text-muted-foreground">{item.purpose}</div>
              </div>
              <Badge variant="outline" className="shrink-0 font-mono">{item.path}</Badge>
            </div>
          ))}
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <Database className="size-4" /> 数据存在哪
          </CardTitle>
          <CardDescription>部署/备份时重点关注：</CardDescription>
        </CardHeader>
        <CardContent className="flex flex-col gap-2">
          {DATA_STORES.map((item) => (
            <div key={item.location} className="flex items-start justify-between gap-3 rounded-lg border p-3">
              <div>
                <div className="font-mono text-xs">{item.location}</div>
                <div className="text-xs text-muted-foreground">{item.content}</div>
              </div>
              <Badge variant={item.persist.startsWith("必须") ? "secondary" : "outline"} className="shrink-0">
                {item.persist}
              </Badge>
            </div>
          ))}
        </CardContent>
      </Card>
    </div>
  );
}
