"use client"

import { useEffect, useRef, useState } from "react"
import { createPortal } from "react-dom"
import { ChevronDown } from "lucide-react"

import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { cn } from "@/lib/utils"

interface HoldingOption {
  symbol: string
  name: string
}

interface SymbolComboboxProps {
  id?: string
  placeholder?: string
  value: string
  onChange: (value: string) => void
}

interface AnchorRect {
  top: number
  left: number
  width: number
}

function tokenize(value: string): string[] {
  return value
    .split(/[\s,，;；]+/)
    .map((part) => part.trim())
    .filter(Boolean)
}

export function SymbolCombobox({
  id,
  placeholder,
  value,
  onChange,
}: SymbolComboboxProps) {
  const [open, setOpen] = useState(false)
  const [holdings, setHoldings] = useState<HoldingOption[]>([])
  const [rect, setRect] = useState<AnchorRect | null>(null)
  const inputRef = useRef<HTMLInputElement>(null)
  const rootRef = useRef<HTMLDivElement>(null)
  const listRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    let cancelled = false
    fetch("/api/holdings")
      .then((response) => (response.ok ? response.json() : null))
      .then((data: { holdings?: HoldingOption[] } | null) => {
        if (!cancelled && data?.holdings) setHoldings(data.holdings)
      })
      .catch(() => {
        /* 持仓加载失败时下拉为空，不影响手动输入 */
      })
    return () => {
      cancelled = true
    }
  }, [])

  const updateRect = () => {
    const element = inputRef.current
    if (!element) return
    const bounds = element.getBoundingClientRect()
    setRect({ top: bounds.bottom + 4, left: bounds.left, width: bounds.width })
  }

  const toggleOpen = () => {
    if (!open) updateRect()
    setOpen((current) => !current)
  }

  useEffect(() => {
    if (!open) return
    updateRect()
    const reposition = () => updateRect()
    const closeOnOutside = (event: PointerEvent) => {
      const target = event.target as Node
      const insideRoot = rootRef.current?.contains(target) ?? false
      const insideList = listRef.current?.contains(target) ?? false
      if (!insideRoot && !insideList) setOpen(false)
    }
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape") setOpen(false)
    }
    window.addEventListener("resize", reposition)
    window.addEventListener("scroll", reposition, true)
    document.addEventListener("pointerdown", closeOnOutside)
    document.addEventListener("keydown", closeOnEscape)
    return () => {
      window.removeEventListener("resize", reposition)
      window.removeEventListener("scroll", reposition, true)
      document.removeEventListener("pointerdown", closeOnOutside)
      document.removeEventListener("keydown", closeOnEscape)
    }
  }, [open])

  const query = value.trim().toLowerCase()
  const tokens = tokenize(value)
  const filtered = query
    ? holdings.filter(
        (holding) =>
          holding.symbol.toLowerCase().includes(query) ||
          holding.name.toLowerCase().includes(query)
      )
    : holdings

  const addSymbol = (symbol: string) => {
    if (!tokens.includes(symbol)) {
      onChange([...tokens, symbol].join(" "))
    }
  }

  return (
    <div ref={rootRef} className="relative">
      <Input
        ref={inputRef}
        id={id}
        placeholder={placeholder}
        value={value}
        onChange={(event) => onChange(event.target.value)}
        className="pr-9"
        aria-autocomplete="list"
        aria-expanded={open}
        aria-controls="symbol-combobox-list"
      />
      <Button
        type="button"
        variant="ghost"
        size="icon"
        aria-label={open ? "收起持仓下拉" : "展开持仓下拉"}
        title={open ? "收起持仓下拉" : "展开持仓下拉"}
        aria-haspopup="listbox"
        aria-expanded={open}
        className="absolute top-0 right-0 h-full w-8 text-muted-foreground"
        onClick={toggleOpen}
      >
        <ChevronDown
          className={cn("size-4 transition-transform duration-150", open && "rotate-180")}
        />
      </Button>
      {open &&
        rect &&
        createPortal(
          <div
            ref={listRef}
            id="symbol-combobox-list"
            role="listbox"
            aria-label="当前持仓"
            style={{ top: rect.top, left: rect.left, width: rect.width }}
            className="fixed z-50 overflow-hidden rounded-lg border border-border bg-popover text-popover-foreground shadow-md"
          >
            <div className="max-h-64 overflow-y-auto p-1">
              {holdings.length === 0 ? (
                <div className="px-2 py-2 text-xs text-muted-foreground">
                  暂无持仓，可先到「我的持仓」添加，或直接手动输入代码
                </div>
              ) : filtered.length === 0 ? (
                <div className="px-2 py-2 text-xs text-muted-foreground">没有匹配的持仓</div>
              ) : (
                filtered.map((holding) => {
                  const selected = tokens.includes(holding.symbol)
                  return (
                    <button
                      key={holding.symbol}
                      type="button"
                      role="option"
                      aria-selected={selected}
                      className={cn(
                        "flex w-full cursor-pointer items-center justify-between gap-2 rounded-md px-2 py-1.5 text-left text-sm hover:bg-accent hover:text-accent-foreground",
                        selected && "bg-accent/50"
                      )}
                      onClick={() => addSymbol(holding.symbol)}
                    >
                      <span className="flex min-w-0 items-center gap-1.5">
                        {holding.name || holding.symbol}
                        {selected && (
                          <span className="text-xs text-muted-foreground">已添加</span>
                        )}
                      </span>
                      <span className="font-mono text-xs text-muted-foreground">
                        {holding.symbol}
                      </span>
                    </button>
                  )
                })
              )}
            </div>
          </div>,
          document.body
        )}
    </div>
  )
}
