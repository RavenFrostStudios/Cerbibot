"use client"

import { useEffect, useState, useCallback } from "react"
import { fetchMemory, addMemory, deleteMemory } from "@/lib/api"
import { getSettings } from "@/lib/settings"
import type { MemoryEntry } from "@/lib/types"
import { Plus, Search, Trash2, X } from "lucide-react"
import { cn } from "@/lib/utils"

const sourceColors: Record<string, string> = {
  user: "border-neon-cyan/40 text-neon-cyan",
  retrieval: "border-neon-magenta/40 text-neon-magenta",
  web: "border-neon-magenta/40 text-neon-magenta",
  model: "border-neon-green/40 text-neon-green",
}

const displaySource = (source: string | undefined) => {
  const value = source || "user"
  return value === "retrieval" ? "web" : value
}

export function MemoryView() {
  const [entries, setEntries] = useState<MemoryEntry[]>([])
  const [search, setSearch] = useState("")
  const [showAdd, setShowAdd] = useState(false)
  const [newStatement, setNewStatement] = useState("")
  const [newSource, setNewSource] = useState("user")
  const [deleteConfirm, setDeleteConfirm] = useState<string | number | null>(null)
  const [projectId, setProjectId] = useState<string>(() => getSettings().activeProjectId || "default")

  const load = useCallback(async () => {
    try {
      const data = await fetchMemory(projectId)
      setEntries(data)
    } catch {
      // demo data
      setEntries([
        {
          id: "m1",
          statement: "User prefers Python for data analysis tasks",
          source_type: "user",
          confidence: 0.95,
          ttl: 86400,
          created_at: "2026-02-09T10:00:00Z",
        },
        {
          id: "m2",
          statement: "Project uses PostgreSQL as primary database",
          source_type: "retrieval",
          confidence: 0.88,
          ttl: 172800,
          created_at: "2026-02-08T14:30:00Z",
        },
        {
          id: "m3",
          statement: "Claude performs better for code review tasks",
          source_type: "model",
          confidence: 0.72,
          ttl: 43200,
          created_at: "2026-02-10T08:15:00Z",
        },
      ])
    }
  }, [projectId])

  useEffect(() => {
    load()
  }, [load])

  useEffect(() => {
    const update = () => {
      const next = (getSettings().activeProjectId || "default").trim() || "default"
      setProjectId(next)
    }
    update()
    window.addEventListener("mmy-settings-changed", update)
    return () => window.removeEventListener("mmy-settings-changed", update)
  }, [])

  const filtered = entries.filter((e) =>
    e.statement.toLowerCase().includes(search.toLowerCase())
  )

  const handleAdd = async () => {
    if (!newStatement.trim()) return
    try {
      const entry = await addMemory(newStatement, newSource, undefined, projectId)
      setEntries((prev) => [entry, ...prev])
    } catch {
      // add locally
      setEntries((prev) => [
        {
          id: `m-${Date.now()}`,
          statement: newStatement,
          source_type: newSource,
          confidence: 1,
          created_at: new Date().toISOString(),
        },
        ...prev,
      ])
    }
    setNewStatement("")
    setShowAdd(false)
  }

  const handleDelete = async (id: string | number) => {
    try {
      await deleteMemory(String(id), projectId)
    } catch {
      // swallow
    }
    setEntries((prev) => prev.filter((e) => e.id !== id))
    setDeleteConfirm(null)
  }

  const formatTTL = (seconds?: number) => {
    if (!seconds) return "--"
    if (seconds > 86400) return `${Math.round(seconds / 86400)}d`
    if (seconds > 3600) return `${Math.round(seconds / 3600)}h`
    return `${Math.round(seconds / 60)}m`
  }

  return (
    <div className="p-6">
      <div className="mb-6 flex items-center justify-between">
        <div>
          <h1 className="font-mono text-lg font-bold tracking-wider text-foreground">
            MEMORY
          </h1>
          <p className="text-sm text-muted-foreground">
            Persistent knowledge entries
          </p>
          <p className="font-mono text-[10px] text-muted-foreground">
            Project: <span className="text-foreground">{projectId}</span>
          </p>
        </div>
        <button
          type="button"
          onClick={() => setShowAdd(true)}
          className="flex items-center gap-2 rounded-md border border-neon-cyan/50 bg-neon-cyan/10 px-3 py-1.5 font-mono text-xs text-neon-cyan transition-all hover:bg-neon-cyan/20 hover:shadow-[0_0_12px_rgba(0,240,255,0.3)]"
        >
          <Plus className="h-3 w-3" />
          ADD MEMORY
        </button>
      </div>

      {/* Search */}
      <div className="neon-border mb-4 flex items-center gap-2 rounded-lg bg-background/50 px-3 py-2">
        <Search className="h-4 w-4 text-muted-foreground" />
        <input
          type="text"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          placeholder="Search memories..."
          className="flex-1 bg-transparent font-mono text-sm text-foreground placeholder:text-muted-foreground focus:outline-none"
        />
      </div>

      {/* Entries */}
      <div className="space-y-3">
        {filtered.map((entry) => (
          <div key={entry.id} className="glass-card group rounded-lg p-4">
            <div className="mb-2 flex items-start justify-between gap-4">
              <p className="flex-1 text-sm text-foreground">
                {entry.statement}
              </p>
              <button
                type="button"
                onClick={() =>
                  deleteConfirm === entry.id
                    ? handleDelete(entry.id)
                    : setDeleteConfirm(entry.id)
                }
                className={cn(
                  "shrink-0 rounded-md px-2 py-1 font-mono text-[10px] transition-all",
                  deleteConfirm === entry.id
                    ? "border border-neon-red/50 bg-neon-red/20 text-neon-red"
                    : "text-muted-foreground opacity-0 hover:text-neon-red group-hover:opacity-100"
                )}
              >
                {deleteConfirm === entry.id ? (
                  "CONFIRM?"
                ) : (
                  <Trash2 className="h-3 w-3" />
                )}
              </button>
            </div>
            <div className="flex flex-wrap items-center gap-3">
              {/* Source badge */}
              <span
                className={cn(
                  "rounded-full border px-2 py-0.5 font-mono text-[10px] uppercase",
                  sourceColors[displaySource(entry.source_type)] || sourceColors.user
                )}
              >
                {displaySource(entry.source_type)}
              </span>

              {/* Confidence bar */}
              {entry.confidence != null && (
                <div className="flex items-center gap-1.5">
                  <span className="font-mono text-[10px] text-muted-foreground">
                    CONF
                  </span>
                  <div className="h-1.5 w-16 overflow-hidden rounded-full bg-secondary">
                    <div
                      className="h-full rounded-full"
                      style={{
                        width: `${entry.confidence * 100}%`,
                        background: `linear-gradient(90deg, #00f0ff, #ff00aa)`,
                      }}
                    />
                  </div>
                  <span className="font-mono text-[10px] text-muted-foreground">
                    {(entry.confidence * 100).toFixed(0)}%
                  </span>
                </div>
              )}

              {/* TTL */}
              {(entry.ttl || entry.ttl_days) && (
                <span className="font-mono text-[10px] text-muted-foreground">
                  TTL: {formatTTL(entry.ttl ?? (entry.ttl_days ? entry.ttl_days * 86400 : undefined))}
                </span>
              )}

              {/* Date */}
              {entry.created_at && (
                <span className="font-mono text-[10px] text-muted-foreground">
                  {new Date(entry.created_at).toLocaleDateString()}
                </span>
              )}
            </div>
          </div>
        ))}

        {filtered.length === 0 && (
          <div className="py-12 text-center font-mono text-sm text-muted-foreground">
            {search
              ? "No matching memories found"
              : "No memory entries yet"}
          </div>
        )}
      </div>

      {/* Add modal */}
      {showAdd && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-background/80 backdrop-blur-sm">
          <div className="glass-card glow-cyan mx-4 w-full max-w-md rounded-lg p-6">
            <div className="mb-4 flex items-center justify-between">
              <h3 className="font-mono text-sm font-bold tracking-wider text-foreground">
                ADD MEMORY
              </h3>
              <button
                type="button"
                onClick={() => setShowAdd(false)}
                className="text-muted-foreground hover:text-foreground"
                aria-label="Close"
              >
                <X className="h-4 w-4" />
              </button>
            </div>
            <textarea
              value={newStatement}
              onChange={(e) => setNewStatement(e.target.value)}
              placeholder="Enter a memory statement..."
              rows={3}
              className="neon-border mb-3 w-full rounded-lg bg-background/50 px-3 py-2 font-mono text-sm text-foreground placeholder:text-muted-foreground focus:outline-none focus:glow-cyan"
            />
            <div className="mb-4">
              <span className="mb-1 block font-mono text-[10px] text-muted-foreground">
                SOURCE TYPE
              </span>
              <div className="flex gap-2">
                {["user", "retrieval", "model"].map((src) => (
                  <button
                    key={src}
                    type="button"
                    onClick={() => setNewSource(src)}
                    className={cn(
                      "rounded-full border px-3 py-1 font-mono text-[11px] uppercase transition-all",
                      newSource === src
                        ? sourceColors[src]
                        : "border-border text-muted-foreground"
                    )}
                  >
                    {displaySource(src)}
                  </button>
                ))}
              </div>
            </div>
            <button
              type="button"
              onClick={handleAdd}
              disabled={!newStatement.trim()}
              className="w-full rounded-md border border-neon-cyan/50 bg-neon-cyan/10 px-4 py-2 font-mono text-sm font-bold text-neon-cyan transition-all hover:bg-neon-cyan/20 disabled:opacity-30"
            >
              STORE MEMORY
            </button>
          </div>
        </div>
      )}
    </div>
  )
}
