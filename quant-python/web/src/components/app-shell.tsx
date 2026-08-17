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
              "flex h-8 items-center gap-2 rounded px-2 text-[13px] text-zinc-600 hover:bg-zinc-100 hover:text-zinc-900",
              active && "bg-[#eef4ff] font-medium text-[#2563eb] hover:bg-[#eef4ff]"
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
    <div className="flex h-screen flex-col overflow-hidden bg-[#f3f6fa] text-zinc-900 md:flex-row">
      <header className="flex h-12 shrink-0 items-center gap-3 border-b border-[#e8edf5] bg-white px-3 md:hidden">
        <button
          type="button"
          aria-label="打开导航"
          title="打开导航"
          className="flex size-8 items-center justify-center rounded hover:bg-zinc-100"
          onClick={() => setMobileNavOpen(true)}
        >
          <Menu className="size-4" />
        </button>
        <span className="min-w-0 truncate text-sm font-semibold">
          {currentPage?.label ?? "缠论信号监控"}
        </span>
      </header>

      <aside className="hidden w-52 shrink-0 flex-col border-r border-[#e8edf5] bg-white md:flex">
        <div className="flex h-12 items-center gap-2 border-b border-[#e8edf5] px-4">
          <span className="text-sm font-semibold tracking-wide">缠论信号监控</span>
        </div>
        {navigation()}
        <div className="border-t border-[#e8edf5] p-3 text-[12px] text-zinc-400">
          量化信号仅供研究，不构成投资建议
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
          <aside className="relative flex h-full w-64 max-w-[85vw] flex-col border-r border-[#e8edf5] bg-white shadow-xl">
            <div className="flex h-12 items-center justify-between border-b border-[#e8edf5] px-4">
              <span className="text-sm font-semibold tracking-wide">缠论信号监控</span>
              <button
                type="button"
                aria-label="关闭导航"
                title="关闭导航"
                className="flex size-8 items-center justify-center rounded hover:bg-zinc-100"
                onClick={() => setMobileNavOpen(false)}
              >
                <X className="size-4" />
              </button>
            </div>
            {navigation(() => setMobileNavOpen(false))}
            <div className="border-t border-[#e8edf5] p-3 text-[12px] text-zinc-400">
              量化信号仅供研究，不构成投资建议
            </div>
          </aside>
        </div>
      )}
    </div>
  );
}
