import {
  ArrowDown,
  ArrowRight,
  BellRing,
  BrainCircuit,
  CheckCircle2,
  Database,
  FileText,
  Filter,
  MonitorPlay,
  ShieldAlert,
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
      "对全市场（all_a）或自选池（watchlist）做日线 MACD 金叉筛选。金叉统一定义为 DIF 上穿 DEA，再按 0轴上方、0轴附近、0轴下方分档；全市场首次扫描会分批连续执行，直到完成一轮。",
  },
  {
    icon: MonitorPlay,
    title: "第 3 步：指标股票池（候选股）",
    entry: "「股票池」页指标股票池卡片",
    description:
      "筛选命中的股票进入指标股票池，严格按 0轴上方 > 0轴附近 > 0轴下方排序，同档再比较策略分；同时展示温和放量、突破 MA5/MA10、红柱放大等确认条件。候选保留 5 个交易日，最多 100 只。",
  },
  {
    icon: BrainCircuit,
    title: "第 4 步：缠论多周期买卖点分析",
    entry: "「结果」页手动触发 / 盘中监控循环",
    description:
      "交易时段内监控循环会对「自选池 + 指标股票池」分批轮询，做 1m/5m/15m/30m/60m/120m/1d 多周期缠论分析。每个新确认的一买、二买、三买及对应卖点都会独立生成结构事件；评分过阈值时在同一事件上标记为强共振。各周期优先取对应周期行情，直接取数失败才用足量 1 分钟数据降级重采样；历史不足会保留实际数量和告警。",
  },
  {
    icon: BellRing,
    title: "第 5 步：信号通知推送",
    entry: "「推送配置」页",
    description:
      "缠论结构信号、MACD 金叉候选和 AI 解读可分别开关。新事件先进入 outbox，再投递到 Bark / 企业微信 / 邮件 / Webhook；独立派送心跳会持续重试，第 5 次失败后标记为 failed 并保留错误。",
  },
  {
    icon: FileText,
    title: "第 6 步：结果查看与 AI 解读",
    entry: "「结果」页",
    description:
      "每次分析/扫描生成 JSON 报告并保存。手动任务可直接 AI 解读；自动盘中/日线任务只在产生新事件时调用模型。解读先作为持久子任务保存，再写入笔记并按配置推送摘要；报告先结构化压缩，各股票和周期的上下文不会被持仓补充文本破坏。",
  },
];

const CONFIG_PAGES = [
  { page: "策略配置", path: "/strategies", purpose: "缠论参数、MACD 参数、扫描范围（自选/全市场）、监控周期" },
  { page: "推送配置", path: "/notifications", purpose: "Bark、企业微信、邮件、Webhook 开关与密钥" },
  { page: "模型配置", path: "/models", purpose: "LLM 接口（地址/模型/Key/代理），用于 AI 解读与图片识别" },
  { page: "定时任务", path: "/schedule", purpose: "每日扫描时间、盘中监控间隔/固定时点、是否仅交易日" },
  { page: "股票池", path: "/pool", purpose: "自选股导入、指标股票池（候选）查看与手动筛选" },
  { page: "我的持仓", path: "/holdings", purpose: "手动维护持仓（代码/名称/份额/持仓价/总金额），分析时携带" },
  { page: "操作日志", path: "/logs", purpose: "任务执行、AI 解读等操作记录，报错原因可直接查看" },
];

const DATA_STORES = [
  { location: "web/data/app.db", content: "Web 配置、自选股票池、模型/推送/定时设置、AI 解读笔记", persist: "必须持久化" },
  { location: "signal_system/state/signal_monitor.db", content: "指标股票池候选、信号事件、通知队列（outbox）", persist: "必须持久化" },
  { location: "signal_system/output/*.json", content: "分析/扫描结果报告", persist: "必须持久化" },
  { location: "signal_system/cache、logs", content: "行情缓存、运行日志", persist: "可清空，自动重建" },
];

const ZERO_AXIS_LEVELS = [
  {
    title: "0轴上方金叉",
    level: "优先级最高",
    description: "两线不在附近容差带内，且 DIF、DEA 都大于 0。通常属于多头趋势回调后的再次启动。",
    className:
      "text-emerald-700 bg-emerald-50 border-emerald-200 dark:text-emerald-300 dark:bg-emerald-500/10 dark:border-emerald-500/30",
  },
  {
    title: "0轴附近金叉",
    level: "中性转多",
    description: "两线都靠近 0 轴，或交叉当根跨越/触及 0 轴。属于反转初期，需要后续站稳 0 轴确认。",
    className:
      "text-amber-700 bg-amber-50 border-amber-200 dark:text-amber-300 dark:bg-amber-500/10 dark:border-amber-500/30",
  },
  {
    title: "0轴下方金叉",
    level: "风险最高",
    description: "两线不在附近容差带内，且 DIF、DEA 都小于 0。通常只是空头趋势中的弱势反弹。",
    className:
      "text-red-700 bg-red-50 border-red-200 dark:text-red-300 dark:bg-red-500/10 dark:border-red-500/30",
  },
];

const GOLDEN_CROSS_CONFIRMATIONS = [
  "成交量温和放大：当日量比位于策略页配置的上下限之间。",
  "突破短期压力：前一根收盘价未站上 MA5/MA10，当前收盘价同时站上两条均线。",
  "红柱同步放大：最近三根 MACD 柱全部为正且连续变长。",
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

      <Card>
        <CardHeader>
          <CardTitle>MACD 零轴金叉判定</CardTitle>
          <CardDescription>
            基础条件是 DIF 从下向上穿越 DEA；位置使用交叉确认当根判断，附近容差按 DIF/DEA 相对收盘价的距离计算。
          </CardDescription>
        </CardHeader>
        <CardContent className="flex flex-col gap-4">
          <div className="grid gap-3 md:grid-cols-3">
            {ZERO_AXIS_LEVELS.map((item) => (
              <div key={item.title} className={`rounded-md border p-3 ${item.className}`}>
                <div className="flex items-center justify-between gap-2">
                  <span className="text-sm font-medium">{item.title}</span>
                  <span className="text-xs">{item.level}</span>
                </div>
                <p className="mt-2 text-xs leading-5">{item.description}</p>
              </div>
            ))}
          </div>
          <div className="border-t pt-4">
            <div className="mb-2 text-sm font-medium">配套确认条件</div>
            <div className="grid gap-2 md:grid-cols-3">
              {GOLDEN_CROSS_CONFIRMATIONS.map((item) => (
                <div key={item} className="flex items-start gap-2 text-xs leading-5 text-muted-foreground">
                  <CheckCircle2 className="mt-0.5 size-4 shrink-0 text-emerald-600" />
                  <span>{item}</span>
                </div>
              ))}
            </div>
          </div>
          <div className="flex items-start gap-2 border-t pt-4 text-xs text-muted-foreground">
            <ShieldAlert className="mt-0.5 size-4 shrink-0 text-amber-600 dark:text-amber-400" />
            <span>排序规则固定为 0轴上方 &gt; 0轴附近 &gt; 0轴下方；只有最新一根确实发生金叉时才评定质量，未发生金叉时质量为 none。确认条件用于同档排序和提高筛选质量，不代表保证盈利或胜率。</span>
          </div>
          <div className="border-t pt-4 text-xs leading-5 text-muted-foreground">
            真实回测会同时输出缠论+零轴策略与原有策略的已闭合交易胜率、收益、回撤和样本外参数结果；单一标的或短区间没有统计意义，任何胜率差异都不能直接外推为未来收益。
          </div>
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
