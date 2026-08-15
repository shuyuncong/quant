"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import {
  Bell,
  BrainCircuit,
  Clock3,
  Database,
  LayoutDashboard,
  SlidersHorizontal,
} from "lucide-react";
import { cn } from "@/lib/utils";

const NAV = [
  { href: "/results", label: "结果", icon: LayoutDashboard },
  { href: "/strategies", label: "策略配置", icon: SlidersHorizontal },
  { href: "/notifications", label: "推送配置", icon: Bell },
  { href: "/models", label: "模型配置", icon: BrainCircuit },
  { href: "/schedule", label: "定时任务", icon: Clock3 },
  { href: "/pool", label: "股票池", icon: Database },
];

export function NavLinks() {
  const pathname = usePathname();
  return (
    <nav className="flex flex-col gap-1">
      {NAV.map((item) => {
        const Icon = item.icon;
        const active = pathname.startsWith(item.href);
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
    </nav>
  );
}
