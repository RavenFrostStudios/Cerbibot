"use client"

import { cn } from "@/lib/utils"
import type { ToolCall } from "@/lib/types"
import { Shield, X } from "lucide-react"

interface ToolApprovalModalProps {
  toolCall: ToolCall
  onApprove: () => void
  onDeny: () => void
}

const riskColors = {
  low: "text-neon-green border-neon-green/30 shadow-[0_0_6px_rgba(0,255,136,0.3)]",
  medium:
    "text-neon-yellow border-neon-yellow/30 shadow-[0_0_6px_rgba(255,170,0,0.3)]",
  high: "text-neon-red border-neon-red/30 shadow-[0_0_6px_rgba(255,51,51,0.3)]",
}

export function ToolApprovalModal({
  toolCall,
  onApprove,
  onDeny,
}: ToolApprovalModalProps) {
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-background/80 backdrop-blur-sm">
      <div className="glass-card glow-cyan mx-4 w-full max-w-lg rounded-lg p-6">
        <div className="mb-4 flex items-center justify-between">
          <div className="flex items-center gap-2">
            <Shield className="h-5 w-5 text-neon-cyan" />
            <h3 className="font-mono text-sm font-bold tracking-wider text-foreground">
              TOOL APPROVAL REQUIRED
            </h3>
          </div>
          <button
            type="button"
            onClick={onDeny}
            className="text-muted-foreground hover:text-foreground"
            aria-label="Close"
          >
            <X className="h-4 w-4" />
          </button>
        </div>

        <div className="mb-4 space-y-3">
          <div className="flex items-center gap-2">
            <span className="font-mono text-xs text-muted-foreground">
              TOOL:
            </span>
            <span className="font-mono text-sm text-neon-cyan">
              {toolCall.tool_name}
            </span>
          </div>

          <div className="flex items-center gap-2">
            <span className="font-mono text-xs text-muted-foreground">
              RISK:
            </span>
            <span
              className={cn(
                "rounded-full border px-2 py-0.5 font-mono text-xs uppercase",
                riskColors[toolCall.risk_level]
              )}
            >
              {toolCall.risk_level}
            </span>
          </div>

          <div>
            <span className="font-mono text-xs text-muted-foreground">
              ARGUMENTS:
            </span>
            <pre className="mt-1 overflow-auto rounded-md bg-background p-3 font-mono text-xs text-foreground">
              {JSON.stringify(toolCall.arguments, null, 2)}
            </pre>
          </div>

          {toolCall.reason && (
            <div>
              <span className="font-mono text-xs text-muted-foreground">
                REASON:
              </span>
              <p className="mt-1 text-xs text-foreground">{toolCall.reason}</p>
            </div>
          )}
        </div>

        <div className="flex gap-3">
          <button
            type="button"
            onClick={onApprove}
            className="flex-1 rounded-md border border-neon-green/50 bg-neon-green/10 px-4 py-2 font-mono text-sm font-bold text-neon-green transition-all hover:bg-neon-green/20 hover:shadow-[0_0_12px_rgba(0,255,136,0.3)]"
          >
            APPROVE
          </button>
          <button
            type="button"
            onClick={onDeny}
            className="flex-1 rounded-md border border-neon-red/50 bg-neon-red/10 px-4 py-2 font-mono text-sm font-bold text-neon-red transition-all hover:bg-neon-red/20 hover:shadow-[0_0_12px_rgba(255,51,51,0.3)]"
          >
            DENY
          </button>
        </div>
      </div>
    </div>
  )
}
