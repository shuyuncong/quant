"use client";

import Link from "next/link";
import { useState } from "react";
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

const SETTINGS_GROUP = { href: "/settings", label: "系统配置" };

/** Exact match, or a `href + "/"` prefix so `/results-old` never selects `/results`. */
function matches(pathname: string, href: string): boolean {
  return pathname === href || pathname.startsWith(`${href}/`);
}

/** Page label for the mobile header; single source of truth is NAV/SETTINGS. */
export function getPageLabel(pathname: string): string {
  const settings = SETTINGS.find((item) => matches(pathname, `/settings/${item.slug}`));
  if (settings) return settings.label;
  if (matches(pathname, SETTINGS_GROUP.href)) return SETTINGS_GROUP.label;
  const item = NAV.find((entry) => matches(pathname, entry.href));
  return item?.label ?? (matches(pathname, "/workflow") ? "使用帮助" : "量化学习");
}

export function NavLinks({ onNavigate }: { onNavigate?: () => void }) {
  const pathname = usePathname();
  // null = no user click yet, so the group follows the route. The key remounts the group when the
  // top-level section changes, which keeps polling refreshes from resetting a manual choice.
  const [userToggle, setUserToggle] = useState<boolean | null>(null);
  const section = pathname.split("/")[1] ?? "";
  return (
    <nav className="flex flex-col gap-1">
      {NAV.map((item) => {
        const Icon = item.icon;
        return (
          <Link
            key={item.href}
            href={item.href}
            onClick={onNavigate}
            aria-current={matches(pathname, item.href) ? "page" : undefined}
            className={cn(
              "flex items-center gap-2 rounded-lg px-3 py-2 text-sm text-muted-foreground transition-colors hover:bg-accent hover:text-accent-foreground",
              matches(pathname, item.href) && "bg-accent font-medium text-accent-foreground"
            )}
          >
            <Icon className="size-4" />
            {item.label}
          </Link>
        );
      })}
      <details key={section} open={userToggle ?? matches(pathname, SETTINGS_GROUP.href)} onToggle={(event) => setUserToggle(event.currentTarget.open)} className="mt-1">
        <summary className="flex cursor-pointer items-center gap-2 rounded-lg px-3 py-2 text-sm text-muted-foreground hover:bg-accent"><Settings className="size-4" />系统配置</summary>
        <div className="ml-4 flex flex-col border-l pl-2">{SETTINGS.map(item => { const Icon = item.icon; const active = matches(pathname, `/settings/${item.slug}`); return <Link key={item.slug} href={`/settings/${item.slug}`} onClick={onNavigate} aria-current={active ? "page" : undefined} className={cn("flex items-center gap-2 rounded px-3 py-2 text-sm text-muted-foreground hover:bg-accent", active && "bg-accent font-medium text-accent-foreground")}><Icon className="size-3.5" />{item.label}</Link>; })}</div>
      </details>
      <Link href="/workflow" onClick={onNavigate} className="mt-4 flex items-center gap-2 border-t px-3 py-3 text-xs text-muted-foreground"><Workflow className="size-3.5" />使用帮助</Link>
    </nav>
  );
}
