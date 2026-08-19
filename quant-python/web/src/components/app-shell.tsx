"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect, useState } from "react";
import {
  Activity,
  BarChart3,
  Bell,
  BrainCircuit,
  Clock3,
  Database,
  Menu,
  MessageSquareText,
  ScrollText,
  Wallet,
  Workflow,
  X,
} from "lucide-react";
import { cn } from "@/lib/utils";
import { ThemeToggle } from "@/components/theme-toggle";

const navItems = [
  { href: "/results", label: "结果", icon: BarChart3 },
  { href: "/interpretations", label: "AI 解读", icon: MessageSquareText },
  { href: "/strategies", label: "策略配置", icon: Activity },
  { href: "/notifications", label: "推送配置", icon: Bell },
  { href: "/models", label: "模型配置", icon: BrainCircuit },
  { href: "/schedule", label: "定时任务", icon: Clock3 },
  { href: "/pool", label: "股票池", icon: Database },
  { href: "/holdings", label: "我的持仓", icon: Wallet },
  { href: "/logs", label: "操作日志", icon: ScrollText },
  { href: "/workflow", label: "流程说明", icon: Workflow },
];

export function AppShell({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();
  const [mobileNavOpen, setMobileNavOpen] = useState(false);
  const currentPage = navItems.find((item) => pathname.startsWith(item.href));

  useEffect(() => {
    if (!mobileNavOpen) return;
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape") setMobileNavOpen(false);
    };
    window.addEventListener("keydown", closeOnEscape);
    return () => window.removeEventListener("keydown", closeOnEscape);
  }, [mobileNavOpen]);

  const navigation = (onNavigate?: () => void) => (
    <nav className="flex-1 space-y-1 overflow-y-auto p-2">
      {navItems.map((item) => {
        const Icon = item.icon;
        const active = pathname.startsWith(item.href);
        return (
          <Link
            key={item.href}
            href={item.href}
            onClick={onNavigate}
            className={cn(
              "flex h-8 items-center gap-2 rounded px-2 text-[13px] text-sidebar-foreground/70 transition-colors hover:bg-sidebar-accent hover:text-sidebar-accent-foreground",
              active && "bg-sidebar-accent font-medium text-sidebar-accent-foreground hover:bg-sidebar-accent"
            )}
          >
            <Icon className="h-4 w-4" />
            {item.label}
          </Link>
        );
      })}
    </nav>
  );

  return (
    <div className="flex h-screen flex-col overflow-hidden bg-background text-foreground md:flex-row">
      <header className="flex h-12 shrink-0 items-center justify-between gap-3 border-b border-sidebar-border bg-sidebar px-3 text-sidebar-foreground md:hidden">
        <div className="flex min-w-0 items-center gap-3">
        <button
          type="button"
          aria-label="打开导航"
          title="打开导航"
          className="flex size-8 shrink-0 items-center justify-center rounded hover:bg-sidebar-accent hover:text-sidebar-accent-foreground"
          onClick={() => setMobileNavOpen(true)}
        >
          <Menu className="size-4" />
        </button>
        <span className="min-w-0 truncate text-sm font-semibold">
          {currentPage?.label ?? "缠论信号监控"}
        </span>
        </div>
        <ThemeToggle />
      </header>

      <aside className="hidden w-52 shrink-0 flex-col border-r border-sidebar-border bg-sidebar text-sidebar-foreground md:flex">
        <div className="flex h-12 items-center gap-2 border-b border-sidebar-border px-4">
          <span className="text-sm font-semibold tracking-wide">缠论信号监控</span>
        </div>
        {navigation()}
        <div className="flex items-center justify-between gap-2 border-t border-sidebar-border p-2 pl-3">
          <span className="min-w-0 text-[12px] leading-tight text-muted-foreground">
            量化信号仅供研究，不构成投资建议
          </span>
          <ThemeToggle />
        </div>
      </aside>

      <main className="min-h-0 min-w-0 flex-1 overflow-y-auto p-3 sm:p-4">{children}</main>

      {mobileNavOpen && (
        <div className="fixed inset-0 z-50 md:hidden" role="dialog" aria-modal="true" aria-label="主导航">
          <button
            type="button"
            aria-label="关闭导航"
            className="absolute inset-0 bg-black/30"
            onClick={() => setMobileNavOpen(false)}
          />
          <aside className="relative flex h-full w-64 max-w-[85vw] flex-col border-r border-sidebar-border bg-sidebar text-sidebar-foreground shadow-xl">
            <div className="flex h-12 items-center justify-between border-b border-sidebar-border px-4">
              <span className="text-sm font-semibold tracking-wide">缠论信号监控</span>
              <button
                type="button"
                aria-label="关闭导航"
                title="关闭导航"
                className="flex size-8 items-center justify-center rounded hover:bg-sidebar-accent hover:text-sidebar-accent-foreground"
                onClick={() => setMobileNavOpen(false)}
              >
                <X className="size-4" />
              </button>
            </div>
            {navigation(() => setMobileNavOpen(false))}
            <div className="border-t border-sidebar-border p-3 text-[12px] text-muted-foreground">
              量化信号仅供研究，不构成投资建议
            </div>
          </aside>
        </div>
      )}
    </div>
  );
}
