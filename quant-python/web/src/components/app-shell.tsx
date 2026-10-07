"use client";

import { usePathname } from "next/navigation";
import { useEffect, useState } from "react";
import { Menu, X } from "lucide-react";
import { getPageLabel, NavLinks } from "@/components/nav-links";
import { ThemeToggle } from "@/components/theme-toggle";

export function AppShell({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();
  const [mobileNavOpen, setMobileNavOpen] = useState(false);
  const currentPageLabel = getPageLabel(pathname);

  useEffect(() => {
    if (!mobileNavOpen) return;
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape") setMobileNavOpen(false);
    };
    window.addEventListener("keydown", closeOnEscape);
    return () => window.removeEventListener("keydown", closeOnEscape);
  }, [mobileNavOpen]);

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
          {currentPageLabel}
        </span>
        </div>
        <ThemeToggle />
      </header>

      <aside className="hidden w-52 shrink-0 flex-col border-r border-sidebar-border bg-sidebar text-sidebar-foreground md:flex">
        <div className="flex h-12 items-center gap-2 border-b border-sidebar-border px-4">
          <span className="text-sm font-semibold tracking-wide">量化学习</span>
        </div>
        <div className="flex-1 overflow-y-auto p-2">
          <NavLinks />
        </div>
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
            className="absolute inset-0 bg-black/30 backdrop-blur-sm"
            onClick={() => setMobileNavOpen(false)}
          />
          <aside className="surface-glass relative flex h-full w-64 max-w-[85vw] flex-col border-r border-sidebar-border text-sidebar-foreground shadow-2xl">
            <div className="flex h-12 items-center justify-between border-b border-sidebar-border px-4">
              <span className="text-sm font-semibold tracking-wide">量化学习</span>
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
            <div className="flex-1 overflow-y-auto p-2">
              <NavLinks onNavigate={() => setMobileNavOpen(false)} />
            </div>
            <div className="border-t border-sidebar-border p-3 text-[12px] text-muted-foreground">
              量化信号仅供研究，不构成投资建议
            </div>
          </aside>
        </div>
      )}
    </div>
  );
}
