import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { FiveTabAnalysis } from "../five-tab-analysis";
import type { AnalysisDocumentView, StrategyResult } from "@/lib/analysis-types";

function document(sell: boolean): AnalysisDocumentView {
  const strategy = (id: StrategyResult["strategy_id"], name: string): StrategyResult => ({
    strategy_id: id,
    name,
    version: "test",
    status: "ok",
    as_of: "2026-10-20",
    reference_price: 10,
    buy: false,
    sell,
    buy_conditions: [],
    sell_conditions: [],
    exit_rule: "divergence_trend",
    warnings: [],
    parameters: {},
  });
  return {
    schema_version: 2,
    symbol: "600036.SH",
    name: "招商银行",
    as_of: "2026-10-20",
    snapshot_hash: "h",
    revision: 1,
    strategies: [
      strategy("macd_zero_axis", "日线零轴金叉"),
      strategy("yearline_pullback", "年线趋势"),
      strategy("macd_divergence", "零轴＋底背离"),
    ],
    technical: { status: "success", content: "" },
    synthesis: { status: "success", content: "" },
  };
}

function render(held: boolean | null) {
  return renderToStaticMarkup(createElement(FiveTabAnalysis, { document: document(true), held }));
}

describe("五页签退出提示", () => {
  it("未持仓时提示退出条件仅作建仓后参考，不写预警", () => {
    const html = render(false);
    expect(html).toContain("未持仓，仅作建仓后参考");
    expect(html).not.toContain("预警");
  });

  it("有持仓时才使用预警口径", () => {
    expect(render(true)).toContain("预警");
  });

  it("阶段为 null 时降级为等待生成而不是崩溃", () => {
    const broken = {
      ...document(false),
      technical: null,
      synthesis: null,
    } as unknown as AnalysisDocumentView;
    const html = renderToStaticMarkup(
      createElement(FiveTabAnalysis, { document: broken })
    );
    expect(html).toContain("等待生成");
    expect(html).toContain("分析结果生成后将在此处实时展示");
  });

  it("展示只读的市场环境与策略适配提示", () => {
    const withGuidance = document(false);
    withGuidance.regime_guidance = {
      version: "2026-10-26",
      regime: "bear",
      regime_label: "熊市/下行",
      primary_strategy: "macd_divergence",
      primary_name: "零轴＋底背离",
      secondary_strategy: "macd_zero_axis",
      secondary_name: "日线零轴金叉",
      confidence: "low",
      summary: "深熊优先底背离，浅熊可看零轴。",
      caveats: ["没有正 Sharpe CI。"],
    };
    const html = renderToStaticMarkup(createElement(FiveTabAnalysis, { document: withGuidance, held: false }));
    expect(html).toContain("市场环境：熊市/下行");
    expect(html).toContain("零轴＋底背离");
    expect(html).toContain("日线零轴金叉");
  });
});
