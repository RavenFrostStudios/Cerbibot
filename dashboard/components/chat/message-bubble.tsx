"use client"

import { useEffect, useState } from "react"
import ReactMarkdown from "react-markdown"
import { Prism as SyntaxHighlighter } from "react-syntax-highlighter"
import { oneDark } from "react-syntax-highlighter/dist/esm/styles/prism"
import type { ChatMessage } from "@/lib/types"
import { getSettings } from "@/lib/settings"
import { classifyStatusTone, cn, formatUsdAdaptive, statusMessageClass } from "@/lib/utils"

interface MessageBubbleProps {
  message: ChatMessage
  isStreaming?: boolean
}

export function MessageBubble({ message, isStreaming }: MessageBubbleProps) {
  const isUser = message.role === "user"
  const [debugWarnings, setDebugWarnings] = useState(false)
  useEffect(() => {
    const update = () => {
      setDebugWarnings(Boolean(getSettings().debugRetrievalWarnings))
    }
    update()
    window.addEventListener("mmy-settings-changed", update)
    return () => window.removeEventListener("mmy-settings-changed", update)
  }, [])
  const allWarnings = message.metadata?.warnings ?? []
  const technicalWarningPattern =
    /^(Retrieval search failed:|Retrieval source fetch failed:|Direct URL fetch failed:|Used direct weather source fallback|Weather source fallback failed:|Privacy masking applied for cloud call|Privacy rehydration applied in trusted runtime path\.)/i
  const warnings = debugWarnings
    ? allWarnings
    : allWarnings.filter((warning) => !technicalWarningPattern.test(String(warning ?? "")))
  const toolOutputs = message.metadata?.tool_outputs ?? []
  const modeLabel = message.metadata?.mode === "retrieval" ? "web" : message.metadata?.mode
  const displayMode = modeLabel ? `${modeLabel.charAt(0).toUpperCase()}${modeLabel.slice(1)}` : undefined
  const formatDuration = (durationMs: number): string => {
    if (durationMs < 1000) return `${durationMs}ms`
    const total = Math.floor(durationMs / 1000)
    const h = Math.floor(total / 3600)
    const m = Math.floor((total % 3600) / 60)
    const s = total % 60
    if (h > 0) return `${h}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`
    return `${m}:${String(s).padStart(2, "0")}`
  }

  const toolStatusTone = (status: string, exitCode: unknown, stderr: string): "success" | "error" | "progress" | "neutral" => {
    const normalized = String(status || "").trim().toLowerCase()
    if (typeof exitCode === "number" && exitCode !== 0) return "error"
    if (stderr.trim().length > 0 && normalized !== "ok" && normalized !== "success") return "error"
    if (["failed", "error", "denied", "rejected", "timeout", "timed_out"].includes(normalized)) return "error"
    if (["running", "pending", "queued", "waiting"].includes(normalized)) return "progress"
    if (["ok", "success", "completed", "done"].includes(normalized)) return "success"
    return classifyStatusTone(normalized)
  }

  return (
    <div
      className={cn(
        "flex w-full gap-3",
        isUser ? "justify-end" : "justify-start"
      )}
    >
      <div
        className={cn(
          "max-w-[80%] rounded-lg px-4 py-3",
          isUser
            ? "bg-secondary text-foreground"
            : "glass-card text-card-foreground"
        )}
      >
        {isUser ? (
          <p className="whitespace-pre-wrap break-words [overflow-wrap:anywhere] text-sm">{message.content}</p>
        ) : (
          <div className="cyber-prose break-words [overflow-wrap:anywhere] text-sm">
            <ReactMarkdown
              components={{
                code({ className, children, ...rest }) {
                  const match = /language-(\w+)/.exec(className || "")
                  const codeString = String(children).replace(/\n$/, "")
                  if (match) {
                    return (
                      <SyntaxHighlighter
                        style={oneDark}
                        language={match[1]}
                        PreTag="div"
                        customStyle={{
                          background: "#0e0e18",
                          border: "1px solid #1e1e3a",
                          borderRadius: "6px",
                          fontSize: "0.85em",
                        }}
                      >
                        {codeString}
                      </SyntaxHighlighter>
                    )
                  }
                  return (
                    <code className={className} {...rest}>
                      {children}
                    </code>
                  )
                },
              }}
            >
              {message.content}
            </ReactMarkdown>
            {isStreaming && (
              <span className="ml-1 inline-block h-4 w-1.5 animate-pulse-neon bg-neon-cyan" />
            )}
          </div>
        )}

        {!isUser && warnings.length > 0 && (
          <div className="mt-2 rounded-md border border-neon-red/40 bg-neon-red/10 px-2 py-1.5">
            <div className="mb-1 font-mono text-[10px] uppercase tracking-wide text-neon-red">
              Warnings
            </div>
            {warnings.map((warning, idx) => (
              <p key={`warn-${idx}`} className="text-xs text-foreground/90">
                {warning}
              </p>
            ))}
            {warnings.some((warning) => /No sources retrieved; answer may be ungrounded\./i.test(warning)) && (
              <p className="mt-1 text-[11px] text-neon-yellow">
                Tip: try again with a direct URL or a shorter query.
              </p>
            )}
          </div>
        )}

        {!isUser && toolOutputs.length > 0 && (
          <div className="mt-2 space-y-1.5">
            {toolOutputs.map((rawTool, idx) => {
              const tool = rawTool as Record<string, unknown>
              const name = typeof tool.tool_name === "string" ? tool.tool_name : "tool"
              const status = typeof tool.status === "string" ? tool.status : "ok"
              const exitCode = tool.exit_code
              const stdout = typeof tool.stdout === "string" ? tool.stdout : ""
              const stderr = typeof tool.stderr === "string" ? tool.stderr : ""
              const tone = toolStatusTone(status, exitCode, stderr)
              return (
                <details
                  key={`tool-${idx}`}
                  className={cn(
                    "rounded-md px-2 py-1",
                    tone === "error" && "border border-neon-red/30 bg-neon-red/10",
                    tone === "progress" && "border border-neon-yellow/30 bg-neon-yellow/10",
                    tone === "success" && "border border-neon-green/30 bg-neon-green/10",
                    tone === "neutral" && "border border-neon-cyan/30 bg-neon-cyan/10"
                  )}
                >
                  <summary className={statusMessageClass(status, "cursor-pointer font-mono text-[10px]")}>
                    {name} [{status}]
                    {typeof exitCode === "number" ? ` exit=${exitCode}` : ""}
                  </summary>
                  {(stdout || stderr) && (
                    <pre className="mt-1 whitespace-pre-wrap break-words rounded border border-border/50 bg-background/40 p-2 font-mono text-[10px] text-foreground/90">
                      {stdout}
                      {stdout && stderr ? "\n" : ""}
                      {stderr}
                    </pre>
                  )}
                </details>
              )
            })}
          </div>
        )}

        {/* Metadata badge */}
        {message.metadata && !isUser && (
          <div className="mt-2 flex flex-wrap gap-1.5">
            {message.metadata.mode && (
              <span className="rounded-full border border-neon-cyan/30 px-2 py-0.5 font-mono text-[10px] text-neon-cyan">
                {displayMode}
              </span>
            )}
            {message.metadata.provider && (
              <span className="rounded-full border border-neon-magenta/30 px-2 py-0.5 font-mono text-[10px] text-neon-magenta">
                {message.metadata.provider}
              </span>
            )}
            {message.metadata.tokens != null && (
              <span className="rounded-full border border-border px-2 py-0.5 font-mono text-[10px] text-muted-foreground">
                {message.metadata.tokens >= 1000
                  ? `${(message.metadata.tokens / 1000).toFixed(1)}k tok`
                  : `${message.metadata.tokens} tok`}
              </span>
            )}
            {message.metadata.cost != null && (
              <span className="rounded-full border border-border px-2 py-0.5 font-mono text-[10px] text-muted-foreground">
                ${formatUsdAdaptive(message.metadata.cost, { minDecimals: 0, maxDecimals: 5, fallback: "--" })}
              </span>
            )}
            {message.metadata.duration_ms != null && (
              <span className="rounded-full border border-border px-2 py-0.5 font-mono text-[10px] text-muted-foreground">
                {formatDuration(message.metadata.duration_ms)}
              </span>
            )}
          </div>
        )}
      </div>
    </div>
  )
}
