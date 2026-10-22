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
    entry: "「个股分析」页最近任务",
    description:
      "每次分析/扫描生成 JSON 报告并保存。任务完成后自动调用模型生成 AI 解读，直接在最近任务中点「查看 AI 分析」阅读；未生成的也可手动触发。解读先作为持久子任务保存，再写入笔记并按配置推送摘要；报告先结构化压缩，各股票和周期的上下文不会被持仓补充文本破坏。",
  },
];

/** 三个买入策略的入场与退出口径；退出数值来自 2024H1 + 2024H2 双样本对照。 */
const ENTRY_STRATEGIES = [
  {
    id: "macd_zero_axis",
    name: "日线零轴金叉",
    recommended: true,
    summary: "先卖一半，剩下的一半保住本",
    entry: "DIF 上穿 DEA 的金叉，按 0轴上方 > 0轴附近 > 0轴下方排序，并要求温和放量、突破 MA5/MA10、红柱放大等确认条件。",
    stop: "默认继承全局止损（8%）。半年对照里把止损放到 10% 更优，但那属于回测口径，不是已生效的建议值。",
    take: "推荐「先卖一半 @+15%，剩余仓位把止损上移到成本价」。同类对照中两段区间分别 +13.12% / +13.10%（一次性全清为 +2.57% / −8.78%），最大回撤也从 16.3% / 21.4% 降到 11.6% / 12.5%。",
    otherExits: "持仓超时 40 根日线；剩余仓位继续按止损与超时离场。",
    steps: [
      { when: "股价涨到成本价 +15%", action: "卖出最初股数的 50%", rest: "剩下 50%" },
      { when: "首档成交之后", action: "剩余仓位的止损上移到成本价（跌破买入价就走）", rest: "剩下 50% 继续持有到止损 / 超时" },
    ],
    example: "10 元买入 1000 股（成本 10000 元）：涨到 11.5 元（+15%）卖出 500 股，落袋约 5750 元；剩下 500 股的止损从 9.2 元上移到 10 元。之后继续涨就拿着，一旦跌破 10 元就卖掉这 500 股、只赚点手续费的差价，不会倒亏。",
    why: "先锁定一半利润，再用「保本止损」挡住「涨过又跌回去」——这是它比 30% 一次全清多赚、回撤还更浅的原因。",
  },
  {
    id: "macd_divergence",
    name: "零轴＋底背离",
    recommended: true,
    summary: "涨一档卖一档，分三次卖完",
    entry: "零轴附近或上方金叉，叠加日线 MACD 底背离（负柱周期完成、价格创新低但动能面积收缩）、放量、收盘在年线上方且年线上行。开启推送时按信号日发纸上跟踪提醒，不自动下单。",
    stop: "默认继承全局止损（8%）；该策略另外启用 v1b 退出规则集（顶背离 / 跌破年线 / 超时）。",
    take: "推荐「阶梯减仓 1/3 @+15%、1/3 @+30%、1/3 @+45%」。两段区间分别 +0.18% / +16.54%（一次性全清为 −4.20% / +3.66%），回撤也从 12.3% / 12.7% 降到 9.7% / 8.1%。",
    otherExits: "已确认的日线顶背离、跌破年线 MA250、持仓超时 40 根日线。",
    steps: [
      { when: "股价涨到成本价 +15%", action: "卖出最初股数的 1/3", rest: "剩下 2/3" },
      { when: "股价涨到成本价 +30%", action: "再卖出 1/3", rest: "剩下 1/3" },
      { when: "股价涨到成本价 +45%", action: "卖出最后 1/3，清仓", rest: "0" },
    ],
    example: "10 元买入 900 股：涨到 11.5 元卖 300 股 → 涨到 13 元再卖 300 股 → 涨到 14.5 元卖完最后 300 股。如果只涨到 12 元就掉头，只有第一档成交（卖掉 1/3），剩下 600 股继续按止损、顶背离、跌破年线、超时离场。",
    why: "底背离是抄底信号，后面能涨多少不确定性大，所以分三档「见好就收」，涨得越多卖得越多。",
  },
  {
    id: "yearline_pullback",
    name: "年线趋势",
    recommended: false,
    summary: "不分批，涨 30% 一次全清",
    entry: "MA250 向上 + 均线多头排列 + 回踩年线区间守稳 + 当日收阳（六条件固定，见「结果」页说明）。",
    stop: "默认继承全局止损（8%）。该策略持仓期长、单笔盈亏比高（5.12），止损偏紧会频繁被震出。",
    take: "保持一次性全清（30%）：8 种分批方案在两段区间全部跑输一次性全清（两段最好分别是 −2.25% 与 +7.24%，同期全清为 +2.37% 与 +14.79%）。它的利润本来就来自 30% 止盈那几笔，提前减半会削掉这部分。",
    otherExits: "持仓超时 40 根日线。",
    steps: [
      { when: "股价涨到成本价 +30%", action: "一次性全部卖出", rest: "0" },
    ],
    example: "10 元买入 1000 股：涨到 13 元（+30%）→ 1000 股一次全卖，这笔结束。中途不设分批卖出。",
    why: "它赚的是大趋势的钱——胜率只有 16%，全靠少数几笔涨到 30% 以上的行情拉动。如果涨 15% 就卖掉一半，等于把大行情的肉提前割掉，所以它保持原样。",
  },
] as const;

/** 读懂上面三张表需要先统一的四个说法。 */
const REDUCTION_GLOSSARY = [
  { term: "档位涨幅", meaning: "相对「买入成本价」。+15% 就是成本价 × 1.15（10 元买的股票涨到 11.5 元）。" },
  { term: "每档卖出比例", meaning: "相对「最初买入股数」，不是相对剩余仓位。三档各 1/3 就是把最初那笔分三次卖完。" },
  { term: "尾仓 / 保本止损", meaning: "首档卖出后，把剩余仓位的止损从「成本价 −8%」上移到「成本价」：之后跌破买入价就保本走人。" },
  { term: "默认状态", meaning: "三个策略默认都是「固定止盈 30% 一次性全清」；分批减仓需要手动开启，可全局设、也可按策略覆盖。" },
];

const CONFIG_PAGES = [
  { page: "策略配置", path: "/settings/strategies", purpose: "缠论参数、MACD 与底背离参数、止损止盈、扫描范围（自选/全市场）、监控周期" },
  { page: "推送配置", path: "/settings/notifications", purpose: "Bark、企业微信、邮件、Webhook 开关与密钥" },
  { page: "模型配置", path: "/settings/models", purpose: "LLM 接口（地址/模型/Key/代理），用于 AI 解读与图片识别" },
  { page: "定时任务", path: "/settings/schedule", purpose: "每日扫描时间、盘中监控间隔/固定时点、是否仅交易日" },
  { page: "股票与持仓", path: "/assets?tab=pool", purpose: "股票池：自选股导入、指标股票池（候选）查看与手动筛选" },
  { page: "股票与持仓", path: "/assets?tab=holdings", purpose: "我的持仓：手动维护持仓（代码/名称/份额/持仓价/总金额），分析时携带" },
  { page: "操作日志", path: "/settings/logs", purpose: "任务执行、AI 解读等操作记录，报错原因可直接查看" },
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
            <ShieldAlert className="size-4" /> 三个买入策略与各自的止盈止损
          </CardTitle>
          <CardDescription>
            在「设置 → 策略配置」里修改。三个策略共用「买入信号 → 次日开盘买入」的框架，但 <strong>退出规则各自独立</strong>：回测结论是「没有全局最优减仓方式，必须按策略分开」。
          </CardDescription>
        </CardHeader>
        <CardContent className="flex flex-col gap-4">
          <div className="rounded-lg border bg-muted/30 p-3">
            <div className="text-xs font-medium">先统一四个说法</div>
            <div className="mt-2 flex flex-col gap-1">
              {REDUCTION_GLOSSARY.map((item) => (
                <div key={item.term} className="text-xs leading-5 text-muted-foreground">
                  <span className="font-medium text-foreground">{item.term}：</span>{item.meaning}
                </div>
              ))}
            </div>
          </div>

          {ENTRY_STRATEGIES.map((strategy) => (
            <div key={strategy.id} className="rounded-lg border p-4">
              <div className="flex flex-wrap items-center gap-2">
                <span className="text-sm font-medium">{strategy.name}</span>
                <Badge variant="outline" className="font-mono text-[10px]">{strategy.id}</Badge>
                {strategy.recommended && <Badge variant="secondary">回测更优</Badge>}
              </div>
              <div className="mt-1.5 text-sm text-foreground">{strategy.summary}</div>
              <div className="mt-2 text-xs leading-5 text-muted-foreground">
                <span className="font-medium text-foreground">买入条件：</span>{strategy.entry}
              </div>
              <div className="mt-1 text-xs leading-5 text-muted-foreground">
                <span className="font-medium text-foreground">止损：</span>{strategy.stop}
              </div>
              <div className="mt-1 text-xs leading-5 text-muted-foreground">
                <span className="font-medium text-foreground">止盈 / 减仓：</span>{strategy.take}
              </div>
              <div className="mt-1 text-xs leading-5 text-muted-foreground">
                <span className="font-medium text-foreground">其它离场：</span>{strategy.otherExits}
              </div>

              <div className="mt-3 overflow-hidden rounded-lg border">
                <table className="w-full text-xs">
                  <thead className="bg-muted/50 text-muted-foreground">
                    <tr>
                      <th className="px-2 py-1.5 text-left font-medium">触发条件</th>
                      <th className="px-2 py-1.5 text-left font-medium">卖出动作</th>
                      <th className="px-2 py-1.5 text-left font-medium">卖出后剩余</th>
                    </tr>
                  </thead>
                  <tbody>
                    {strategy.steps.map((step) => (
                      <tr key={step.when} className="border-t">
                        <td className="px-2 py-1.5">{step.when}</td>
                        <td className="px-2 py-1.5">{step.action}</td>
                        <td className="px-2 py-1.5 text-muted-foreground">{step.rest}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>

              <div className="mt-2 text-xs leading-5 text-muted-foreground">
                <span className="font-medium text-foreground">举例：</span>{strategy.example}
              </div>
              <div className="mt-1 text-xs leading-5 text-muted-foreground">
                <span className="font-medium text-foreground">为什么这样：</span>{strategy.why}
              </div>
            </div>
          ))}

          <div className="overflow-hidden rounded-lg border">
            <table className="w-full text-xs">
              <thead className="bg-muted/50 text-muted-foreground">
                <tr>
                  <th className="px-2 py-1.5 text-left font-medium">策略</th>
                  <th className="px-2 py-1.5 text-left font-medium">减仓方式（一句话）</th>
                </tr>
              </thead>
              <tbody>
                {ENTRY_STRATEGIES.map((strategy) => (
                  <tr key={strategy.id} className="border-t">
                    <td className="px-2 py-1.5 font-medium">{strategy.name}</td>
                    <td className="px-2 py-1.5">{strategy.summary}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          <div className="rounded-lg border p-3 text-xs leading-5 text-muted-foreground">
            <span className="font-medium text-foreground">三种减仓方式：</span>
            <span className="ml-1">① 一次性全清（配置项 <code className="font-mono">risk.stop_profit_pct</code>，默认 30%，现状）</span>
            <span className="mx-1">·</span>
            <span>② 阶梯减仓（配置项 <code className="font-mono">risk.exit_plan</code>，按涨幅分档卖出原始仓位的固定比例）</span>
            <span className="mx-1">·</span>
            <span>③ 尾仓保护（首档成交后把剩余仓位止损上移到成本价，或按回撤比例移动止盈）。</span>
            <span className="ml-1">三者在「策略配置」的「分批止盈」区域设置；默认关闭，即全部按 ① 执行。</span>
          </div>
          <div className="flex items-start gap-2 border-t pt-4 text-xs leading-5 text-muted-foreground">
            <ShieldAlert className="mt-0.5 size-4 shrink-0 text-amber-600 dark:text-amber-400" />
            <span>
              下表「回测更优」来自 2024 上半年（选参数）+ 2024 下半年（样本外复核）两段各半年的对照，止损档位沿用上一轮回测结果，<strong>未覆盖 2018/2022 类持续熊市</strong>，也没有做多区间滚动。它是研究结论，不是自动生效的推荐；启用与否属于人工决策。
            </span>
          </div>
        </CardContent>
      </Card>

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
