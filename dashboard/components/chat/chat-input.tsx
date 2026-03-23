"use client"

import React from "react"

import { Send } from "lucide-react"
import { useState, useRef, useEffect } from "react"
import type { Mode } from "@/lib/types"
import { cn, classifyStatusTone, statusMessageClass } from "@/lib/utils"
import { getSettings, saveSettings } from "@/lib/settings"

const modes: { value: Mode; label: string }[] = [
  { value: "single", label: "Single" },
  { value: "critique", label: "Critique" },
  { value: "debate", label: "Debate" },
  { value: "consensus", label: "Consensus" },
  { value: "council", label: "Council" },
  { value: "retrieval", label: "Web" },
]

interface ChatInputProps {
  onSend: (
    message: string,
    mode: Mode,
    tools: boolean,
    factCheck: boolean,
    autoMemory: boolean
  ) => void
  autoMemory: boolean
  onAutoMemoryChange: (next: boolean) => void
  disabled?: boolean
  statusText?: string
}

export function ChatInput({
  onSend,
  autoMemory,
  onAutoMemoryChange,
  disabled,
  statusText,
}: ChatInputProps) {
  const [input, setInput] = useState("")
  const [mode, setMode] = useState<Mode>("single")
  const [tools, setTools] = useState(false)
  const [factCheck, setFactCheck] = useState(false)
  const textareaRef = useRef<HTMLTextAreaElement>(null)

  const updateMode = (next: Mode) => {
    setMode(next)
    saveSettings({ chatMode: next })
  }

  const updateTools = (next: boolean) => {
    setTools(next)
    saveSettings({ chatToolsEnabled: next })
  }

  const updateFactCheck = (next: boolean) => {
    setFactCheck(next)
    saveSettings({ chatFactCheckEnabled: next })
  }

  useEffect(() => {
    const update = () => {
      const cfg = getSettings()
      setMode(cfg.chatMode || "single")
      setTools(Boolean(cfg.chatToolsEnabled))
      setFactCheck(Boolean(cfg.chatFactCheckEnabled))
    }
    update()
    window.addEventListener("mmy-settings-changed", update)
    return () => window.removeEventListener("mmy-settings-changed", update)
  }, [])

  useEffect(() => {
    if (textareaRef.current) {
      textareaRef.current.style.height = "auto"
      textareaRef.current.style.height = `${Math.min(textareaRef.current.scrollHeight, 160)}px`
    }
  }, [input])

  useEffect(() => {
    if (disabled) return
    const id = window.requestAnimationFrame(() => {
      textareaRef.current?.focus()
    })
    return () => window.cancelAnimationFrame(id)
  }, [disabled])

  const handleSubmit = () => {
    const trimmed = input.trim()
    if (!trimmed || disabled) return
    onSend(trimmed, mode, tools, factCheck, autoMemory)
    setInput("")
  }

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault()
      handleSubmit()
    }
  }

  const statusTone = classifyStatusTone(statusText)

  return (
    <div className="border-t border-border bg-card/80 p-3 backdrop-blur-sm">
      {/* Status text */}
      {statusText && (
        <div className="mb-2 flex items-center gap-2 px-1">
          <span
            className={cn(
              "h-1.5 w-1.5 rounded-full",
              statusTone === "error"
                ? "bg-neon-red"
                : statusTone === "success"
                  ? "bg-neon-green"
                  : statusTone === "progress"
                    ? "animate-pulse-neon bg-neon-cyan"
                    : "bg-muted-foreground"
            )}
          />
          <span className={statusMessageClass(statusText, "font-mono text-xs")}>{statusText}</span>
        </div>
      )}

      {/* Controls row */}
      <div className="mb-2 flex flex-wrap items-center gap-2 px-1">
        {/* Mode chips */}
        <div className="flex flex-wrap gap-1">
          {modes.map((m) => (
            <button
              key={m.value}
              type="button"
              onClick={() => updateMode(m.value)}
              className={cn(
                "rounded-full border px-2.5 py-1 font-mono text-[11px] transition-all",
                mode === m.value
                  ? "border-neon-cyan/60 bg-neon-cyan/10 text-neon-cyan shadow-[0_0_8px_rgba(0,240,255,0.2)]"
                  : "border-border text-muted-foreground hover:border-neon-cyan/30 hover:text-foreground"
              )}
            >
              {m.label}
            </button>
          ))}
        </div>

        <div className="h-4 w-px bg-border" />

        {/* Toggles */}
        <label className="flex cursor-pointer items-center gap-1.5">
          <span className="font-mono text-[10px] text-muted-foreground">
            TOOLS
          </span>
          <button
            type="button"
            role="switch"
            aria-checked={tools}
            onClick={() => updateTools(!tools)}
            className={cn(
              "relative h-4 w-8 rounded-full transition-colors",
              tools
                ? "bg-neon-cyan/30 shadow-[0_0_6px_rgba(0,240,255,0.3)]"
                : "bg-secondary"
            )}
          >
            <span
              className={cn(
                "absolute top-0.5 left-0.5 h-3 w-3 rounded-full transition-all",
                tools ? "translate-x-4 bg-neon-cyan" : "bg-muted-foreground"
              )}
            />
          </button>
        </label>

        <label className="flex cursor-pointer items-center gap-1.5">
          <span className="font-mono text-[10px] text-muted-foreground">
            FACT-CHECK
          </span>
          <button
            type="button"
            role="switch"
            aria-checked={factCheck}
            onClick={() => updateFactCheck(!factCheck)}
            className={cn(
              "relative h-4 w-8 rounded-full transition-colors",
              factCheck
                ? "bg-neon-magenta/30 shadow-[0_0_6px_rgba(255,0,170,0.3)]"
                : "bg-secondary"
            )}
          >
            <span
              className={cn(
                "absolute top-0.5 left-0.5 h-3 w-3 rounded-full transition-all",
                factCheck
                  ? "translate-x-4 bg-neon-magenta"
                  : "bg-muted-foreground"
              )}
            />
          </button>
        </label>

        <label className="flex cursor-pointer items-center gap-1.5">
          <span className="font-mono text-[10px] text-muted-foreground">
            AUTO MEMORY
          </span>
          <button
            type="button"
            role="switch"
            aria-checked={autoMemory}
            onClick={() => onAutoMemoryChange(!autoMemory)}
            className={cn(
              "relative h-4 w-8 rounded-full transition-colors",
              autoMemory
                ? "bg-neon-green/30 shadow-[0_0_6px_rgba(0,255,136,0.3)]"
                : "bg-secondary"
            )}
          >
            <span
              className={cn(
                "absolute top-0.5 left-0.5 h-3 w-3 rounded-full transition-all",
                autoMemory
                  ? "translate-x-4 bg-neon-green"
                  : "bg-muted-foreground"
              )}
            />
          </button>
        </label>
      </div>

      {/* Input row */}
      <div className="flex items-end gap-2">
        <div className="neon-border flex-1 rounded-lg bg-background/50 focus-within:glow-cyan">
          <textarea
            ref={textareaRef}
            autoFocus
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={handleKeyDown}
            placeholder="Enter query..."
            rows={1}
            disabled={disabled}
            className="w-full resize-none bg-transparent px-3 py-2.5 font-mono text-sm text-foreground placeholder:text-muted-foreground focus:outline-none disabled:opacity-50"
          />
        </div>
        <button
          type="button"
          onClick={handleSubmit}
          disabled={disabled || !input.trim()}
          className="flex h-10 w-10 shrink-0 items-center justify-center rounded-lg border border-neon-cyan/50 bg-neon-cyan/10 text-neon-cyan transition-all hover:bg-neon-cyan/20 hover:shadow-[0_0_12px_rgba(0,240,255,0.3)] disabled:opacity-30 disabled:hover:bg-neon-cyan/10 disabled:hover:shadow-none"
          aria-label="Send message"
        >
          <Send className="h-4 w-4" />
        </button>
      </div>
    </div>
  )
}
