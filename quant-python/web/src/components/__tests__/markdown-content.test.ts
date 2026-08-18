import * as React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { MarkdownContent } from "../markdown-content";

describe("MarkdownContent", () => {
  const sample = `# 招商银行（600036.SH）解读

## 核心信号
- **买入信号**：MACD 零轴上方金叉
- 仓位建议：占总资金 **30%**

| 信号 | 强度 | 说明 |
| --- | --- | --- |
| MACD | 强 | DIF 上穿 DEA |

> 注意：仅作参考

\`\`\`
x = 1
\`\`\`
`;

  it("renders headings, lists, tables, quotes and code blocks", () => {
    const html = renderToStaticMarkup(
      React.createElement(MarkdownContent, { content: sample })
    );
    expect(html).toContain(">招商银行（600036.SH）解读</h1>");
    expect(html).toContain(">核心信号</h2>");
    expect(html).toContain("<li");
    expect(html).toContain("<strong");
    expect(html).toContain("<table");
    expect(html).toContain(">信号</th>");
    expect(html).toContain("<blockquote");
    expect(html).toContain("<pre");
    expect(html).toContain("<code");
  });

  it("escapes embedded html instead of executing it", () => {
    const html = renderToStaticMarkup(
      React.createElement(MarkdownContent, { content: "<script>alert(1)</script>" })
    );
    expect(html).not.toContain("<script>");
    expect(html).toContain("&lt;script&gt;");
  });

  it("renders empty content without crashing", () => {
    const html = renderToStaticMarkup(React.createElement(MarkdownContent, { content: "" }));
    expect(html).toBeTruthy();
  });
});