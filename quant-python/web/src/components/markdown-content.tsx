"use client";

import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

/**
 * 渲染 AI 解读的 Markdown 内容。react-markdown 默认忽略原始 HTML，
 * 相比 dangerouslySetInnerHTML 更安全；remark-gfm 支持表格等扩展语法。
 */
export function MarkdownContent({ content }: { content: string }) {
  return (
    <div className="overflow-x-auto text-sm leading-relaxed">
      <ReactMarkdown remarkPlugins={[remarkGfm]}
        components={{
          h1: (props) => (
            <h1 className="mb-2 mt-4 border-b pb-1 text-lg font-semibold first:mt-0" {...props} />
          ),
          h2: (props) => (
            <h2 className="mb-2 mt-3 text-base font-semibold first:mt-0" {...props} />
          ),
          h3: (props) => (
            <h3 className="mb-1 mt-3 text-sm font-semibold first:mt-0" {...props} />
          ),
          h4: (props) => (
            <h4 className="mb-1 mt-2 text-sm font-semibold first:mt-0" {...props} />
          ),
          p: (props) => <p className="mb-2 last:mb-0" {...props} />,
          ul: (props) => (
            <ul className="mb-2 list-disc space-y-0.5 pl-5 last:mb-0" {...props} />
          ),
          ol: (props) => (
            <ol className="mb-2 list-decimal space-y-0.5 pl-5 last:mb-0" {...props} />
          ),
          li: (props) => <li className="leading-relaxed" {...props} />,
          strong: (props) => <strong className="font-semibold" {...props} />,
          em: (props) => <em className="italic" {...props} />,
          code: (props) => (
            <code className="rounded bg-muted px-1 py-0.5 font-mono text-xs" {...props} />
          ),
          pre: (props) => (
            <pre
              className="mb-2 overflow-x-auto rounded-lg bg-muted p-3 font-mono text-xs leading-relaxed last:mb-0"
              {...props}
            />
          ),
          table: (props) => (
            <table className="mb-2 w-full border-collapse text-xs last:mb-0" {...props} />
          ),
          th: (props) => (
            <th
              className="border border-border bg-muted/50 px-2 py-1 text-left font-semibold"
              {...props}
            />
          ),
          td: (props) => <td className="border border-border px-2 py-1" {...props} />,
          blockquote: (props) => (
            <blockquote
              className="mb-2 border-l-2 border-border pl-3 text-muted-foreground last:mb-0"
              {...props}
            />
          ),
          hr: (props) => <hr className="my-3 border-border" {...props} />,
          a: (props) => (
            <a
              className="text-primary underline underline-offset-2"
              target="_blank"
              rel="noopener noreferrer"
              {...props}
            />
          ),
        }}
      >
        {content}
      </ReactMarkdown>
    </div>
  );
}