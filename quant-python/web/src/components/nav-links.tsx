"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import {
  Bell,
  BrainCircuit,
  Clock3,
  LayoutDashboard,
  ScrollText,
  SlidersHorizontal,
  Wallet,
  Workflow,
  ChartNoAxesCombined,
  Settings,
} from "lucide-react";
import { cn } from "@/lib/utils";

const NAV = [
  { href: "/results", label: "个股分析", icon: LayoutDashboard },
  { href: "/assets", label: "股票与持仓", icon: Wallet },
  { href: "/backtest", label: "策略回测", icon: ChartNoAxesCombined },
];
const SETTINGS = [
  { slug: "strategies", label: "策略配置", icon: SlidersHorizontal },
  { slug: "notifications", label: "推送配置", icon: Bell },
  { slug: "models", label: "模型配置", icon: BrainCircuit },
  { slug: "schedule", label: "定时任务", icon: Clock3 },
  { slug: "logs", label: "操作日志", icon: ScrollText },
];

export function NavLinks() {
  const pathname = usePathname();
  return (
    <nav className="flex flex-col gap-1">
      {NAV.map((item) => {
        const Icon = item.icon;
        const active = pathname.startsWith(item.href) || (item.href === "/assets" && ["/pool", "/holdings"].includes(pathname));
        return (
          <Link
            key={item.href}
            href={item.href}
            className={cn(
              "flex items-center gap-2 rounded-lg px-3 py-2 text-sm text-muted-foreground transition-colors hover:bg-accent hover:text-accent-foreground",
              active && "bg-accent font-medium text-accent-foreground"
            )}
          >
            <Icon className="size-4" />
            {item.label}
          </Link>
        );
      })}
      <details open className="mt-1">
        <summary className="flex cursor-pointer items-center gap-2 rounded-lg px-3 py-2 text-sm text-muted-foreground hover:bg-accent"><Settings className="size-4" />系统配置</summary>
        <div className="ml-4 flex flex-col border-l pl-2">{SETTINGS.map(item => { const Icon = item.icon; const active = pathname === `/${item.slug}` || pathname === `/settings/${item.slug}`; return <Link key={item.slug} href={`/settings/${item.slug}`} className={cn("flex items-center gap-2 rounded px-3 py-2 text-sm text-muted-foreground hover:bg-accent", active && "bg-accent font-medium text-accent-foreground")}><Icon className="size-3.5" />{item.label}</Link>; })}</div>
      </details>
      <Link href="/workflow" className="mt-4 flex items-center gap-2 border-t px-3 py-3 text-xs text-muted-foreground"><Workflow className="size-3.5" />使用帮助</Link>
    </nav>
  );
}
