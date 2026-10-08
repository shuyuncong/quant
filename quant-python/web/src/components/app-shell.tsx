"use client";

import { usePathname } from "next/navigation";
import { useEffect, useRef, useState } from "react";
import { Menu, X } from "lucide-react";
import { getPageLabel, NavLinks } from "@/components/nav-links";
import { ThemeToggle } from "@/components/theme-toggle";
import { Dialog, DialogClose, DialogContent, DialogDescription, DialogTitle, DialogTrigger } from "@/components/ui/dialog";

export function AppShell({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();
  const [mobileNavOpen, setMobileNavOpen] = useState(false);
  const closeNavigation = useRef<HTMLButtonElement>(null);
  const currentPageLabel = getPageLabel(pathname);

  useEffect(() => {
    const desktop = window.matchMedia("(min-width: 768px)");
    const closeOnDesktop = () => {
      if (desktop.matches) setMobileNavOpen(false);
    };
    desktop.addEventListener("change", closeOnDesktop);
    return () => desktop.removeEventListener("change", closeOnDesktop);
  }, []);

  return (
    <div className="flex h-screen flex-col overflow-hidden bg-background text-foreground md:flex-row">
      <Dialog open={mobileNavOpen} onOpenChange={setMobileNavOpen}>
        <header className="flex h-12 shrink-0 items-center justify-between gap-3 border-b border-sidebar-border bg-sidebar px-3 text-sidebar-foreground md:hidden">
          <div className="flex min-w-0 items-center gap-3">
            <DialogTrigger
              aria-label="打开导航"
              title="打开导航"
              className="flex size-8 shrink-0 items-center justify-center rounded hover:bg-sidebar-accent hover:text-sidebar-accent-foreground focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring"
            >
              <Menu className="size-4" aria-hidden="true" />
            </DialogTrigger>
            <span className="min-w-0 truncate text-sm font-semibold">
              {currentPageLabel}
            </span>
          </div>
          <ThemeToggle />
        </header>
        <DialogContent
          showCloseButton={false}
          initialFocus={closeNavigation}
          className="top-0 left-0 flex h-dvh w-64 max-w-[85vw] translate-x-0 translate-y-0 flex-col gap-0 rounded-none border-r border-sidebar-border bg-sidebar p-0 text-sidebar-foreground shadow-2xl md:hidden"
        >
          <div className="flex h-12 shrink-0 items-center justify-between border-b border-sidebar-border px-4">
            <DialogTitle className="text-sm font-semibold">主导航</DialogTitle>
            <DialogClose
              ref={closeNavigation}
              aria-label="关闭导航"
              title="关闭导航"
              className="flex size-8 items-center justify-center rounded hover:bg-sidebar-accent hover:text-sidebar-accent-foreground focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring"
            >
              <X className="size-4" aria-hidden="true" />
            </DialogClose>
          </div>
          <DialogDescription className="sr-only">选择页面，或按 Escape 关闭导航。</DialogDescription>
          <div className="min-h-0 flex-1 overflow-y-auto p-2">
            <NavLinks onNavigate={() => setMobileNavOpen(false)} />
          </div>
          <div className="shrink-0 border-t border-sidebar-border p-3 text-[12px] text-muted-foreground">
            量化信号仅供研究，不构成投资建议
          </div>
        </DialogContent>
      </Dialog>

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

    </div>
  );
}
