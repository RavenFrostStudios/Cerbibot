"use client"

import { Fragment, useCallback, useEffect, useMemo, useState } from "react"
import { RefreshCw } from "lucide-react"
import {
  clearRuns,
  deleteRun,
  fetchRuns,
  fetchRunsDag,
  fetchRunTriggers,
  getAdminPasswordStatus,
  resumeRun,
  rotateRunTriggerSecret,
  saveRunTriggers,
  sweepRunTriggers,
  updateRunDependencies,
} from "@/lib/api"
import { setRouteActiveSessionId } from "@/lib/chat-state"
import { cn, statusMessageClass } from "@/lib/utils"
import type { RunBlocker, RunDag, RunInfo, RunTrigger } from "@/lib/types"
import { useRouter } from "next/navigation"

type RunFilter = "all" | "running" | "blocked" | "failed" | "completed"
type RunsLayout = "board" | "list"

function trimUiText(value: string, max = 48): string {
  const normalized = value.replace(/\s+/g, " ").trim()
  if (normalized.length <= max) return normalized
  return `${normalized.slice(0, Math.max(1, max - 1)).trimEnd()}...`
}

function isRunBlocked(row: RunInfo): boolean {
  const blockers = Array.isArray(row.blockers) ? row.blockers : []
  return blockers.some((item) => String(item?.status ?? "open").toLowerCase() === "open")
}

function parseIsoTime(value?: string | null): number | null {
  if (!value) return null
  const ts = Date.parse(value)
  return Number.isFinite(ts) ? ts : null
}

function runElapsedMs(row: RunInfo, nowMs: number): number | null {
  const created = parseIsoTime(row.created_at)
  if (created === null) return null
  return Math.max(0, nowMs - created)
}

function formatElapsed(ms: number | null): string {
  if (ms === null) return "n/a"
  const total = Math.floor(ms / 1000)
  const h = Math.floor(total / 3600)
  const m = Math.floor((total % 3600) / 60)
  const s = total % 60
  if (h > 0) return `${h}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`
  return `${m}:${String(s).padStart(2, "0")}`
}

function isRunStalled(row: RunInfo, nowMs: number): boolean {
  const status = String(row.status ?? "").toLowerCase()
  if (!["running", "resuming", "waiting", "paused"].includes(status)) return false
  const lastHeartbeat = parseIsoTime(row.last_heartbeat_at)
  const lastUpdated = parseIsoTime(row.updated_at)
  const lastSeen = Math.max(lastHeartbeat ?? 0, lastUpdated ?? 0)
  if (!lastSeen) return false
  return nowMs - lastSeen > 60_000
}

function canRetryRun(row: RunInfo): boolean {
  const status = String(row.status ?? "").toLowerCase()
  return !["running", "resuming"].includes(status)
}

function runBoardStatus(row: RunInfo): "queued" | "active" | "blocked" | "failed" | "done" {
  if (isRunBlocked(row)) return "blocked"
  const status = String(row.status ?? "").toLowerCase()
  if (status === "completed") return "done"
  if (status === "failed") return "failed"
  if (status === "running" || status === "resuming") return "active"
  return "queued"
}

function runStatusClass(row: RunInfo): string {
  const status = runBoardStatus(row)
  if (status === "blocked") return "text-neon-yellow"
  if (status === "failed") return "text-neon-red"
  if (status === "done") return "text-neon-green"
  return "text-neon-cyan"
}

function boardColumnTone(key: "queued" | "active" | "blocked" | "failed" | "done"): string {
  if (key === "blocked") return "border-neon-yellow/30 bg-neon-yellow/5"
  if (key === "failed") return "border-neon-red/30 bg-neon-red/5"
  if (key === "done") return "border-neon-green/30 bg-neon-green/5"
  if (key === "active") return "border-neon-cyan/30 bg-neon-cyan/5"
  return "border-border bg-card/30"
}

export function RunsView() {
  const router = useRouter()
  const [runs, setRuns] = useState<RunInfo[]>([])
  const [loading, setLoading] = useState(true)
  const [filter, setFilter] = useState<RunFilter>("all")
  const [searchQuery, setSearchQuery] = useState("")
  const [page, setPage] = useState(1)
  const [pageSize, setPageSize] = useState<10 | 20 | 50>(20)
  const [status, setStatus] = useState<string | null>(null)
  const [busyRunId, setBusyRunId] = useState<string | null>(null)
  const [editingRunId, setEditingRunId] = useState<string | null>(null)
  const [depEditorText, setDepEditorText] = useState("")
  const [blockerEditor, setBlockerEditor] = useState<RunBlocker[]>([])
  const [runDagOpen, setRunDagOpen] = useState(false)
  const [runDagLoading, setRunDagLoading] = useState(false)
  const [runDagError, setRunDagError] = useState<string | null>(null)
  const [runDag, setRunDag] = useState<RunDag | null>(null)
  const [runDagFocusOnly, setRunDagFocusOnly] = useState(false)
  const [runDagFocusId, setRunDagFocusId] = useState("")
  const [runDagFocusDepth, setRunDagFocusDepth] = useState<1 | 2>(1)
  const [nowMs, setNowMs] = useState<number>(Date.now())
  const [triggers, setTriggers] = useState<RunTrigger[]>([])
  const [triggerStatus, setTriggerStatus] = useState<string | null>(null)
  const [triggerBusy, setTriggerBusy] = useState(false)
  const [layout, setLayout] = useState<RunsLayout>("board")

  const parseDeps = useCallback((text: string): string[] => {
    const tokens = text
      .split(/[\n,]+/)
      .map((item) => item.trim())
      .filter(Boolean)
    return Array.from(new Set(tokens))
  }, [])

  const loadRuns = useCallback(async () => {
    setLoading(true)
    setStatus(null)
    try {
      const rows = await fetchRuns({ limit: 100 })
      setRuns(rows)
    } catch (err) {
      const msg = err instanceof Error ? err.message : "Failed to load runs"
      setStatus(msg)
      setRuns([])
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void loadRuns()
  }, [loadRuns])

  const loadTriggers = useCallback(async () => {
    try {
      const rows = await fetchRunTriggers()
      setTriggers(rows)
    } catch (err) {
      const msg = err instanceof Error ? err.message : "Failed to load triggers"
      setTriggerStatus(msg)
      setTriggers([])
    }
  }, [])

  useEffect(() => {
    void loadTriggers()
  }, [loadTriggers])

  useEffect(() => {
    const id = window.setInterval(() => setNowMs(Date.now()), 1000)
    return () => window.clearInterval(id)
  }, [])

  const filteredRuns = useMemo(() => {
    const byStatus =
      filter === "all"
        ? runs
        : filter === "blocked"
          ? runs.filter((row) => isRunBlocked(row))
          : runs.filter((row) => String(row.status) === filter)
    const q = searchQuery.trim().toLowerCase()
    if (!q) return byStatus
    return byStatus.filter((row) => {
      const haystack = [
        row.run_id,
        row.status,
        row.endpoint,
        row.session_id,
        row.checkpoint?.stage,
        row.checkpoint?.note,
        row.error_detail,
        Array.isArray(row.dependencies) ? row.dependencies.join(" ") : "",
        Array.isArray(row.blockers)
          ? row.blockers.map((blocker) => `${blocker.code ?? ""} ${blocker.message ?? ""}`).join(" ")
          : "",
      ]
        .filter(Boolean)
        .join(" ")
        .toLowerCase()
      return haystack.includes(q)
    })
  }, [runs, filter, searchQuery])

  const totalPages = useMemo(
    () => Math.max(1, Math.ceil(filteredRuns.length / pageSize)),
    [filteredRuns.length, pageSize]
  )
  const pagedRuns = useMemo(() => {
    const safePage = Math.min(page, totalPages)
    const start = (safePage - 1) * pageSize
    return filteredRuns.slice(start, start + pageSize)
  }, [filteredRuns, page, pageSize, totalPages])

  useEffect(() => {
    setPage(1)
  }, [filter, searchQuery, pageSize])

  useEffect(() => {
    if (page > totalPages) setPage(totalPages)
  }, [page, totalPages])

  const boardColumns = useMemo(
    () => [
      {
        key: "queued" as const,
        label: "Queued",
        hint: "waiting, paused, and new work",
        rows: filteredRuns.filter((row) => runBoardStatus(row) === "queued"),
      },
      {
        key: "active" as const,
        label: "Active",
        hint: "currently running or resuming",
        rows: filteredRuns.filter((row) => runBoardStatus(row) === "active"),
      },
      {
        key: "blocked" as const,
        label: "Blocked",
        hint: "open blockers or dependency waits",
        rows: filteredRuns.filter((row) => runBoardStatus(row) === "blocked"),
      },
      {
        key: "failed" as const,
        label: "Failed",
        hint: "needs retry or diagnosis",
        rows: filteredRuns.filter((row) => runBoardStatus(row) === "failed"),
      },
      {
        key: "done" as const,
        label: "Done",
        hint: "completed successfully",
        rows: filteredRuns.filter((row) => runBoardStatus(row) === "done"),
      },
    ],
    [filteredRuns]
  )

  const editingRun = useMemo(
    () => runs.find((row) => row.run_id === editingRunId) ?? null,
    [editingRunId, runs]
  )

  const resumeSingle = async (runId: string) => {
    if (busyRunId) return
    setBusyRunId(runId)
    setStatus(`Resuming ${runId.slice(0, 16)}...`)
    try {
      const payload = await resumeRun(runId)
      const resumedSessionId = payload.resume?.session_id
      await loadRuns()
      if (resumedSessionId) {
        setRouteActiveSessionId(resumedSessionId)
        setStatus("Run resumed. Opening session in chat...")
        router.push("/")
        return
      }
      setStatus("Run resumed.")
    } catch (err) {
      const msg = err instanceof Error ? err.message : "Resume failed"
      setStatus(msg)
    } finally {
      setBusyRunId(null)
    }
  }

  const deleteSingle = async (runId: string) => {
    if (busyRunId) return
    setBusyRunId(runId)
    setStatus(`Deleting ${runId.slice(0, 16)}...`)
    try {
      await deleteRun(runId)
      await loadRuns()
      setStatus("Run deleted.")
    } catch (err) {
      const msg = err instanceof Error ? err.message : "Delete failed"
      setStatus(msg)
    } finally {
      setBusyRunId(null)
    }
  }

  const clearCompleted = async () => {
    if (busyRunId) return
    setStatus("Clearing completed runs...")
    try {
      const payload = await clearRuns("completed")
      await loadRuns()
      setStatus(`Cleared ${payload.deleted} completed run(s).`)
    } catch (err) {
      const msg = err instanceof Error ? err.message : "Clear failed"
      setStatus(msg)
    }
  }

  const retryAllFailed = async () => {
    if (busyRunId) return
    const failedRuns = runs.filter((row) => String(row.status ?? "").toLowerCase() === "failed")
    if (failedRuns.length === 0) {
      setStatus("No failed runs to retry.")
      return
    }
    setBusyRunId("__bulk_retry__")
    let resumed = 0
    let errored = 0
    try {
      for (let i = 0; i < failedRuns.length; i += 1) {
        const run = failedRuns[i]
        setStatus(`Retrying failed runs (${i + 1}/${failedRuns.length})...`)
        try {
          await resumeRun(run.run_id)
          resumed += 1
        } catch {
          errored += 1
        }
      }
      await loadRuns()
      setStatus(`Retry all complete: ${resumed} resumed, ${errored} failed.`)
    } catch (err) {
      const msg = err instanceof Error ? err.message : "Bulk retry failed"
      setStatus(`Retry all failed (${msg}).`)
    } finally {
      setBusyRunId(null)
    }
  }

  const defaultTrigger = (): RunTrigger => ({
    trigger_id: "",
    name: "New webhook trigger",
    enabled: true,
    project_id: "default",
    session_id: "",
    mode: "single",
    provider: "",
    message: "Run the saved automation task.",
    tools: false,
    fact_check: false,
    assistant_name: "",
    assistant_instructions: "",
    strict_profile: false,
    web_assist_mode: "off",
    interval_minutes: 0,
    next_run_at: "",
    secret: "",
    webhook_path: "",
    webhook_url: "",
    last_triggered_at: "",
    last_run_id: "",
    updated_at: "",
  })

  const persistTriggers = async (next: RunTrigger[]) => {
    setTriggerBusy(true)
    setTriggerStatus("Saving webhook triggers...")
    try {
      const adminPassword = await maybePromptAdminPassword()
      const rows = await saveRunTriggers(next, adminPassword)
      setTriggers(rows)
      setTriggerStatus("Webhook triggers saved.")
    } catch (err) {
      const msg = err instanceof Error ? err.message : "Save failed"
      setTriggerStatus(`Could not save webhook triggers (${msg}).`)
    } finally {
      setTriggerBusy(false)
    }
  }

  const patchTrigger = (index: number, patch: Partial<RunTrigger>) => {
    setTriggers((prev) => prev.map((row, idx) => (idx === index ? { ...row, ...patch } : row)))
  }

  const addTrigger = () => {
    setTriggers((prev) => [...prev, defaultTrigger()])
    setTriggerStatus("New webhook trigger added locally. Save to persist and generate a secret URL.")
  }

  const removeTrigger = (index: number) => {
    const next = triggers.filter((_, idx) => idx !== index)
    void persistTriggers(next)
  }

  const rotateSecret = async (triggerId: string) => {
    if (!triggerId || triggerBusy) return
    setTriggerBusy(true)
    setTriggerStatus("Rotating webhook secret...")
    try {
      const adminPassword = await maybePromptAdminPassword()
      const row = await rotateRunTriggerSecret(triggerId, adminPassword)
      setTriggers((prev) => prev.map((item) => (item.trigger_id === triggerId ? row : item)))
      setTriggerStatus("Webhook secret rotated.")
    } catch (err) {
      const msg = err instanceof Error ? err.message : "Rotate failed"
      setTriggerStatus(`Could not rotate webhook secret (${msg}).`)
    } finally {
      setTriggerBusy(false)
    }
  }

  const copyTriggerUrl = async (value: string) => {
    try {
      await navigator.clipboard.writeText(value)
      setTriggerStatus("Webhook URL copied.")
    } catch {
      setTriggerStatus("Could not copy webhook URL.")
    }
  }

  const sweepTriggers = async () => {
    if (triggerBusy) return
    setTriggerBusy(true)
    setTriggerStatus("Sweeping due triggers...")
    try {
      const result = await sweepRunTriggers()
      await loadRuns()
      await loadTriggers()
      setTriggerStatus(`Sweep complete: ${result.fired} fired, ${result.due} due, ${result.checked} checked.`)
    } catch (err) {
      const msg = err instanceof Error ? err.message : "Sweep failed"
      setTriggerStatus(`Could not sweep triggers (${msg}).`)
    } finally {
      setTriggerBusy(false)
    }
  }

  const maybePromptAdminPassword = async (): Promise<string | undefined> => {
    const status = await getAdminPasswordStatus()
    if (!status.configured) return undefined
    const password = window.prompt("Admin password required for trigger changes")
    if (!password) throw new Error("Admin password entry cancelled")
    return password
  }

  const openSession = (sessionId: string) => {
    setRouteActiveSessionId(sessionId)
    router.push("/")
  }

  const copyRunId = async (runId: string) => {
    try {
      await navigator.clipboard.writeText(runId)
      setStatus(`Copied run ID: ${runId.slice(0, 18)}`)
    } catch {
      setStatus("Could not copy run ID.")
    }
  }

  const openArtifacts = async (runId: string) => {
    await navigator.clipboard.writeText(runId).catch(() => {})
    router.push("/artifacts")
  }

  const startEdit = (row: RunInfo) => {
    setEditingRunId(row.run_id)
    setDepEditorText(Array.isArray(row.dependencies) ? row.dependencies.join(", ") : "")
    setBlockerEditor(
      Array.isArray(row.blockers)
        ? row.blockers.map((item, idx) => ({
            blocker_id: item.blocker_id || `blocker-${idx + 1}`,
            code: item.code || "",
            message: item.message || "",
            severity: item.severity || "medium",
            status: item.status || "open",
          }))
        : []
    )
  }

  const cancelEdit = () => {
    setEditingRunId(null)
    setDepEditorText("")
    setBlockerEditor([])
  }

  const addBlocker = () => {
    setBlockerEditor((prev) => [
      ...prev,
      {
        blocker_id: `blocker-${Date.now()}`,
        code: "",
        message: "",
        severity: "medium",
        status: "open",
      },
    ])
  }

  const updateBlockerField = (
    index: number,
    field: keyof RunBlocker,
    value: string
  ) => {
    setBlockerEditor((prev) =>
      prev.map((item, idx) => (idx === index ? { ...item, [field]: value } : item))
    )
  }

  const removeBlocker = (index: number) => {
    setBlockerEditor((prev) => prev.filter((_, idx) => idx !== index))
  }

  const saveEdit = async (runId: string) => {
    if (busyRunId) return
    setBusyRunId(runId)
    setStatus(`Saving DAG metadata for ${runId.slice(0, 16)}...`)
    try {
      const dependencies = parseDeps(depEditorText)
      const blockers = blockerEditor
        .map((item, idx) => ({
          blocker_id: item.blocker_id || `blocker-${idx + 1}`,
          code: String(item.code || "").trim(),
          message: String(item.message || "").trim(),
          severity: String(item.severity || "medium"),
          status: String(item.status || "open"),
        }))
        .filter((item) => item.code || item.message)
      await updateRunDependencies(runId, { dependencies, blockers })
      await loadRuns()
      setStatus("Run DAG metadata saved.")
      cancelEdit()
    } catch (err) {
      const msg = err instanceof Error ? err.message : "Failed to save run DAG metadata"
      setStatus(msg)
    } finally {
      setBusyRunId(null)
    }
  }

  const openRunDagModal = async () => {
    setRunDagOpen(true)
    setRunDagLoading(true)
    setRunDagError(null)
    setRunDagFocusOnly(false)
    setRunDagFocusId("")
    setRunDagFocusDepth(1)
    try {
      const dag = await fetchRunsDag(200)
      setRunDag(dag)
    } catch (err) {
      const msg = err instanceof Error ? err.message : "Failed to load run DAG"
      setRunDagError(msg)
      setRunDag(null)
    } finally {
      setRunDagLoading(false)
    }
  }

  const focusedRunDag = useMemo(() => {
    if (!runDag) return null
    if (!runDagFocusOnly || !runDagFocusId.trim()) return runDag
    const target = runDagFocusId.trim()
    const adjacency = new Map<string, Set<string>>()
    for (const edge of runDag.edges ?? []) {
      if (!adjacency.has(edge.from)) adjacency.set(edge.from, new Set<string>())
      if (!adjacency.has(edge.to)) adjacency.set(edge.to, new Set<string>())
      adjacency.get(edge.from)?.add(edge.to)
      adjacency.get(edge.to)?.add(edge.from)
    }
    const related = new Set<string>([target])
    let frontier = new Set<string>([target])
    for (let depth = 0; depth < runDagFocusDepth; depth += 1) {
      const next = new Set<string>()
      for (const nodeId of frontier) {
        const neighbors = adjacency.get(nodeId)
        if (!neighbors) continue
        for (const neighbor of neighbors) {
          if (!related.has(neighbor)) {
            related.add(neighbor)
            next.add(neighbor)
          }
        }
      }
      frontier = next
      if (frontier.size === 0) break
    }
    const nodes = (runDag.nodes ?? []).filter((node) => related.has(node.id))
    const edges = (runDag.edges ?? []).filter((edge) => related.has(edge.from) && related.has(edge.to))
    return { ...runDag, nodes, edges, count: nodes.length }
  }, [runDag, runDagFocusDepth, runDagFocusId, runDagFocusOnly])

  return (
    <div className="p-6">
      <div className="mb-6 flex items-center justify-between">
        <div>
          <h1 className="font-mono text-lg font-bold tracking-wider text-foreground">RUNS</h1>
          <p className="text-sm text-muted-foreground">Run lifecycle, checkpoints, and recovery actions</p>
        </div>
        <div className="flex items-center gap-2">
          <button
            type="button"
            onClick={() => {
              void loadRuns()
            }}
            disabled={loading}
            className="inline-flex items-center gap-2 rounded border border-border px-2.5 py-1.5 font-mono text-xs text-muted-foreground hover:text-foreground disabled:opacity-50"
          >
            <RefreshCw className={`h-3 w-3 ${loading ? "animate-spin" : ""}`} />
            REFRESH
          </button>
          <button
            type="button"
            onClick={() => {
              void clearCompleted()
            }}
            disabled={loading || busyRunId !== null}
            className="rounded border border-border px-2.5 py-1.5 font-mono text-xs text-muted-foreground hover:text-foreground disabled:opacity-50"
          >
            CLEAR COMPLETED
          </button>
          <button
            type="button"
            onClick={() => {
              void retryAllFailed()
            }}
            disabled={loading || busyRunId !== null}
            className="rounded border border-neon-yellow/40 bg-neon-yellow/10 px-2.5 py-1.5 font-mono text-xs text-neon-yellow disabled:opacity-50"
          >
            RETRY ALL FAILED
          </button>
          <button
            type="button"
            onClick={() => {
              void openRunDagModal()
            }}
            className="rounded border border-border px-2.5 py-1.5 font-mono text-xs text-muted-foreground hover:text-neon-cyan"
          >
            VIEW DAG
          </button>
        </div>
      </div>

      <div className="mb-4 rounded-lg border border-border bg-card/30 p-3">
        <div className="mb-2 flex items-center justify-between gap-2">
          <div>
            <h2 className="font-mono text-xs tracking-wider text-foreground">WEBHOOK TRIGGERS</h2>
            <p className="font-mono text-[10px] text-muted-foreground">
              Save externally callable run templates. Each trigger fires a normal chat run through a secret URL.
            </p>
          </div>
          <div className="flex items-center gap-2">
            <button
              type="button"
              onClick={addTrigger}
              disabled={triggerBusy}
              className="rounded border border-neon-cyan/40 bg-neon-cyan/10 px-2 py-1 font-mono text-[10px] text-neon-cyan disabled:opacity-50"
            >
              ADD TRIGGER
            </button>
            <button
              type="button"
              onClick={() => void persistTriggers(triggers)}
              disabled={triggerBusy}
              className="rounded border border-border px-2 py-1 font-mono text-[10px] text-muted-foreground hover:text-foreground disabled:opacity-50"
            >
              SAVE TRIGGERS
            </button>
            <button
              type="button"
              onClick={() => void sweepTriggers()}
              disabled={triggerBusy}
              className="rounded border border-neon-yellow/40 bg-neon-yellow/10 px-2 py-1 font-mono text-[10px] text-neon-yellow disabled:opacity-50"
            >
              SWEEP DUE
            </button>
          </div>
        </div>
        {triggerStatus && <p className={statusMessageClass(triggerStatus, "mb-2 font-mono text-[10px]")}>{triggerStatus}</p>}
        <div className="space-y-3">
          {triggers.length === 0 && (
            <p className="font-mono text-[10px] text-muted-foreground">
              No webhook triggers configured yet. Add one, save it, then copy its URL into the external system.
            </p>
          )}
          {triggers.map((trigger, idx) => (
            <div key={trigger.trigger_id || `draft-${idx}`} className="rounded border border-border bg-background/40 p-2">
              <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
                <label className="inline-flex items-center gap-1 font-mono text-[10px] text-muted-foreground">
                  <input
                    type="checkbox"
                    checked={Boolean(trigger.enabled)}
                    onChange={(e) => patchTrigger(idx, { enabled: e.target.checked })}
                    className="h-3 w-3"
                  />
                  enabled
                </label>
                <div className="flex flex-wrap gap-2">
                  <button
                    type="button"
                    onClick={() => void copyTriggerUrl(trigger.webhook_url)}
                    disabled={!trigger.webhook_url}
                    className="rounded border border-border px-2 py-1 font-mono text-[10px] text-muted-foreground hover:text-foreground disabled:opacity-50"
                  >
                    COPY URL
                  </button>
                  <button
                    type="button"
                    onClick={() => void rotateSecret(trigger.trigger_id)}
                    disabled={!trigger.trigger_id || triggerBusy}
                    className="rounded border border-neon-yellow/40 bg-neon-yellow/10 px-2 py-1 font-mono text-[10px] text-neon-yellow disabled:opacity-50"
                  >
                    ROTATE SECRET
                  </button>
                  <button
                    type="button"
                    onClick={() => removeTrigger(idx)}
                    disabled={triggerBusy}
                    className="rounded border border-neon-red/40 bg-neon-red/10 px-2 py-1 font-mono text-[10px] text-neon-red disabled:opacity-50"
                  >
                    REMOVE
                  </button>
                </div>
              </div>
              <div className="grid gap-2 md:grid-cols-2">
                <input
                  type="text"
                  value={trigger.name}
                  onChange={(e) => patchTrigger(idx, { name: e.target.value })}
                  placeholder="Trigger name"
                  className="rounded border border-border bg-background/60 px-2 py-1 font-mono text-[10px] text-foreground focus:outline-none"
                />
                <input
                  type="text"
                  value={trigger.project_id ?? ""}
                  onChange={(e) => patchTrigger(idx, { project_id: e.target.value })}
                  placeholder="Project ID"
                  className="rounded border border-border bg-background/60 px-2 py-1 font-mono text-[10px] text-foreground focus:outline-none"
                />
                <select
                  value={trigger.mode}
                  onChange={(e) => patchTrigger(idx, { mode: e.target.value as RunTrigger["mode"] })}
                  className="rounded border border-border bg-background/60 px-2 py-1 font-mono text-[10px] text-foreground focus:outline-none"
                >
                  {["single", "critique", "debate", "consensus", "council", "retrieval"].map((mode) => (
                    <option key={mode} value={mode}>{mode}</option>
                  ))}
                </select>
                <input
                  type="text"
                  value={trigger.provider ?? ""}
                  onChange={(e) => patchTrigger(idx, { provider: e.target.value })}
                  placeholder="Provider override (optional)"
                  className="rounded border border-border bg-background/60 px-2 py-1 font-mono text-[10px] text-foreground focus:outline-none"
                />
                <input
                  type="number"
                  min={0}
                  value={String(trigger.interval_minutes ?? 0)}
                  onChange={(e) => patchTrigger(idx, { interval_minutes: Number.parseInt(e.target.value || "0", 10) || 0 })}
                  placeholder="Interval minutes (0 = webhook only)"
                  className="rounded border border-border bg-background/60 px-2 py-1 font-mono text-[10px] text-foreground focus:outline-none"
                />
                <div className="rounded border border-border bg-background/60 px-2 py-1 font-mono text-[10px] text-muted-foreground">
                  Next run: {trigger.next_run_at || "webhook only"}
                </div>
              </div>
              <textarea
                value={trigger.message}
                onChange={(e) => patchTrigger(idx, { message: e.target.value })}
                rows={3}
                placeholder="Message template sent when the webhook fires..."
                className="mt-2 w-full rounded border border-border bg-background/60 px-2 py-1 font-mono text-[10px] text-foreground focus:outline-none"
              />
              <div className="mt-2 flex flex-wrap gap-3">
                <label className="inline-flex items-center gap-1 font-mono text-[10px] text-muted-foreground">
                  <input type="checkbox" checked={Boolean(trigger.tools)} onChange={(e) => patchTrigger(idx, { tools: e.target.checked })} className="h-3 w-3" />
                  tools
                </label>
                <label className="inline-flex items-center gap-1 font-mono text-[10px] text-muted-foreground">
                  <input type="checkbox" checked={Boolean(trigger.fact_check)} onChange={(e) => patchTrigger(idx, { fact_check: e.target.checked })} className="h-3 w-3" />
                  fact check
                </label>
                <label className="inline-flex items-center gap-1 font-mono text-[10px] text-muted-foreground">
                  <input type="checkbox" checked={Boolean(trigger.strict_profile)} onChange={(e) => patchTrigger(idx, { strict_profile: e.target.checked })} className="h-3 w-3" />
                  strict profile
                </label>
              </div>
              <p className="mt-2 font-mono text-[10px] text-muted-foreground break-all">
                {trigger.webhook_url || "Save this trigger to generate a webhook URL."}
              </p>
              <p className="mt-1 font-mono text-[10px] text-muted-foreground">
                Last run: {trigger.last_run_id || "n/a"} {trigger.last_triggered_at ? `| ${trigger.last_triggered_at}` : ""}
              </p>
            </div>
          ))}
        </div>
      </div>

      <div className="mb-3 flex flex-wrap items-center gap-2">
        {(["all", "running", "blocked", "failed", "completed"] as const).map((entry) => (
          <button
            key={entry}
            type="button"
            onClick={() => setFilter(entry)}
            className={`rounded border px-2 py-1 font-mono text-[10px] uppercase ${
              filter === entry
                ? entry === "failed"
                  ? "border-neon-red/40 bg-neon-red/10 text-neon-red"
                  : entry === "blocked"
                    ? "border-neon-yellow/40 bg-neon-yellow/10 text-neon-yellow"
                  : entry === "completed"
                    ? "border-neon-green/40 bg-neon-green/10 text-neon-green"
                    : "border-neon-cyan/40 bg-neon-cyan/10 text-neon-cyan"
                : "border-border text-muted-foreground"
            }`}
          >
            {entry}
          </button>
        ))}
        <input
          type="text"
          value={searchQuery}
          onChange={(e) => setSearchQuery(e.target.value)}
          placeholder="Search runs..."
          className="ml-auto min-w-48 rounded border border-border bg-background/60 px-2 py-1 font-mono text-[10px] text-foreground placeholder:text-muted-foreground/60 focus:outline-none"
        />
        <select
          value={pageSize}
          onChange={(e) => setPageSize(Number(e.target.value) as 10 | 20 | 50)}
          className="rounded border border-border bg-background/60 px-1.5 py-1 font-mono text-[10px] text-foreground focus:outline-none"
          aria-label="Runs per page"
        >
          <option value={10}>10 / page</option>
          <option value={20}>20 / page</option>
          <option value={50}>50 / page</option>
        </select>
        <div className="inline-flex overflow-hidden rounded border border-border">
          {(["board", "list"] as RunsLayout[]).map((value) => (
            <button
              key={value}
              type="button"
              onClick={() => setLayout(value)}
              className={cn(
                "px-2 py-1 font-mono text-[10px]",
                layout === value ? "bg-neon-cyan/10 text-neon-cyan" : "text-muted-foreground hover:text-foreground"
              )}
            >
              {value.toUpperCase()}
            </button>
          ))}
        </div>
      </div>

      {status && <p className={statusMessageClass(status, "mb-3 font-mono text-xs")}>{status}</p>}

      {layout === "board" ? (
        <div className="grid gap-3 xl:grid-cols-5">
          {boardColumns.map((column) => (
            <section key={column.key} className={cn("rounded-lg border p-3", boardColumnTone(column.key))}>
              <div className="mb-3 flex items-start justify-between gap-2">
                <div>
                  <h2 className="font-mono text-xs tracking-wider text-foreground">{column.label}</h2>
                  <p className="font-mono text-[10px] text-muted-foreground">{column.hint}</p>
                </div>
                <span className="rounded border border-border/70 px-1.5 py-0.5 font-mono text-[10px] text-muted-foreground">
                  {column.rows.length}
                </span>
              </div>
              <div className="grid gap-2">
                {column.rows.length === 0 && (
                  <div className="rounded border border-dashed border-border/70 px-2 py-3 font-mono text-[10px] text-muted-foreground">
                    No runs in this lane.
                  </div>
                )}
                {column.rows.map((row) => {
                  const openBlockers = Array.isArray(row.blockers)
                    ? row.blockers.filter((item) => String(item?.status ?? "open").toLowerCase() === "open")
                    : []
                  return (
                    <article key={row.run_id} className="rounded border border-border/70 bg-background/70 p-2">
                      <div className="mb-1 flex items-center gap-2">
                        <span className="truncate font-mono text-[10px] text-foreground" title={row.run_id}>
                          {row.run_id.slice(0, 18)}
                        </span>
                        <span className={cn("ml-auto font-mono text-[10px]", runStatusClass(row))}>
                          {runBoardStatus(row)}
                        </span>
                      </div>
                      <p className="font-mono text-[10px] text-muted-foreground">
                        {trimUiText(row.checkpoint?.note || row.error_detail || row.checkpoint?.stage || "No checkpoint note.", 88)}
                      </p>
                      <div className="mt-2 grid grid-cols-2 gap-2 font-mono text-[10px] text-muted-foreground">
                        <span>Deps: {Array.isArray(row.dependencies) ? row.dependencies.length : 0}</span>
                        <span>Blockers: {openBlockers.length}</span>
                        <span>Elapsed: {formatElapsed(runElapsedMs(row, nowMs))}</span>
                        <span>{isRunStalled(row, nowMs) ? "Stalled" : "Live"}</span>
                      </div>
                      {row.session_id && (
                        <p className="mt-2 truncate font-mono text-[10px] text-muted-foreground" title={row.session_id}>
                          Session {row.session_id}
                        </p>
                      )}
                      <div className="mt-2 flex flex-wrap gap-1.5">
                        {canRetryRun(row) && (
                          <button
                            type="button"
                            onClick={() => {
                              void resumeSingle(row.run_id)
                            }}
                            disabled={busyRunId === row.run_id}
                            className="rounded border border-neon-yellow/40 bg-neon-yellow/10 px-2 py-0.5 font-mono text-[10px] text-neon-yellow disabled:opacity-50"
                          >
                            {busyRunId === row.run_id ? "..." : "RETRY"}
                          </button>
                        )}
                        {row.session_id && (
                          <button
                            type="button"
                            onClick={() => openSession(row.session_id as string)}
                            className="rounded border border-border px-2 py-0.5 font-mono text-[10px] text-muted-foreground hover:text-foreground"
                          >
                            OPEN
                          </button>
                        )}
                        <button
                          type="button"
                          onClick={() => {
                            if (editingRunId === row.run_id) {
                              cancelEdit()
                            } else {
                              startEdit(row)
                            }
                          }}
                          className="rounded border border-border px-2 py-0.5 font-mono text-[10px] text-muted-foreground hover:text-neon-cyan"
                        >
                          {editingRunId === row.run_id ? "CLOSE" : "EDIT DAG"}
                        </button>
                        <button
                          type="button"
                          onClick={() => {
                            void copyRunId(row.run_id)
                          }}
                          className="rounded border border-border px-2 py-0.5 font-mono text-[10px] text-muted-foreground hover:text-foreground"
                        >
                          COPY
                        </button>
                      </div>
                    </article>
                  )
                })}
              </div>
            </section>
          ))}
        </div>
      ) : (
      <div className="overflow-hidden rounded-lg border border-border">
        <table className="w-full">
          <thead className="bg-card/40">
            <tr className="text-left">
              <th className="px-3 py-2 font-mono text-[10px] tracking-wider text-muted-foreground">RUN ID</th>
              <th className="px-3 py-2 font-mono text-[10px] tracking-wider text-muted-foreground">STATUS</th>
              <th className="px-3 py-2 font-mono text-[10px] tracking-wider text-muted-foreground">STAGE</th>
              <th className="px-3 py-2 font-mono text-[10px] tracking-wider text-muted-foreground">DEPS</th>
              <th className="px-3 py-2 font-mono text-[10px] tracking-wider text-muted-foreground">BLOCKERS</th>
              <th className="px-3 py-2 font-mono text-[10px] tracking-wider text-muted-foreground">SESSION</th>
              <th className="px-3 py-2 font-mono text-[10px] tracking-wider text-muted-foreground">UPDATED</th>
              <th className="px-3 py-2 font-mono text-[10px] tracking-wider text-muted-foreground">ACTIONS</th>
            </tr>
          </thead>
          <tbody>
            {loading && (
              <tr>
                <td colSpan={8} className="px-3 py-3 font-mono text-xs text-muted-foreground">
                  Loading runs...
                </td>
              </tr>
            )}
            {!loading && filteredRuns.length === 0 && (
              <tr>
                <td colSpan={8} className="px-3 py-3 font-mono text-xs text-muted-foreground">
                  No runs found for this filter.
                </td>
              </tr>
            )}
            {!loading &&
              pagedRuns.map((row) => (
                <Fragment key={row.run_id}>
                <tr className="border-t border-border/70">
                  <td className="px-3 py-2 font-mono text-xs text-foreground">{row.run_id.slice(0, 22)}</td>
                  <td
                    className={`px-3 py-2 font-mono text-xs ${
                      isRunBlocked(row)
                        ? "text-neon-yellow"
                        : row.status === "failed"
                        ? "text-neon-red"
                        : row.status === "completed"
                          ? "text-neon-green"
                          : "text-neon-cyan"
                    }`}
                  >
                    {isRunBlocked(row) ? "blocked" : row.status ?? "unknown"}
                  </td>
                  <td className="px-3 py-2 font-mono text-xs text-muted-foreground">
                    {row.checkpoint?.stage ?? "n/a"}
                  </td>
                  <td className="px-3 py-2 font-mono text-xs text-muted-foreground">
                    {Array.isArray(row.dependencies) ? row.dependencies.length : 0}
                  </td>
                  <td className="px-3 py-2 font-mono text-xs text-muted-foreground">
                    {
                      (Array.isArray(row.blockers)
                        ? row.blockers.filter((item) => String(item?.status ?? "open").toLowerCase() === "open")
                        : []
                      ).length
                    }
                  </td>
                  <td className="px-3 py-2 font-mono text-xs text-muted-foreground">
                    {row.session_id ? row.session_id.slice(0, 18) : "n/a"}
                  </td>
                  <td className="px-3 py-2 font-mono text-xs text-muted-foreground">
                    {row.updated_at ? new Date(row.updated_at).toLocaleTimeString() : "n/a"}
                    <div className="mt-0.5 text-[10px]">
                      elapsed {formatElapsed(runElapsedMs(row, nowMs))}
                      {isRunStalled(row, nowMs) ? " • stalled" : ""}
                    </div>
                  </td>
                  <td className="px-3 py-2">
                    <div className="flex items-center gap-2">
                      {canRetryRun(row) && (
                        <button
                          type="button"
                          onClick={() => {
                            void resumeSingle(row.run_id)
                          }}
                          disabled={busyRunId === row.run_id}
                          className="rounded border border-neon-yellow/40 bg-neon-yellow/10 px-2 py-0.5 font-mono text-[10px] text-neon-yellow disabled:opacity-50"
                        >
                          {busyRunId === row.run_id ? "..." : "RETRY"}
                        </button>
                      )}
                      <button
                        type="button"
                        onClick={() => {
                          void copyRunId(row.run_id)
                        }}
                        className="rounded border border-border px-2 py-0.5 font-mono text-[10px] text-muted-foreground hover:text-foreground"
                      >
                        COPY ID
                      </button>
                      <button
                        type="button"
                        onClick={() => {
                          void openArtifacts(row.run_id)
                        }}
                        className="rounded border border-border px-2 py-0.5 font-mono text-[10px] text-muted-foreground hover:text-neon-cyan"
                      >
                        ARTIFACTS
                      </button>
                      {row.session_id && (
                        <button
                          type="button"
                          onClick={() => openSession(row.session_id as string)}
                          className="rounded border border-border px-2 py-0.5 font-mono text-[10px] text-muted-foreground hover:text-foreground"
                        >
                          OPEN CHAT
                        </button>
                      )}
                      <button
                        type="button"
                        onClick={() => {
                          if (editingRunId === row.run_id) {
                            cancelEdit()
                          } else {
                            startEdit(row)
                          }
                        }}
                        className="rounded border border-border px-2 py-0.5 font-mono text-[10px] text-muted-foreground hover:text-neon-cyan"
                      >
                        {editingRunId === row.run_id ? "CLOSE DAG" : "EDIT DAG"}
                      </button>
                      <button
                        type="button"
                        onClick={() => {
                          void deleteSingle(row.run_id)
                        }}
                        disabled={busyRunId === row.run_id}
                        className="rounded border border-border px-2 py-0.5 font-mono text-[10px] text-muted-foreground hover:text-neon-red disabled:opacity-50"
                      >
                        DELETE
                      </button>
                    </div>
                  </td>
                </tr>
                {editingRunId === row.run_id && (
                  <tr className="border-t border-border/70 bg-background/40">
                    <td colSpan={8} className="px-3 py-3">
                      <div className="grid gap-3">
                        <div>
                          <p className="mb-1 font-mono text-[10px] text-muted-foreground">DEPENDENCIES (comma or newline separated run IDs)</p>
                          <textarea
                            value={depEditorText}
                            onChange={(e) => setDepEditorText(e.target.value)}
                            rows={2}
                            className="w-full rounded border border-border bg-background/60 px-2 py-1 font-mono text-xs text-foreground focus:outline-none"
                            placeholder="run-a, run-b"
                          />
                        </div>
                        <div>
                          <div className="mb-1 flex items-center gap-2">
                            <p className="font-mono text-[10px] text-muted-foreground">BLOCKERS</p>
                            <button
                              type="button"
                              onClick={addBlocker}
                              className="rounded border border-border px-2 py-0.5 font-mono text-[10px] text-muted-foreground hover:text-foreground"
                            >
                              ADD BLOCKER
                            </button>
                          </div>
                          <div className="grid gap-2">
                            {blockerEditor.map((item, idx) => (
                              <div key={`${item.blocker_id}-${idx}`} className="grid grid-cols-1 gap-2 rounded border border-border/70 p-2 md:grid-cols-[1fr_2fr_auto_auto_auto]">
                                <input
                                  type="text"
                                  value={item.code ?? ""}
                                  onChange={(e) => updateBlockerField(idx, "code", e.target.value)}
                                  placeholder="code"
                                  className="rounded border border-border bg-background/60 px-2 py-1 font-mono text-[10px] text-foreground focus:outline-none"
                                />
                                <input
                                  type="text"
                                  value={item.message ?? ""}
                                  onChange={(e) => updateBlockerField(idx, "message", e.target.value)}
                                  placeholder="message"
                                  className="rounded border border-border bg-background/60 px-2 py-1 font-mono text-[10px] text-foreground focus:outline-none"
                                />
                                <select
                                  value={String(item.severity ?? "medium")}
                                  onChange={(e) => updateBlockerField(idx, "severity", e.target.value)}
                                  className="rounded border border-border bg-background/60 px-2 py-1 font-mono text-[10px] text-foreground focus:outline-none"
                                >
                                  <option value="low">low</option>
                                  <option value="medium">medium</option>
                                  <option value="high">high</option>
                                </select>
                                <select
                                  value={String(item.status ?? "open")}
                                  onChange={(e) => updateBlockerField(idx, "status", e.target.value)}
                                  className="rounded border border-border bg-background/60 px-2 py-1 font-mono text-[10px] text-foreground focus:outline-none"
                                >
                                  <option value="open">open</option>
                                  <option value="resolved">resolved</option>
                                </select>
                                <button
                                  type="button"
                                  onClick={() => removeBlocker(idx)}
                                  className="rounded border border-border px-2 py-1 font-mono text-[10px] text-muted-foreground hover:text-neon-red"
                                >
                                  REMOVE
                                </button>
                              </div>
                            ))}
                            {blockerEditor.length === 0 && (
                              <p className="font-mono text-[10px] text-muted-foreground">No blockers configured.</p>
                            )}
                          </div>
                        </div>
                        <div className="flex items-center gap-2">
                          <button
                            type="button"
                            onClick={() => {
                              void saveEdit(row.run_id)
                            }}
                            disabled={busyRunId === row.run_id}
                            className="rounded border border-neon-cyan/40 bg-neon-cyan/10 px-2 py-1 font-mono text-[10px] text-neon-cyan disabled:opacity-50"
                          >
                            {busyRunId === row.run_id ? "SAVING..." : "SAVE DAG"}
                          </button>
                          <button
                            type="button"
                            onClick={cancelEdit}
                            className="rounded border border-border px-2 py-1 font-mono text-[10px] text-muted-foreground hover:text-foreground"
                          >
                            CANCEL
                          </button>
                        </div>
                      </div>
                    </td>
                  </tr>
                )}
                </Fragment>
              ))}
          </tbody>
        </table>
      </div>
      )}
      {layout === "board" && editingRun && (
        <div className="mt-3 rounded-lg border border-neon-cyan/30 bg-card/30 p-3">
          <div className="mb-2 flex items-center justify-between gap-2">
            <div>
              <h3 className="font-mono text-xs tracking-wider text-foreground">EDIT RUN DAG</h3>
              <p className="font-mono text-[10px] text-muted-foreground">
                {editingRun.run_id} {editingRun.session_id ? `| session ${editingRun.session_id}` : ""}
              </p>
            </div>
            <button
              type="button"
              onClick={cancelEdit}
              className="rounded border border-border px-2 py-1 font-mono text-[10px] text-muted-foreground hover:text-foreground"
            >
              CLOSE
            </button>
          </div>
          <div className="grid gap-3">
            <div>
              <p className="mb-1 font-mono text-[10px] text-muted-foreground">DEPENDENCIES (comma or newline separated run IDs)</p>
              <textarea
                value={depEditorText}
                onChange={(e) => setDepEditorText(e.target.value)}
                rows={2}
                className="w-full rounded border border-border bg-background/60 px-2 py-1 font-mono text-xs text-foreground focus:outline-none"
                placeholder="run-a, run-b"
              />
            </div>
            <div>
              <div className="mb-1 flex items-center gap-2">
                <p className="font-mono text-[10px] text-muted-foreground">BLOCKERS</p>
                <button
                  type="button"
                  onClick={addBlocker}
                  className="rounded border border-border px-2 py-0.5 font-mono text-[10px] text-muted-foreground hover:text-foreground"
                >
                  ADD BLOCKER
                </button>
              </div>
              <div className="grid gap-2">
                {blockerEditor.map((item, idx) => (
                  <div key={`${item.blocker_id}-${idx}`} className="grid grid-cols-1 gap-2 rounded border border-border/70 p-2 md:grid-cols-[1fr_2fr_auto_auto_auto]">
                    <input
                      type="text"
                      value={item.code ?? ""}
                      onChange={(e) => updateBlockerField(idx, "code", e.target.value)}
                      placeholder="code"
                      className="rounded border border-border bg-background/60 px-2 py-1 font-mono text-[10px] text-foreground focus:outline-none"
                    />
                    <input
                      type="text"
                      value={item.message ?? ""}
                      onChange={(e) => updateBlockerField(idx, "message", e.target.value)}
                      placeholder="message"
                      className="rounded border border-border bg-background/60 px-2 py-1 font-mono text-[10px] text-foreground focus:outline-none"
                    />
                    <select
                      value={String(item.severity ?? "medium")}
                      onChange={(e) => updateBlockerField(idx, "severity", e.target.value)}
                      className="rounded border border-border bg-background/60 px-2 py-1 font-mono text-[10px] text-foreground focus:outline-none"
                    >
                      <option value="low">low</option>
                      <option value="medium">medium</option>
                      <option value="high">high</option>
                    </select>
                    <select
                      value={String(item.status ?? "open")}
                      onChange={(e) => updateBlockerField(idx, "status", e.target.value)}
                      className="rounded border border-border bg-background/60 px-2 py-1 font-mono text-[10px] text-foreground focus:outline-none"
                    >
                      <option value="open">open</option>
                      <option value="resolved">resolved</option>
                    </select>
                    <button
                      type="button"
                      onClick={() => removeBlocker(idx)}
                      className="rounded border border-border px-2 py-1 font-mono text-[10px] text-muted-foreground hover:text-neon-red"
                    >
                      REMOVE
                    </button>
                  </div>
                ))}
                {blockerEditor.length === 0 && (
                  <p className="font-mono text-[10px] text-muted-foreground">No blockers configured.</p>
                )}
              </div>
            </div>
            <div className="flex items-center gap-2">
              <button
                type="button"
                onClick={() => {
                  void saveEdit(editingRun.run_id)
                }}
                disabled={busyRunId === editingRun.run_id}
                className="rounded border border-neon-cyan/40 bg-neon-cyan/10 px-2 py-1 font-mono text-[10px] text-neon-cyan disabled:opacity-50"
              >
                {busyRunId === editingRun.run_id ? "SAVING..." : "SAVE DAG"}
              </button>
              <button
                type="button"
                onClick={cancelEdit}
                className="rounded border border-border px-2 py-1 font-mono text-[10px] text-muted-foreground hover:text-foreground"
              >
                CANCEL
              </button>
            </div>
          </div>
        </div>
      )}
      <div className="mt-3 flex items-center justify-between gap-2">
        <p className="font-mono text-[10px] text-muted-foreground">
          {layout === "list" ? `Showing ${pagedRuns.length} of ${filteredRuns.length} run(s)` : `Board loaded with ${filteredRuns.length} run(s)`}
        </p>
        <div className={cn("flex items-center gap-2", layout !== "list" && "pointer-events-none opacity-40")}>
          <button
            type="button"
            onClick={() => setPage((p) => Math.max(1, p - 1))}
            disabled={page <= 1}
            className="rounded border border-border px-2 py-0.5 font-mono text-[10px] text-muted-foreground hover:text-foreground disabled:opacity-50"
          >
            PREV
          </button>
          <span className="font-mono text-[10px] text-muted-foreground">
            PAGE {page} / {totalPages}
          </span>
          <button
            type="button"
            onClick={() => setPage((p) => Math.min(totalPages, p + 1))}
            disabled={page >= totalPages}
            className="rounded border border-border px-2 py-0.5 font-mono text-[10px] text-muted-foreground hover:text-foreground disabled:opacity-50"
          >
            NEXT
          </button>
        </div>
      </div>
      {runDagOpen && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4">
          <div className="w-full max-w-3xl rounded-lg border border-neon-cyan/30 bg-card p-4 shadow-xl">
            <div className="mb-2 flex items-center justify-between">
              <h3 className="font-mono text-sm font-bold tracking-wider text-foreground">RUN DAG</h3>
              <button
                type="button"
                onClick={() => setRunDagOpen(false)}
                className="rounded border border-border px-2 py-1 font-mono text-[10px] text-muted-foreground hover:text-foreground"
              >
                CLOSE
              </button>
            </div>
            {runDagLoading && (
              <p className="font-mono text-xs text-muted-foreground">Loading DAG...</p>
            )}
            {!runDagLoading && runDagError && (
              <p className="font-mono text-xs text-neon-red">{runDagError}</p>
            )}
            {!runDagLoading && !runDagError && runDag && (
              <div className="grid gap-3">
                <div className="flex flex-wrap items-center gap-2">
                  <label className="font-mono text-[10px] text-muted-foreground">FOCUS RUN</label>
                  <select
                    value={runDagFocusId}
                    onChange={(e) => setRunDagFocusId(e.target.value)}
                    className="rounded border border-border bg-background/60 px-2 py-1 font-mono text-[10px] text-foreground focus:outline-none"
                  >
                    <option value="">(none)</option>
                    {(runDag.nodes ?? []).map((node) => (
                      <option key={node.id} value={node.id}>
                        {node.label || node.id}
                      </option>
                    ))}
                  </select>
                  <label className="inline-flex items-center gap-1 font-mono text-[10px] text-muted-foreground">
                    <input
                      type="checkbox"
                      checked={runDagFocusOnly}
                      onChange={(e) => setRunDagFocusOnly(e.target.checked)}
                    />
                    focus only
                  </label>
                  <label className="inline-flex items-center gap-1 font-mono text-[10px] text-muted-foreground">
                    depth
                    <select
                      value={runDagFocusDepth}
                      onChange={(e) => setRunDagFocusDepth(Number(e.target.value) as 1 | 2)}
                      className="rounded border border-border bg-background/60 px-1 py-0.5 font-mono text-[10px] text-foreground focus:outline-none"
                    >
                      <option value={1}>1-hop</option>
                      <option value={2}>2-hop</option>
                    </select>
                  </label>
                </div>
                <div className="flex flex-wrap items-center gap-3 font-mono text-[10px] text-muted-foreground">
                  <span>NODES: {Array.isArray(focusedRunDag?.nodes) ? focusedRunDag.nodes.length : 0}</span>
                  <span>EDGES: {Array.isArray(focusedRunDag?.edges) ? focusedRunDag.edges.length : 0}</span>
                </div>
                <div className="grid max-h-[60vh] grid-cols-1 gap-3 overflow-auto md:grid-cols-2">
                  <div className="rounded border border-border/70 p-2">
                    <p className="mb-1 font-mono text-[10px] text-muted-foreground">NODES</p>
                    <div className="grid gap-1">
                      {(focusedRunDag?.nodes ?? []).map((node) => (
                        <div key={node.id} className="rounded border border-border/60 bg-background/30 px-2 py-1">
                          <div className="flex items-center gap-2">
                            <span className="truncate font-mono text-[10px] text-foreground" title={node.label || node.id}>
                              {trimUiText(node.label || node.id)}
                            </span>
                            <span
                              className={`ml-auto font-mono text-[10px] ${
                                node.status === "blocked"
                                  ? "text-neon-yellow"
                                  : node.status === "failed"
                                    ? "text-neon-red"
                                    : node.status === "completed"
                                      ? "text-neon-green"
                                      : "text-neon-cyan"
                              }`}
                            >
                              {node.status || "unknown"}
                            </span>
                          </div>
                          <p className="truncate font-mono text-[10px] text-muted-foreground" title={node.id}>
                            {trimUiText(node.id)}
                          </p>
                        </div>
                      ))}
                    </div>
                  </div>
                  <div className="rounded border border-border/70 p-2">
                    <p className="mb-1 font-mono text-[10px] text-muted-foreground">EDGES</p>
                    <div className="grid gap-1">
                      {(focusedRunDag?.edges ?? []).map((edge, idx) => (
                        <div key={`${edge.from}-${edge.to}-${idx}`} className="rounded border border-border/60 bg-background/30 px-2 py-1 font-mono text-[10px] text-foreground">
                          <span className="text-neon-cyan">{edge.from.slice(0, 18)}</span>
                          <span className="px-1 text-muted-foreground">
                            {edge.type === "blocked_by" ? "-[blocked_by]->" : "-[depends_on]->"}
                          </span>
                          <span className="text-neon-cyan">{edge.to.slice(0, 18)}</span>
                        </div>
                      ))}
                    </div>
                  </div>
                </div>
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  )
}
