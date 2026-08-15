"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import {
  Activity,
  BarChart3,
  Bell,
  BrainCircuit,
  Clock3,
  Database,
} from "lucide-react";
import { cn } from "@/lib/utils";

const navItems = [
  { href: "/results", label: "结果", icon: BarChart3 },
  { href: "/strategies", label: "策略配置", icon: Activity },
  { href: "/notifications", label: "推送配置", icon: Bell },
  { href: "/models", label: "模型配置", icon: BrainCircuit },
  { href: "/schedule", label: "定时任务", icon: Clock3 },
  { href: "/pool", label: "股票池", icon: Database },
];

export function AppShell({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();
  return (
    <div className="flex h-screen overflow-hidden bg-[#f3f6fa] text-zinc-900">
      <aside className="flex w-52 shrink-0 flex-col border-r border-[#e8edf5] bg-white">
        <div className="flex h-12 items-center gap-2 border-b border-[#e8edf5] px-4">
          <span className="text-sm font-semibold tracking-wide">缠论信号监控</span>
        </div>
        <nav className="flex-1 space-y-1 overflow-y-auto p-2">
          {navItems.map((item) => {
            const Icon = item.icon;
            const active = pathname.startsWith(item.href);
            return (
              <Link
                key={item.href}
                href={item.href}
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
        <div className="border-t border-[#e8edf5] p-3 text-[12px] text-zinc-400">
          量化信号仅供研究，不构成投资建议
        </div>
      </aside>
      <main className="min-w-0 flex-1 overflow-y-auto p-4">{children}</main>
    </div>
  );
}
