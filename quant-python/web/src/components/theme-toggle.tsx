"use client"

import { useSyncExternalStore } from "react"
import { useTheme } from "next-themes"
import { Moon, Sun } from "lucide-react"

import { Button } from "@/components/ui/button"

export function ThemeToggle() {
  const { resolvedTheme, setTheme } = useTheme()
  const mounted = useSyncExternalStore(
    () => () => {},
    () => true,
    () => false
  )

  if (!mounted) {
    return <Button variant="ghost" size="icon" aria-label="切换主题" disabled />
  }

  const isDark = resolvedTheme === "dark"
  const label = isDark ? "切换到浅色模式" : "切换到深色模式"

  const toggleTheme = () => {
    const root = document.documentElement
    root.classList.add("theme-transition")
    setTheme(isDark ? "light" : "dark")
    window.setTimeout(() => root.classList.remove("theme-transition"), 400)
  }

  return (
    <Button
      variant="ghost"
      size="icon"
      aria-label={label}
      title={label}
      onClick={toggleTheme}
    >
      {isDark ? <Sun className="size-4" /> : <Moon className="size-4" />}
    </Button>
  )
}
