"use client"

import { useCallback, useEffect, useRef, useState } from "react"
import type { ArtifactRun, ArtifactStep } from "@/lib/types"
import {
  deleteAllArtifacts,
  deleteArtifact,
  exportAllArtifacts,
  exportArtifact,
  fetchArtifact,
  fetchArtifacts,
  fetchArtifactsEncryptionStatus,
  getAdminPasswordStatus,
} from "@/lib/api"
import { ChevronDown, ChevronRight, Download, AlertTriangle, Trash2 } from "lucide-react"
import { cn, formatUsdAdaptive } from "@/lib/utils"

const displayMode = (mode: string) => {
  const normalized = mode === "retrieval" ? "web" : mode
  return normalized ? `${normalized.charAt(0).toUpperCase()}${normalized.slice(1)}` : normalized
}
const trimArtifactTitle = (query: string, max = 110) => {
  const normalized = query.replace(/\s+/g, " ").trim()
  if (normalized.length <= max) return normalized
  return `${normalized.slice(0, Math.max(1, max - 1)).trimEnd()}...`
}

function StepCard({ step, index }: { step: ArtifactStep; index: number }) {
  const [expanded, setExpanded] = useState(false)

  const stepColors: Record<string, string> = {
    "User Query": "border-neon-cyan/40",
    "Guardian Pre-flight": "border-neon-yellow/40",
    Draft: "border-neon-magenta/40",
    Critique: "border-neon-green/40",
    Refine: "border-neon-cyan/40",
    "Guardian Post-output": "border-neon-yellow/40",
    Citations: "border-neon-cyan/40",
    "Final Response": "border-neon-green/40",
  }

  return (
    <div className="relative flex gap-3">
      <div className="flex flex-col items-center">
        <div className="flex h-6 w-6 shrink-0 items-center justify-center rounded-full border border-border bg-card font-mono text-[10px] text-muted-foreground">
          {index + 1}
        </div>
        <div className="w-px flex-1 bg-border" />
      </div>
      <div className={cn("mb-3 flex-1 rounded-lg border bg-card/50 transition-all", stepColors[step.name] || "border-border")}>
        <button type="button" onClick={() => setExpanded(!expanded)} className="flex w-full items-center justify-between px-3 py-2">
          <div className="flex items-center gap-2">
            <span className="font-mono text-xs font-medium text-foreground">{step.name}</span>
            {step.duration_ms != null && step.duration_ms > 0 && (
              <span className="font-mono text-[10px] text-muted-foreground">{step.duration_ms}ms</span>
            )}
            {step.cost != null && step.cost > 0 && (
              <span className="font-mono text-[10px] text-neon-cyan">
                ${formatUsdAdaptive(step.cost, { minDecimals: 0, maxDecimals: 5, fallback: "--" })}
              </span>
            )}
          </div>
          {expanded ? <ChevronDown className="h-3 w-3 text-muted-foreground" /> : <ChevronRight className="h-3 w-3 text-muted-foreground" />}
        </button>
        {expanded && (
          <div className="border-t border-border px-3 py-2">
            <pre className="overflow-auto whitespace-pre-wrap break-all font-mono text-xs text-muted-foreground">{step.content}</pre>
            {step.metadata && (
              <div className="mt-2 flex flex-wrap gap-1">
                {Object.entries(step.metadata).map(([k, v]) => (
                  <span key={k} className="rounded-full border border-border px-2 py-0.5 font-mono text-[10px] text-muted-foreground">
                    {k}: {String(v)}
                  </span>
                ))}
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  )
}

export function ArtifactsView() {
  const [runs, setRuns] = useState<ArtifactRun[]>([])
  const [expandedRun, setExpandedRun] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [exportStatus, setExportStatus] = useState<string | null>(null)
  const [exportBusy, setExportBusy] = useState(false)
  const [deleteBusy, setDeleteBusy] = useState(false)
  const [encryptionStatus, setEncryptionStatus] = useState<string | null>(null)
  const [exportAuthOpen, setExportAuthOpen] = useState(false)
  const [exportAuthPassword, setExportAuthPassword] = useState("")
  const [authPromptPurpose, setAuthPromptPurpose] = useState<"export" | "delete">("export")
  const [deleteCandidate, setDeleteCandidate] = useState<ArtifactRun | null>(null)
  const [deleteOlderOpen, setDeleteOlderOpen] = useState(false)
  const [deleteOlderDays, setDeleteOlderDays] = useState("30")
  const exportAuthResolverRef = useRef<((value: string | null) => void) | null>(null)

  const load = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const data = await fetchArtifacts()
      setRuns(data)
      try {
        const enc = await fetchArtifactsEncryptionStatus()
        setEncryptionStatus(
          enc.enabled
            ? `Encryption ON (${enc.encrypted_files}/${enc.sampled_files} sampled files encrypted)`
            : "Encryption OFF (at-rest artifact encryption disabled)"
        )
      } catch {
        setEncryptionStatus(null)
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load artifacts")
      setRuns([])
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    load()
  }, [load])

  const toggleExpand = async (runId: string) => {
    if (expandedRun === runId) {
      setExpandedRun(null)
      return
    }
    setExpandedRun(runId)
    const target = runs.find((r) => r.id === runId)
    if (!target || (target.steps && target.steps.length > 0)) return
    try {
      const full = await fetchArtifact(runId)
      setRuns((prev) => prev.map((r) => (r.id === runId ? full : r)))
    } catch {
      // keep summary view only
    }
  }

  const handleExport = (run: ArtifactRun) => {
    void doExportSingle(run)
  }

  const promptActionPassword = (purpose: "export" | "delete") =>
    new Promise<string | null>((resolve) => {
      exportAuthResolverRef.current = resolve
      setAuthPromptPurpose(purpose)
      setExportAuthPassword("")
      setExportAuthOpen(true)
    })

  const closeExportAuthPrompt = (value: string | null) => {
    setExportAuthOpen(false)
    const resolve = exportAuthResolverRef.current
    exportAuthResolverRef.current = null
    if (resolve) resolve(value)
  }

  const maybeGetActionPassword = async (purpose: "export" | "delete"): Promise<string | undefined> => {
    const status = await getAdminPasswordStatus()
    if (!status.configured) return undefined
    const entered = await promptActionPassword(purpose)
    if (!entered) throw new Error("Action cancelled")
    return entered
  }

  const downloadJson = (filename: string, payload: unknown) => {
    const blob = new Blob([JSON.stringify(payload, null, 2)], { type: "application/json" })
    const url = URL.createObjectURL(blob)
    const a = document.createElement("a")
    a.href = url
    a.download = filename
    a.click()
    URL.revokeObjectURL(url)
  }

  const doExportSingle = async (run: ArtifactRun) => {
    if (exportBusy) return
    setExportBusy(true)
    setExportStatus(null)
    try {
      const adminPassword = await maybeGetActionPassword("export")
      const payload = await exportArtifact(run.id, adminPassword)
      downloadJson(`artifact-${run.id}.json`, payload)
      setExportStatus(`Exported artifact ${run.id}.`)
    } catch (err) {
      const msg = err instanceof Error ? err.message : "failed"
      if (msg === "Action cancelled") setExportStatus("Artifact export cancelled.")
      else setExportStatus(`Artifact export failed (${msg}).`)
    } finally {
      setExportBusy(false)
    }
  }

  const doExportAll = async () => {
    if (exportBusy) return
    setExportBusy(true)
    setExportStatus(null)
    try {
      const adminPassword = await maybeGetActionPassword("export")
      const rows = await exportAllArtifacts(adminPassword, 200)
      downloadJson("artifacts-all.json", {
        exported_at: new Date().toISOString(),
        count: rows.length,
        artifacts: rows,
      })
      setExportStatus(`Exported ${rows.length} artifact(s).`)
    } catch (err) {
      const msg = err instanceof Error ? err.message : "failed"
      if (msg === "Action cancelled") setExportStatus("Artifact export cancelled.")
      else setExportStatus(`Artifact export failed (${msg}).`)
    } finally {
      setExportBusy(false)
    }
  }

  const doDeleteSingle = async (run: ArtifactRun) => {
    if (deleteBusy || exportBusy) return
    setDeleteBusy(true)
    setExportStatus(null)
    try {
      const adminPassword = await maybeGetActionPassword("delete")
      await deleteArtifact(run.id, adminPassword)
      setRuns((prev) => prev.filter((item) => item.id !== run.id))
      if (expandedRun === run.id) setExpandedRun(null)
      setExportStatus(`Deleted artifact ${run.id}.`)
    } catch (err) {
      const msg = err instanceof Error ? err.message : "failed"
      if (msg === "Action cancelled") setExportStatus("Artifact delete cancelled.")
      else setExportStatus(`Artifact delete failed (${msg}).`)
    } finally {
      setDeleteBusy(false)
      setDeleteCandidate(null)
    }
  }

  const doDeleteOlder = async () => {
    if (deleteBusy || exportBusy) return
    const parsed = Number.parseInt(deleteOlderDays.trim(), 10)
    const olderThanDays = Number.isFinite(parsed) && parsed > 0 ? parsed : 0
    setDeleteBusy(true)
    setExportStatus(null)
    try {
      const adminPassword = await maybeGetActionPassword("delete")
      const result = await deleteAllArtifacts(
        adminPassword,
        olderThanDays > 0 ? olderThanDays : undefined,
        5000
      )
      await load()
      if (olderThanDays > 0) {
        setExportStatus(`Deleted ${result.deleted} artifact(s) older than ${olderThanDays} day(s).`)
      } else {
        setExportStatus(`Deleted ${result.deleted} artifact(s).`)
      }
    } catch (err) {
      const msg = err instanceof Error ? err.message : "failed"
      if (msg === "Action cancelled") setExportStatus("Artifact cleanup cancelled.")
      else setExportStatus(`Artifact cleanup failed (${msg}).`)
    } finally {
      setDeleteBusy(false)
      setDeleteOlderOpen(false)
    }
  }

  return (
    <div className="p-6">
      <div className="mb-6">
        <h1 className="font-mono text-lg font-bold tracking-wider text-foreground">Artifacts</h1>
        <p className="text-sm text-muted-foreground">Pipeline run history and trace explorer</p>
        <div className="mt-2 flex items-center gap-2">
          <button
            type="button"
            onClick={() => void doExportAll()}
            disabled={exportBusy || deleteBusy}
            className="rounded border border-neon-cyan/40 bg-neon-cyan/10 px-2 py-1 font-mono text-[10px] text-neon-cyan hover:bg-neon-cyan/20 disabled:opacity-50"
          >
            Export All JSON
          </button>
          <button
            type="button"
            onClick={() => setDeleteOlderOpen(true)}
            disabled={exportBusy || deleteBusy}
            className="rounded border border-neon-red/40 bg-neon-red/10 px-2 py-1 font-mono text-[10px] text-neon-red hover:bg-neon-red/20 disabled:opacity-50"
          >
            Delete Older...
          </button>
          {encryptionStatus && (
            <span className="font-mono text-[10px] text-muted-foreground">{encryptionStatus}</span>
          )}
        </div>
        {exportStatus && (
          <p className="mt-1 font-mono text-[10px] text-muted-foreground">{exportStatus}</p>
        )}
      </div>

      {loading && <div className="font-mono text-sm text-muted-foreground">Loading artifacts...</div>}
      {error && (
        <div className="mb-4 font-mono text-xs text-neon-yellow">
          Artifact service is unavailable right now ({error}).
        </div>
      )}
      {!loading && runs.length === 0 && <div className="font-mono text-sm text-muted-foreground">No artifacts found.</div>}

      <div className="space-y-3">
        {runs.map((run) => (
          <div key={run.id} className="glass-card rounded-lg">
            <button type="button" onClick={() => void toggleExpand(run.id)} className="flex w-full items-center justify-between px-4 py-3">
              <div className="flex flex-1 items-center gap-3">
                {expandedRun === run.id ? (
                  <ChevronDown className="h-4 w-4 shrink-0 text-muted-foreground" />
                ) : (
                  <ChevronRight className="h-4 w-4 shrink-0 text-muted-foreground" />
                )}
                <div className="min-w-0 text-left">
                  <p className="max-w-full truncate text-sm text-foreground">{trimArtifactTitle(run.query)}</p>
                  <div className="mt-1 flex flex-wrap gap-2">
                    <span className="font-mono text-[10px] text-muted-foreground">{new Date(run.date).toLocaleString()}</span>
                    <span className="rounded-full border border-neon-cyan/30 px-2 py-0.5 font-mono text-[10px] text-neon-cyan">
                      {displayMode(run.mode)}
                    </span>
                    <span className="font-mono text-[10px] text-neon-magenta">
                      ${formatUsdAdaptive(run.cost, { minDecimals: 0, maxDecimals: 5, fallback: "--" })}
                    </span>
                    {run.guardian_flags?.map((f) => (
                      <span
                        key={f}
                        className="flex max-w-full items-center gap-1 truncate rounded-full border border-neon-yellow/30 px-2 py-0.5 font-mono text-[10px] text-neon-yellow"
                        title={f}
                      >
                        <AlertTriangle className="h-2.5 w-2.5" />
                        {f}
                      </span>
                    ))}
                  </div>
                </div>
              </div>
            </button>

            {expandedRun === run.id && run.steps && (
              <div className="border-t border-border px-4 py-4">
                <div className="mb-3 flex items-center justify-between">
                  <span className="font-mono text-[10px] tracking-wider text-muted-foreground">Pipeline Trace</span>
                  <button
                    type="button"
                    onClick={() => handleExport(run)}
                    disabled={exportBusy || deleteBusy}
                    className="flex items-center gap-1 rounded-md border border-border px-2 py-1 font-mono text-[10px] text-muted-foreground transition-colors hover:border-neon-cyan/30 hover:text-neon-cyan"
                  >
                    <Download className="h-3 w-3" />
                    Export JSON
                  </button>
                  <button
                    type="button"
                    onClick={() => setDeleteCandidate(run)}
                    disabled={exportBusy || deleteBusy}
                    className="flex items-center gap-1 rounded-md border border-neon-red/40 bg-neon-red/10 px-2 py-1 font-mono text-[10px] text-neon-red transition-colors hover:bg-neon-red/20 disabled:opacity-50"
                  >
                    <Trash2 className="h-3 w-3" />
                    Delete
                  </button>
                </div>

                <div className="mb-4">
                  <span className="font-mono text-[10px] text-muted-foreground">Cost Breakdown</span>
                  <div className="mt-1 flex items-end gap-1">
                    {run.steps
                      .filter((s) => (s.cost ?? 0) > 0)
                      .map((s) => (
                        <div key={s.name} className="flex flex-col items-center">
                          <div
                            className="w-8 rounded-t bg-neon-cyan/60"
                            style={{ height: `${Math.max(((s.cost ?? 0) / Math.max(run.cost, 0.0001)) * 60, 4)}px` }}
                            title={`${s.name}: $${formatUsdAdaptive(s.cost, { minDecimals: 0, maxDecimals: 5, fallback: "--" })}`}
                          />
                          <span className="mt-1 w-12 truncate text-center font-mono text-[8px] text-muted-foreground">{s.name.split(" ")[0]}</span>
                        </div>
                      ))}
                  </div>
                </div>

                <div>
                  {run.steps.map((step, i) => (
                    <StepCard key={`${run.id}-step-${step.name}-${i}`} step={step} index={i} />
                  ))}
                </div>
              </div>
            )}
          </div>
        ))}
      </div>

      {exportAuthOpen && (
        <div className="fixed inset-0 z-[70] flex items-center justify-center bg-black/40 p-4">
          <div className="w-full max-w-md rounded-lg border border-neon-yellow/40 bg-card p-4 shadow-xl">
            <h3 className="font-mono text-sm font-bold tracking-wider text-foreground">
              ADMIN PASSWORD REQUIRED
            </h3>
            <p className="mt-1 font-mono text-xs text-muted-foreground">
              {authPromptPurpose === "delete"
                ? "Enter admin password to authorize artifact deletion."
                : "Enter admin password to authorize artifact export."}
            </p>
            <input
              type="password"
              value={exportAuthPassword}
              onChange={(e) => setExportAuthPassword(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") closeExportAuthPrompt(exportAuthPassword.trim() || null)
                if (e.key === "Escape") closeExportAuthPrompt(null)
              }}
              className="mt-3 w-full rounded border border-border bg-background/60 px-2 py-1.5 font-mono text-xs text-foreground placeholder:text-muted-foreground/50 focus:outline-none"
              placeholder="Admin password"
              autoFocus
            />
            <div className="mt-3 flex justify-end gap-2">
              <button
                type="button"
                onClick={() => closeExportAuthPrompt(null)}
                className="rounded border border-border px-2.5 py-1.5 font-mono text-[10px] text-muted-foreground hover:text-foreground"
              >
                CANCEL
              </button>
              <button
                type="button"
                onClick={() => closeExportAuthPrompt(exportAuthPassword.trim() || null)}
                className="rounded border border-neon-yellow/40 bg-neon-yellow/10 px-2.5 py-1.5 font-mono text-[10px] text-neon-yellow hover:bg-neon-yellow/20"
              >
                CONTINUE
              </button>
            </div>
          </div>
        </div>
      )}

      {deleteCandidate && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4">
          <div className="w-full max-w-md rounded-lg border border-neon-red/40 bg-card p-4 shadow-xl">
            <h3 className="font-mono text-sm font-bold tracking-wider text-foreground">Delete Artifact</h3>
            <p className="mt-1 font-mono text-xs text-muted-foreground">
              Delete this artifact permanently?
            </p>
            <p className="mt-2 truncate font-mono text-[10px] text-neon-red" title={deleteCandidate.id}>
              {deleteCandidate.id}
            </p>
            <div className="mt-3 flex justify-end gap-2">
              <button
                type="button"
                onClick={() => setDeleteCandidate(null)}
                disabled={deleteBusy}
                className="rounded border border-border px-2.5 py-1.5 font-mono text-[10px] text-muted-foreground hover:text-foreground disabled:opacity-50"
              >
                CANCEL
              </button>
              <button
                type="button"
                onClick={() => void doDeleteSingle(deleteCandidate)}
                disabled={deleteBusy}
                className="rounded border border-neon-red/40 bg-neon-red/10 px-2.5 py-1.5 font-mono text-[10px] text-neon-red hover:bg-neon-red/20 disabled:opacity-50"
              >
                {deleteBusy ? "Deleting..." : "Delete"}
              </button>
            </div>
          </div>
        </div>
      )}

      {deleteOlderOpen && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4">
          <div className="w-full max-w-md rounded-lg border border-neon-red/40 bg-card p-4 shadow-xl">
            <h3 className="font-mono text-sm font-bold tracking-wider text-foreground">Delete Old Artifacts</h3>
            <p className="mt-1 font-mono text-xs text-muted-foreground">
              Delete artifacts older than this many days.
            </p>
            <input
              type="number"
              min={0}
              value={deleteOlderDays}
              onChange={(e) => setDeleteOlderDays(e.target.value)}
              className="mt-3 w-full rounded border border-border bg-background/60 px-2 py-1.5 font-mono text-xs text-foreground placeholder:text-muted-foreground/50 focus:outline-none"
            />
            <p className="mt-2 font-mono text-[10px] text-muted-foreground">
              Use <span className="text-foreground">0</span> to delete all artifacts.
            </p>
            <div className="mt-3 flex justify-end gap-2">
              <button
                type="button"
                onClick={() => setDeleteOlderOpen(false)}
                disabled={deleteBusy}
                className="rounded border border-border px-2.5 py-1.5 font-mono text-[10px] text-muted-foreground hover:text-foreground disabled:opacity-50"
              >
                CANCEL
              </button>
              <button
                type="button"
                onClick={() => void doDeleteOlder()}
                disabled={deleteBusy}
                className="rounded border border-neon-red/40 bg-neon-red/10 px-2.5 py-1.5 font-mono text-[10px] text-neon-red hover:bg-neon-red/20 disabled:opacity-50"
              >
                {deleteBusy ? "Deleting..." : "Delete Older"}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
