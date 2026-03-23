"use client"

import { Download, Plus, RefreshCw, Search, Trash2 } from "lucide-react"
import { cn } from "@/lib/utils"
import type { Session, ToolCall } from "@/lib/types"

interface SessionListProps {
  sessions: Session[]
  searchQuery: string
  sortOrder: "newest" | "oldest"
  exportStatus?: string | null
  exportBusy?: boolean
  pendingApprovals: ToolCall[]
  side?: "left" | "right"
  activeId: string | null
  onSelect: (id: string) => void
  onSearchQueryChange: (value: string) => void
  onSortOrderChange: (value: "newest" | "oldest") => void
  onNew: () => void
  onDelete: (id: string) => void
  onExportActive: () => void
  onExportAll: () => void
  onApproveTool: (approvalId: string) => void
  onDenyTool: (approvalId: string) => void
  onRefreshApprovals: () => void
}

export function SessionList({
  sessions,
  searchQuery,
  sortOrder,
  exportStatus,
  exportBusy = false,
  pendingApprovals,
  side = "left",
  activeId,
  onSelect,
  onSearchQueryChange,
  onSortOrderChange,
  onNew,
  onDelete,
  onExportActive,
  onExportAll,
  onApproveTool,
  onDenyTool,
  onRefreshApprovals,
}: SessionListProps) {
  return (
    <div className={cn("flex h-full flex-col border-border bg-sidebar", side === "left" ? "border-r" : "border-l")}>
      <div className="flex items-center justify-between border-b border-border px-3 py-3">
        <span className="font-mono text-xs tracking-wider text-muted-foreground">
          SESSIONS
        </span>
        <button
          type="button"
          onClick={onNew}
          className="flex items-center justify-center rounded-md p-1.5 text-muted-foreground transition-colors hover:bg-secondary hover:text-neon-cyan"
          aria-label="New session"
        >
          <Plus className="h-4 w-4" />
        </button>
      </div>
      <div className="flex-1 overflow-auto p-2">
        <div className="mb-2 space-y-2 rounded-md border border-border bg-background/30 p-2">
          <div className="flex items-center gap-1 rounded border border-border bg-background/60 px-2 py-1">
            <Search className="h-3.5 w-3.5 text-muted-foreground" />
            <input
              type="text"
              value={searchQuery}
              onChange={(e) => onSearchQueryChange(e.target.value)}
              className="w-full bg-transparent font-mono text-xs text-foreground placeholder:text-muted-foreground/50 focus:outline-none"
              placeholder="Search sessions..."
            />
          </div>
          <div className="flex gap-1">
            <button
              type="button"
              onClick={() => onSortOrderChange("newest")}
              className={cn(
                "flex-1 rounded border px-2 py-1 font-mono text-[10px] uppercase",
                sortOrder === "newest"
                  ? "border-neon-cyan/40 bg-neon-cyan/10 text-neon-cyan"
                  : "border-border text-muted-foreground hover:text-foreground"
              )}
            >
              Newest
            </button>
            <button
              type="button"
              onClick={() => onSortOrderChange("oldest")}
              className={cn(
                "flex-1 rounded border px-2 py-1 font-mono text-[10px] uppercase",
                sortOrder === "oldest"
                  ? "border-neon-cyan/40 bg-neon-cyan/10 text-neon-cyan"
                  : "border-border text-muted-foreground hover:text-foreground"
              )}
            >
              Oldest
            </button>
          </div>
          <div className="flex gap-1">
            <button
              type="button"
              onClick={onExportActive}
              disabled={exportBusy || !activeId}
              className="inline-flex flex-1 items-center justify-center gap-1 rounded border border-neon-green/40 bg-neon-green/10 px-2 py-1 font-mono text-[10px] text-neon-green disabled:opacity-50"
            >
              <Download className="h-3 w-3" />
              Export Active
            </button>
            <button
              type="button"
              onClick={onExportAll}
              disabled={exportBusy}
              className="inline-flex flex-1 items-center justify-center gap-1 rounded border border-border px-2 py-1 font-mono text-[10px] text-muted-foreground hover:text-foreground disabled:opacity-50"
            >
              <Download className="h-3 w-3" />
              Export All
            </button>
          </div>
          {exportStatus && (
            <p className="font-mono text-[10px] text-muted-foreground">{exportStatus}</p>
          )}
        </div>
        {sessions.length === 0 && (
          <p className="px-2 py-4 text-center font-mono text-xs text-muted-foreground">
            No sessions yet
          </p>
        )}
        {sessions.map((s) => (
          <div
            key={s.id}
            role="button"
            tabIndex={0}
            onClick={() => onSelect(s.id)}
            onKeyDown={(e) => {
              if (e.key === "Enter" || e.key === " ") {
                e.preventDefault()
                onSelect(s.id)
              }
            }}
            className={cn(
              "group mb-1 flex w-full items-center justify-between rounded-md px-3 py-2 text-left text-sm transition-all",
              activeId === s.id
                ? "neon-border glow-cyan bg-secondary text-neon-cyan"
                : "text-muted-foreground hover:bg-secondary hover:text-foreground"
            )}
            aria-label={`Select session ${s.id}`}
          >
            <span className="truncate font-mono text-xs">
              {s.title || s.id.slice(0, 12)}
            </span>
            <button
              type="button"
              onClick={(e) => {
                e.stopPropagation()
                onDelete(s.id)
              }}
              className="ml-2 shrink-0 opacity-0 transition-opacity group-hover:opacity-100"
              aria-label={`Delete session ${s.id}`}
            >
              <Trash2 className="h-3 w-3 text-neon-red" />
            </button>
          </div>
        ))}

        <div className="mt-3 border-t border-border pt-3">
          <div className="mb-2 flex items-center justify-between px-2">
            <div className="flex items-center gap-1.5">
              <span className="font-mono text-[10px] tracking-wider text-muted-foreground">
                PENDING APPROVALS
              </span>
              <span className="rounded-full border border-border px-1.5 py-0.5 font-mono text-[9px] text-muted-foreground">
                {pendingApprovals.length}
              </span>
            </div>
            <button
              type="button"
              onClick={onRefreshApprovals}
              className="rounded p-1 text-muted-foreground hover:bg-secondary hover:text-foreground"
              aria-label="Refresh approvals"
              title="Refresh approvals"
            >
              <RefreshCw className="h-3 w-3" />
            </button>
          </div>
          {pendingApprovals.length === 0 && (
            <p className="px-2 py-1 text-xs text-muted-foreground">None</p>
          )}
          {pendingApprovals.map((approval, idx) => (
            <div
              key={approval.approval_id || `${approval.tool_name}-${idx}`}
              className="mb-2 rounded-md border border-border bg-background/40 p-2"
            >
              <p className="truncate font-mono text-[11px] text-foreground">
                {approval.tool_name}
              </p>
              {approval.reason && (
                <p className="mt-1 text-[10px] text-muted-foreground">
                  {approval.reason}
                </p>
              )}
              <div className="mt-2 flex gap-1">
                <button
                  type="button"
                  onClick={() => approval.approval_id && onApproveTool(approval.approval_id)}
                  className="rounded border border-neon-green/50 px-2 py-0.5 font-mono text-[10px] text-neon-green hover:bg-neon-green/10"
                  disabled={!approval.approval_id}
                >
                  APPROVE
                </button>
                <button
                  type="button"
                  onClick={() => approval.approval_id && onDenyTool(approval.approval_id)}
                  className="rounded border border-neon-red/50 px-2 py-0.5 font-mono text-[10px] text-neon-red hover:bg-neon-red/10"
                  disabled={!approval.approval_id}
                >
                  DENY
                </button>
              </div>
            </div>
          ))}
        </div>
      </div>
    </div>
  )
}
