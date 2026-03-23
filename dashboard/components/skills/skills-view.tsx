"use client"

import { Fragment, useCallback, useEffect, useRef, useState, type ChangeEvent } from "react"
import type { Mode, Skill, SkillCatalogEntry } from "@/lib/types"
import {
  deleteSkill,
  exportSkill,
  fetchSkillCatalog,
  fetchSkills,
  importSkillBundle,
  runSkillGovernanceAnalysis,
  runInstalledSkill,
  saveSkillDraft,
  setSkillEnabled,
  testSkillDraft,
  type SkillGovernanceReport,
  validateSkillDraft,
} from "@/lib/api"
import { cn, statusMessageClass } from "@/lib/utils"
import { BadgeCheck, ChevronDown, ChevronRight, Download, Shield, TestTube2, Trash2 } from "lucide-react"

const riskConfig = {
  low: {
    color: "text-neon-green border-neon-green/40",
    glow: "shadow-[0_0_4px_rgba(0,255,136,0.3)]",
  },
  medium: {
    color: "text-neon-yellow border-neon-yellow/40",
    glow: "shadow-[0_0_4px_rgba(255,170,0,0.3)]",
  },
  high: {
    color: "text-neon-red border-neon-red/40",
    glow: "shadow-[0_0_4px_rgba(255,51,51,0.3)]",
  },
}

const starterSkillYaml = `name: repo_health_check
description: Run lightweight repository health checks and summarize outcomes.
risk_level: low
manifest:
  purpose: Validate baseline project health before larger runs.
  tools: [system_info]
  data_scope: [repo_metadata, test_outputs]
  permissions: [read_repo]
  approval_policy: draft_only
  rate_limits:
    actions_per_hour: 30
  budgets:
    usd_cap: 1.0
  kill_switch:
    enabled: true
  audit_sink: server.audit
  failure_mode: fail_closed
steps:
  - id: smoke
    tool: system_info
    args: {}
`

export function SkillsView() {
  const [skills, setSkills] = useState<Skill[]>([])
  const [expandedSkill, setExpandedSkill] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [draftText, setDraftText] = useState("")
  const [draftStatus, setDraftStatus] = useState<string | null>(null)
  const [draftErrors, setDraftErrors] = useState<string[]>([])
  const [draftValid, setDraftValid] = useState(false)
  const [draftBusy, setDraftBusy] = useState(false)
  const [draftName, setDraftName] = useState("")
  const [draftTestStatus, setDraftTestStatus] = useState<string | null>(null)
  const [deleteStatus, setDeleteStatus] = useState<string | null>(null)
  const [deleteCandidate, setDeleteCandidate] = useState<Skill | null>(null)
  const [deleteBusy, setDeleteBusy] = useState(false)
  const [runInputBySkill, setRunInputBySkill] = useState<Record<string, string>>({})
  const [runModeBySkill, setRunModeBySkill] = useState<Record<string, Mode>>({})
  const [runStatusBySkill, setRunStatusBySkill] = useState<Record<string, string>>({})
  const [runOutputBySkill, setRunOutputBySkill] = useState<Record<string, string>>({})
  const [runBusyBySkill, setRunBusyBySkill] = useState<Record<string, boolean>>({})
  const [exportStatusBySkill, setExportStatusBySkill] = useState<Record<string, string>>({})
  const [exportBusyBySkill, setExportBusyBySkill] = useState<Record<string, boolean>>({})
  const [authorMenuOpen, setAuthorMenuOpen] = useState(false)
  const [importMenuOpen, setImportMenuOpen] = useState(false)
  const [importStatus, setImportStatus] = useState<string | null>(null)
  const [importBusy, setImportBusy] = useState(false)
  const [catalogStatus, setCatalogStatus] = useState<string | null>(null)
  const [catalogEntries, setCatalogEntries] = useState<SkillCatalogEntry[]>([])
  const [catalogLoading, setCatalogLoading] = useState(true)
  const [catalogBusyById, setCatalogBusyById] = useState<Record<string, boolean>>({})
  const [governanceStatus, setGovernanceStatus] = useState<string | null>(null)
  const [governanceRunning, setGovernanceRunning] = useState(false)
  const [governanceIncludeDisabled, setGovernanceIncludeDisabled] = useState(false)
  const [governanceReport, setGovernanceReport] = useState<SkillGovernanceReport | null>(null)
  const [governanceModalOpen, setGovernanceModalOpen] = useState(false)
  const [governanceActionStatus, setGovernanceActionStatus] = useState<string | null>(null)
  const [expandedGovernanceRows, setExpandedGovernanceRows] = useState<Record<string, boolean>>({})
  const fileInputRef = useRef<HTMLInputElement | null>(null)
  const importFileInputRef = useRef<HTMLInputElement | null>(null)
  const authorMenuRef = useRef<HTMLDivElement | null>(null)
  const importMenuRef = useRef<HTMLDivElement | null>(null)
  const [exportCandidate, setExportCandidate] = useState<Skill | null>(null)
  const [exportConfirmBusy, setExportConfirmBusy] = useState(false)

  const load = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const rows = await fetchSkills()
      setSkills(rows)
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load skills")
      setSkills([])
    } finally {
      setLoading(false)
    }
  }, [])

  const loadCatalog = useCallback(async () => {
    setCatalogLoading(true)
    try {
      const rows = await fetchSkillCatalog()
      setCatalogEntries(rows)
    } catch (err) {
      const msg = err instanceof Error ? err.message : "Failed to load catalog"
      setCatalogStatus(`Catalog unavailable (${msg}).`)
      setCatalogEntries([])
    } finally {
      setCatalogLoading(false)
    }
  }, [])

  useEffect(() => {
    load()
    loadCatalog()
  }, [load, loadCatalog])

  useEffect(() => {
    const onDocClick = (event: MouseEvent) => {
      const target = event.target as Node | null
      if (authorMenuRef.current && target && !authorMenuRef.current.contains(target)) {
        setAuthorMenuOpen(false)
      }
      if (importMenuRef.current && target && !importMenuRef.current.contains(target)) {
        setImportMenuOpen(false)
      }
    }
    document.addEventListener("mousedown", onDocClick)
    return () => document.removeEventListener("mousedown", onDocClick)
  }, [])

  const toggleEnabled = async (skill: Skill) => {
    const next = !skill.enabled
    setSkills((prev) => prev.map((s) => (s.id === skill.id ? { ...s, enabled: next } : s)))
    try {
      await setSkillEnabled(skill.name, next)
    } catch {
      setSkills((prev) => prev.map((s) => (s.id === skill.id ? { ...s, enabled: !next } : s)))
    }
  }

  const handleValidateDraft = async () => {
    if (draftBusy) return
    setDraftBusy(true)
    setDraftStatus(null)
    setDraftErrors([])
    try {
      const result = await validateSkillDraft(draftText)
      setDraftValid(result.valid)
      setDraftErrors(result.errors)
      setDraftName(result.name)
      setDraftStatus(result.valid ? `Draft valid: ${result.name || "unnamed skill"}` : "Draft has validation errors.")
    } catch (err) {
      const msg = err instanceof Error ? err.message : "failed"
      setDraftValid(false)
      setDraftName("")
      setDraftStatus(`Validation failed (${msg}).`)
    } finally {
      setDraftBusy(false)
    }
  }

  const handleSaveDraft = async () => {
    if (draftBusy) return
    setDraftBusy(true)
    setDraftStatus(null)
    setDraftErrors([])
    try {
      const result = await saveSkillDraft(draftText)
      setDraftValid(true)
      setDraftName(result.name)
      setDraftStatus(`Saved skill: ${result.name} (default state: disabled)`)
      setAuthorMenuOpen(false)
      await load()
    } catch (err) {
      const msg = err instanceof Error ? err.message : "failed"
      setDraftValid(false)
      setDraftStatus(`Save failed (${msg}).`)
    } finally {
      setDraftBusy(false)
    }
  }

  const handleTestDraft = async () => {
    if (draftBusy) return
    setDraftBusy(true)
    setDraftTestStatus(null)
    try {
      const result = await testSkillDraft(draftText)
      if (!result.validation.valid) {
        setDraftTestStatus(
          `Draft test blocked by validation: ${result.validation.errors.join("; ") || "unknown validation error"}`
        )
        return
      }
      if (result.run) {
        setDraftTestStatus(
          `Draft test OK: ${result.run.skill_name} (${result.run.steps_executed} step(s), $${result.run.total_cost.toFixed(4)})`
        )
      } else {
        setDraftTestStatus("Draft test finished with no run output.")
      }
      setAuthorMenuOpen(false)
    } catch (err) {
      const msg = err instanceof Error ? err.message : "failed"
      setDraftTestStatus(`Draft test failed (${msg}).`)
    } finally {
      setDraftBusy(false)
    }
  }

  const handlePickFile = () => {
    setAuthorMenuOpen(false)
    fileInputRef.current?.click()
  }

  const handlePickImportFile = () => {
    importFileInputRef.current?.click()
  }

  const handleFileSelected = async (event: ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0]
    if (!file) return
    try {
      const text = await file.text()
      setDraftText(text)
      setDraftStatus(`Loaded draft from file: ${file.name}`)
      setDraftErrors([])
      setDraftValid(false)
      setDraftName("")
      setDraftTestStatus(null)
    } catch {
      setDraftStatus(`Could not read file: ${file.name}`)
    } finally {
      event.target.value = ""
    }
  }

  const handleImportFileSelected = async (event: ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0]
    if (!file) return
    if (importBusy) {
      event.target.value = ""
      return
    }
    setImportBusy(true)
    setImportStatus(null)
    try {
      const text = await file.text()
      const bundle = JSON.parse(text) as unknown
      const result = await importSkillBundle(bundle, false)
      if (result.imported) {
        setImportStatus(`Imported skill: ${result.name} (disabled by default)`)
      } else {
        setImportStatus("Import completed with no changes.")
      }
      await load()
      setImportMenuOpen(false)
    } catch (err) {
      const msg = err instanceof Error ? err.message : "failed"
      setImportStatus(`Import failed (${msg}).`)
    } finally {
      setImportBusy(false)
      event.target.value = ""
    }
  }

  const handleDeleteSkill = async (skill: Skill) => {
    setDeleteCandidate(skill)
  }

  const runGovernance = async () => {
    if (governanceRunning) return
    setGovernanceRunning(true)
    setGovernanceStatus("Running governance analysis...")
    try {
      const report = await runSkillGovernanceAnalysis({
        includeDisabled: governanceIncludeDisabled,
        limit: 6,
      })
      setGovernanceReport(report)
      setGovernanceStatus(
        `Governance complete: ${report.summary.skills_analyzed} skills, ${report.summary.merge_candidates} merge candidate(s), ${report.summary.crossover_candidates} crossover candidate(s).`
      )
    } catch (err) {
      const msg = err instanceof Error ? err.message : "failed"
      setGovernanceStatus(`Governance analysis failed (${msg}).`)
    } finally {
      setGovernanceRunning(false)
    }
  }

  const copyGovernanceReport = async () => {
    if (!governanceReport) return
    try {
      await navigator.clipboard.writeText(JSON.stringify(governanceReport, null, 2))
      setGovernanceActionStatus("Governance JSON copied.")
    } catch {
      setGovernanceActionStatus("Copy failed.")
    }
  }

  const exportGovernanceReport = () => {
    if (!governanceReport) return
    const payload = JSON.stringify(governanceReport, null, 2)
    const blob = new Blob([payload], { type: "application/json" })
    const url = URL.createObjectURL(blob)
    const a = document.createElement("a")
    a.href = url
    a.download = `skills-governance-${new Date().toISOString().replace(/[:.]/g, "-")}.json`
    document.body.appendChild(a)
    a.click()
    document.body.removeChild(a)
    URL.revokeObjectURL(url)
    setGovernanceActionStatus("Governance JSON exported.")
  }

  const toggleGovernanceRow = (rowId: string) => {
    setExpandedGovernanceRows((prev) => ({ ...prev, [rowId]: !prev[rowId] }))
  }

  const useCatalogSkill = async (
    entry: SkillCatalogEntry,
    action: "load" | "validate" | "test" | "install" | "install_test"
  ) => {
    if (draftBusy || catalogBusyById[entry.id]) return
    setCatalogBusyById((prev) => ({ ...prev, [entry.id]: true }))
    setCatalogStatus(null)
    setDraftText(entry.workflow_text)
    setDraftErrors([])
    setDraftValid(false)
    setDraftName("")
    setDraftTestStatus(null)
    if (action === "load") {
      setCatalogStatus(`Loaded catalog skill: ${entry.title}`)
      setCatalogBusyById((prev) => ({ ...prev, [entry.id]: false }))
      return
    }
    setDraftBusy(true)
    try {
      if (action === "validate") {
        const result = await validateSkillDraft(entry.workflow_text)
        setDraftValid(result.valid)
        setDraftErrors(result.errors)
        setDraftName(result.name)
        setDraftStatus(result.valid ? `Draft valid: ${result.name || "unnamed skill"}` : "Draft has validation errors.")
        setCatalogStatus(result.valid ? `${entry.title}: validation passed.` : `${entry.title}: validation failed.`)
      } else if (action === "test") {
        const result = await testSkillDraft(entry.workflow_text)
        if (!result.validation.valid) {
          setCatalogStatus(`${entry.title}: test blocked by validation.`)
          setDraftErrors(result.validation.errors)
        } else if (result.run) {
          setCatalogStatus(`${entry.title}: test passed (${result.run.steps_executed} step(s)).`)
          setDraftTestStatus(
            `Draft test OK: ${result.run.skill_name} (${result.run.steps_executed} step(s), $${result.run.total_cost.toFixed(4)})`
          )
        } else {
          setCatalogStatus(`${entry.title}: test completed with no run payload.`)
        }
      } else if (action === "install") {
        const result = await saveSkillDraft(entry.workflow_text)
        setDraftValid(true)
        setDraftName(result.name)
        setDraftStatus(`Saved skill: ${result.name} (default state: disabled)`)
        setCatalogStatus(`${entry.title}: installed as disabled. Enable from Installed Skills.`)
        await load()
        await loadCatalog()
      } else {
        const saveResult = await saveSkillDraft(entry.workflow_text)
        const testResult = await testSkillDraft(entry.workflow_text)
        setDraftValid(true)
        setDraftName(saveResult.name)
        if (!testResult.validation.valid) {
          setCatalogStatus(`${entry.title}: installed, then validation failed on run.`)
        } else if (testResult.run) {
          setCatalogStatus(
            `${entry.title}: installed + test passed (${testResult.run.steps_executed} step(s), $${testResult.run.total_cost.toFixed(4)}).`
          )
        } else {
          setCatalogStatus(`${entry.title}: installed, test returned no run payload.`)
        }
        await load()
        await loadCatalog()
      }
    } catch (err) {
      const msg = err instanceof Error ? err.message : "failed"
      setCatalogStatus(`${entry.title}: ${action} failed (${msg}).`)
    } finally {
      setDraftBusy(false)
      setCatalogBusyById((prev) => ({ ...prev, [entry.id]: false }))
    }
  }

  const confirmDeleteSkill = async () => {
    if (!deleteCandidate || deleteBusy) return
    const skill = deleteCandidate
    setDeleteBusy(true)
    setDeleteStatus(null)
    try {
      await deleteSkill(skill.name)
      setSkills((prev) => prev.filter((row) => row.id !== skill.id))
      if (expandedSkill === skill.id) setExpandedSkill(null)
      setDeleteStatus(`Deleted skill: ${skill.name}`)
      setDeleteCandidate(null)
    } catch (err) {
      const msg = err instanceof Error ? err.message : "failed"
      setDeleteStatus(`Delete failed (${msg}).`)
    } finally {
      setDeleteBusy(false)
    }
  }

  const handleRunSkill = async (skill: Skill) => {
    if (runBusyBySkill[skill.id]) return
    const rawInput = (runInputBySkill[skill.id] ?? "{}").trim()
    let parsedInput: Record<string, unknown> = {}
    if (rawInput) {
      try {
        const parsed = JSON.parse(rawInput)
        if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) {
          parsedInput = parsed as Record<string, unknown>
        } else {
          setRunStatusBySkill((prev) => ({ ...prev, [skill.id]: "Input JSON must be an object." }))
          return
        }
      } catch {
        setRunStatusBySkill((prev) => ({ ...prev, [skill.id]: "Input JSON is invalid." }))
        return
      }
    }
    setRunBusyBySkill((prev) => ({ ...prev, [skill.id]: true }))
    setRunStatusBySkill((prev) => ({ ...prev, [skill.id]: "Running skill..." }))
    try {
      const result = await runInstalledSkill(skill.name, {
        mode: runModeBySkill[skill.id] ?? "single",
        input: parsedInput,
      })
      if (!result.validation.valid) {
        setRunStatusBySkill((prev) => ({
          ...prev,
          [skill.id]: `Validation failed: ${result.validation.errors.join("; ") || "unknown error"}`,
        }))
        setRunOutputBySkill((prev) => ({ ...prev, [skill.id]: "" }))
        return
      }
      if (!result.run) {
        setRunStatusBySkill((prev) => ({ ...prev, [skill.id]: "Skill run completed with no run payload." }))
        setRunOutputBySkill((prev) => ({ ...prev, [skill.id]: "" }))
        return
      }
      const run = result.run
      setRunStatusBySkill((prev) => ({
        ...prev,
        [skill.id]: `Run OK: ${run.steps_executed} step(s), $${run.total_cost.toFixed(4)}`,
      }))
      setRunOutputBySkill((prev) => ({
        ...prev,
        [skill.id]: JSON.stringify(run.outputs, null, 2),
      }))
    } catch (err) {
      const msg = err instanceof Error ? err.message : "failed"
      setRunStatusBySkill((prev) => ({ ...prev, [skill.id]: `Run failed (${msg}).` }))
    } finally {
      setRunBusyBySkill((prev) => ({ ...prev, [skill.id]: false }))
    }
  }

  const handleExportSkill = async (skill: Skill) => {
    setExportCandidate(skill)
  }

  const confirmExportSkill = async () => {
    if (!exportCandidate || exportConfirmBusy) return
    const skill = exportCandidate
    if (exportBusyBySkill[skill.id]) return
    setExportConfirmBusy(true)
    setExportBusyBySkill((prev) => ({ ...prev, [skill.id]: true }))
    setExportStatusBySkill((prev) => ({ ...prev, [skill.id]: "Exporting..." }))
    try {
      const bundle = await exportSkill(skill.name)
      const payload = {
        exported_at: new Date().toISOString(),
        skill: {
          name: bundle.name,
          enabled: bundle.enabled,
          checksum: bundle.checksum,
          signature_verified: bundle.signature_verified,
        },
        workflow_text: bundle.workflow_text,
      }
      const blob = new Blob([JSON.stringify(payload, null, 2)], { type: "application/json" })
      const url = URL.createObjectURL(blob)
      const a = document.createElement("a")
      a.href = url
      a.download = `skill-${bundle.name}.json`
      document.body.appendChild(a)
      a.click()
      document.body.removeChild(a)
      URL.revokeObjectURL(url)
      setExportStatusBySkill((prev) => ({ ...prev, [skill.id]: "Exported." }))
      setExportCandidate(null)
    } catch (err) {
      const msg = err instanceof Error ? err.message : "failed"
      setExportStatusBySkill((prev) => ({ ...prev, [skill.id]: `Export failed (${msg}).` }))
    } finally {
      setExportBusyBySkill((prev) => ({ ...prev, [skill.id]: false }))
      setExportConfirmBusy(false)
    }
  }

  return (
    <div className="p-6">
      <div className="mb-6">
        <h1 className="font-mono text-lg font-bold tracking-wider text-foreground">SKILLS</h1>
        <p className="text-sm text-muted-foreground">Installed workflow skills and governance status</p>
      </div>

      <div className="flex flex-col">
      <div className="order-0 mb-6 rounded-lg border border-border bg-card/30 p-4">
        <div className="mb-2 flex items-center justify-between">
          <h2 className="font-mono text-sm font-bold tracking-wider text-foreground">SKILL GOVERNANCE</h2>
          <div className="flex items-center gap-2">
            <label className="inline-flex items-center gap-1 font-mono text-[10px] text-muted-foreground">
              <input
                type="checkbox"
                checked={governanceIncludeDisabled}
                onChange={(e) => setGovernanceIncludeDisabled(e.target.checked)}
                className="h-3 w-3"
              />
              Include Disabled
            </label>
            <button
              type="button"
              onClick={() => void runGovernance()}
              disabled={governanceRunning}
              className="rounded border border-neon-cyan/40 bg-neon-cyan/10 px-2 py-1 font-mono text-[10px] text-neon-cyan disabled:opacity-50"
            >
              {governanceRunning ? "Running..." : "Run Analysis"}
            </button>
            <button
              type="button"
              onClick={() => setGovernanceModalOpen(true)}
              disabled={!governanceReport}
              className="rounded border border-border px-2 py-1 font-mono text-[10px] text-muted-foreground disabled:opacity-50"
            >
              View Details
            </button>
          </div>
        </div>
        {governanceStatus && <p className={statusMessageClass(governanceStatus, "font-mono text-[10px]")}>{governanceStatus}</p>}
        {governanceActionStatus && (
          <p className={statusMessageClass(governanceActionStatus, "mt-1 font-mono text-[10px]")}>{governanceActionStatus}</p>
        )}
        {governanceReport && (
          <div className="mt-2 grid gap-3 sm:grid-cols-2">
            <div className="rounded border border-border bg-background/30 p-2">
              <p className="font-mono text-[10px] text-muted-foreground">Summary</p>
              <p className="mt-1 font-mono text-[10px] text-foreground">
                analyzed={governanceReport.summary.skills_analyzed} merge={governanceReport.summary.merge_candidates} crossover=
                {governanceReport.summary.crossover_candidates}
              </p>
              <p className="mt-1 break-all font-mono text-[10px] text-muted-foreground">
                out: {governanceReport.artifacts.out_dir}
              </p>
            </div>
            <div className="rounded border border-border bg-background/30 p-2">
              <p className="font-mono text-[10px] text-muted-foreground">Top Merge Candidates</p>
              {governanceReport.merge_candidates.length === 0 ? (
                <p className="mt-1 font-mono text-[10px] text-muted-foreground">none</p>
              ) : (
                <div className="mt-1 space-y-1">
                  {governanceReport.merge_candidates.slice(0, 3).map((row, idx) => (
                    <p key={`gov-merge-${idx}`} className="font-mono text-[10px] text-foreground">
                      {row.skill_a} + {row.skill_b} ({row.score.toFixed(2)})
                    </p>
                  ))}
                </div>
              )}
            </div>
          </div>
        )}
      </div>

      <div className="order-2 mb-6 rounded-lg border border-border bg-card/30 p-4">
        <div className="mb-2 flex items-center justify-between">
          <h2 className="font-mono text-sm font-bold tracking-wider text-foreground">CURATED CATALOG</h2>
          <span className="font-mono text-[10px] text-muted-foreground">
            Trusted MMY starters with live install/test status
          </span>
        </div>
        {catalogLoading && <p className="mb-2 font-mono text-[10px] text-muted-foreground">Loading catalog...</p>}
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
          {catalogEntries.map((entry) => (
            <div key={entry.id} className="rounded border border-border bg-background/30 p-3">
              <div className="mb-2">
                <h3 className="font-mono text-xs font-bold text-foreground">{entry.title}</h3>
                <p className="mt-1 text-[11px] text-muted-foreground">{entry.description}</p>
              </div>
              <div className="mb-2 flex flex-wrap gap-1.5">
                <span className="inline-flex items-center gap-1 rounded border border-neon-green/40 bg-neon-green/10 px-1.5 py-0.5 font-mono text-[9px] text-neon-green">
                  <BadgeCheck className="h-2.5 w-2.5" />
                  {entry.official && entry.signature_verified ? "mmy-official" : "community"}
                </span>
                <span className="inline-flex items-center gap-1 rounded border border-neon-cyan/40 bg-neon-cyan/10 px-1.5 py-0.5 font-mono text-[9px] text-neon-cyan">
                  <TestTube2 className="h-2.5 w-2.5" />
                  tested:{entry.tested}
                </span>
                <span
                  className={cn(
                    "inline-flex items-center gap-1 rounded border px-1.5 py-0.5 font-mono text-[9px]",
                    entry.installed
                      ? "border-neon-green/40 bg-neon-green/10 text-neon-green"
                      : "border-border text-muted-foreground"
                  )}
                >
                  {entry.installed ? "installed" : "not installed"}
                </span>
                {entry.installed && (
                  <span
                    className={cn(
                      "inline-flex items-center gap-1 rounded border px-1.5 py-0.5 font-mono text-[9px]",
                      entry.signature_verified
                        ? "border-neon-green/40 bg-neon-green/10 text-neon-green"
                        : "border-neon-yellow/40 bg-neon-yellow/10 text-neon-yellow"
                    )}
                  >
                    {entry.official
                      ? `catalog-signature:${entry.signature_verified ? "verified" : "unverified"}`
                      : `signature:${entry.signature_verified ? "verified" : "unverified"}`}
                  </span>
                )}
              </div>
              <div className="grid grid-cols-2 gap-1">
                <button
                  type="button"
                  onClick={() => void useCatalogSkill(entry, "load")}
                  disabled={draftBusy || catalogBusyById[entry.id]}
                  className="rounded border border-border px-2 py-1 font-mono text-[10px] text-muted-foreground hover:text-foreground disabled:opacity-50"
                >
                  LOAD
                </button>
                <button
                  type="button"
                  onClick={() => void useCatalogSkill(entry, "validate")}
                  disabled={draftBusy || catalogBusyById[entry.id]}
                  className="rounded border border-neon-cyan/40 bg-neon-cyan/10 px-2 py-1 font-mono text-[10px] text-neon-cyan disabled:opacity-50"
                >
                  Validate
                </button>
                <button
                  type="button"
                  onClick={() => void useCatalogSkill(entry, "test")}
                  disabled={draftBusy || catalogBusyById[entry.id]}
                  className="rounded border border-neon-yellow/40 bg-neon-yellow/10 px-2 py-1 font-mono text-[10px] text-neon-yellow disabled:opacity-50"
                >
                  TEST
                </button>
                <button
                  type="button"
                  onClick={() => void useCatalogSkill(entry, "install")}
                  disabled={draftBusy || catalogBusyById[entry.id]}
                  className="rounded border border-neon-green/40 bg-neon-green/10 px-2 py-1 font-mono text-[10px] text-neon-green disabled:opacity-50"
                >
                  INSTALL
                </button>
                <button
                  type="button"
                  onClick={() => void useCatalogSkill(entry, "install_test")}
                  disabled={draftBusy || catalogBusyById[entry.id]}
                  className="col-span-2 rounded border border-neon-cyan/40 bg-neon-cyan/10 px-2 py-1 font-mono text-[10px] text-neon-cyan disabled:opacity-50"
                >
                  {catalogBusyById[entry.id] ? "Working..." : "Install + Test"}
                </button>
              </div>
            </div>
          ))}
        </div>
        {!catalogLoading && catalogEntries.length === 0 && (
          <p className="mt-2 font-mono text-[10px] text-muted-foreground">No catalog entries available from server.</p>
        )}
        {catalogStatus && <p className={statusMessageClass(catalogStatus, "mt-2 font-mono text-[10px]")}>{catalogStatus}</p>}
      </div>

      <div className="order-1 mb-6 rounded-lg border border-border bg-card/30 p-4">
        <div className="mb-2 flex items-center justify-between">
          <h2 className="font-mono text-sm font-bold tracking-wider text-foreground">SKILL DRAFT</h2>
          <div className="flex flex-wrap items-center gap-2">
            <input
              ref={fileInputRef}
              type="file"
              accept=".yaml,.yml,text/yaml,application/x-yaml,text/plain"
              className="hidden"
              onChange={(e) => {
                void handleFileSelected(e)
              }}
            />
            <input
              ref={importFileInputRef}
              type="file"
              accept=".json,application/json,text/plain"
              className="hidden"
              onChange={(e) => {
                void handleImportFileSelected(e)
              }}
            />
            <div ref={authorMenuRef} className="relative">
              <button
                type="button"
                onClick={() => {
                  setAuthorMenuOpen((prev) => !prev)
                  setImportMenuOpen(false)
                }}
                title="Click to open authoring actions"
                className="rounded border border-border px-2 py-1 font-mono text-[10px] text-muted-foreground"
              >
                Author
              </button>
              {authorMenuOpen && (
                <div className="absolute left-0 z-20 mt-1 min-w-36 rounded border border-border bg-card p-1 shadow-lg">
                <button
                  type="button"
                  onClick={handlePickFile}
                  disabled={draftBusy}
                  className="block w-full rounded px-2 py-1 text-left font-mono text-[10px] text-muted-foreground hover:bg-background/60 hover:text-foreground disabled:opacity-50"
                >
                  Load YAML
                </button>
                <button
                  type="button"
                  onClick={() => setDraftText(starterSkillYaml)}
                  disabled={draftBusy}
                  className="mt-1 block w-full rounded px-2 py-1 text-left font-mono text-[10px] text-muted-foreground hover:bg-background/60 hover:text-foreground disabled:opacity-50"
                >
                  Load Template
                </button>
                </div>
              )}
            </div>
            <div ref={importMenuRef} className="relative">
              <button
                type="button"
                onClick={() => {
                  setImportMenuOpen((prev) => !prev)
                  setAuthorMenuOpen(false)
                }}
                title="Click to open import actions"
                className="rounded border border-border px-2 py-1 font-mono text-[10px] text-muted-foreground"
              >
                Import Skill
              </button>
              {importMenuOpen && (
                <div className="absolute left-0 z-20 mt-1 min-w-40 rounded border border-border bg-card p-1 shadow-lg">
                <button
                  type="button"
                  onClick={handlePickImportFile}
                  disabled={draftBusy || importBusy}
                  className="block w-full rounded px-2 py-1 text-left font-mono text-[10px] text-muted-foreground hover:bg-background/60 hover:text-foreground disabled:opacity-50"
                >
                  {importBusy ? "IMPORTING..." : "Import Bundle"}
                </button>
                </div>
              )}
            </div>
            <span className="mx-1 h-4 w-px bg-border" />
            <button
              type="button"
              onClick={() => void handleValidateDraft()}
              disabled={draftBusy || !draftText.trim()}
              className="rounded border border-neon-cyan/40 bg-neon-cyan/10 px-2 py-1 font-mono text-[10px] text-neon-cyan disabled:opacity-50"
            >
              {draftBusy ? "Working..." : "Validate"}
            </button>
            <button
              type="button"
              onClick={() => void handleTestDraft()}
              disabled={draftBusy || !draftText.trim()}
              className="rounded border border-neon-yellow/40 bg-neon-yellow/10 px-2 py-1 font-mono text-[10px] text-neon-yellow disabled:opacity-50"
            >
              Test Draft
            </button>
            <button
              type="button"
              onClick={() => void handleSaveDraft()}
              disabled={draftBusy || !draftText.trim()}
              className="rounded border border-neon-green/40 bg-neon-green/10 px-2 py-1 font-mono text-[10px] text-neon-green disabled:opacity-50"
            >
              Save Skill
            </button>
          </div>
        </div>
        <textarea
          value={draftText}
          onChange={(e) => setDraftText(e.target.value)}
          rows={10}
          placeholder="Paste workflow YAML here..."
          className="w-full resize-y rounded border border-border bg-background/50 p-2 font-mono text-xs text-foreground focus:outline-none"
        />
        {draftStatus && <p className={statusMessageClass(draftStatus, "mt-2 font-mono text-[10px]")}>{draftStatus}</p>}
        {draftName && (
          <p className="mt-1 font-mono text-[10px] text-muted-foreground">name: {draftName}</p>
        )}
        {draftErrors.length > 0 && (
          <div className="mt-2 rounded border border-neon-red/40 bg-neon-red/5 p-2">
            {draftErrors.map((issue, idx) => (
              <p key={`draft-error-${idx}`} className="font-mono text-[10px] text-neon-red">
                {issue}
              </p>
            ))}
          </div>
        )}
        {draftTestStatus && <p className={statusMessageClass(draftTestStatus, "mt-2 font-mono text-[10px]")}>{draftTestStatus}</p>}
        {importStatus && <p className={statusMessageClass(importStatus, "mt-2 font-mono text-[10px]")}>{importStatus}</p>}
      </div>
      </div>

      {loading && <div className="font-mono text-sm text-muted-foreground">Loading skills...</div>}
      {error && (
        <div className="mb-4 font-mono text-xs text-neon-yellow">
          Skills service is unavailable right now ({error}).
        </div>
      )}
      {deleteStatus && <div className={statusMessageClass(deleteStatus, "mb-3 font-mono text-[10px]")}>{deleteStatus}</div>}
      {!loading && skills.length === 0 && <div className="font-mono text-sm text-muted-foreground">No skills found.</div>}

      <div className="grid gap-4 sm:grid-cols-2">
        {skills.map((skill) => {
          const risk = riskConfig[skill.risk_level] ?? riskConfig.low
          const isExpanded = expandedSkill === skill.id

          return (
            <div key={skill.id} className="glass-card rounded-lg">
              <div className="flex items-start gap-3 p-4">
                <div className="flex-1">
                  <div className="mb-1 flex items-center gap-2">
                    <h3 className="font-mono text-sm font-bold text-foreground">{skill.name}</h3>
                    <span className={cn("rounded-full border px-2 py-0.5 font-mono text-[10px] uppercase", risk.color, risk.glow)}>
                      {skill.risk_level}
                    </span>
                  </div>
                  <p className="text-xs text-muted-foreground">{skill.description || "No description"}</p>
                </div>

                <button
                  type="button"
                  role="switch"
                  aria-checked={skill.enabled}
                  onClick={() => void toggleEnabled(skill)}
                  className={cn(
                    "relative h-5 w-10 shrink-0 rounded-full transition-colors",
                    skill.enabled ? "bg-neon-cyan/30 shadow-[0_0_8px_rgba(0,240,255,0.3)]" : "bg-secondary"
                  )}
                >
                  <span
                    className={cn(
                      "absolute top-0.5 left-0.5 h-4 w-4 rounded-full transition-all",
                      skill.enabled ? "translate-x-5 bg-neon-cyan" : "bg-muted-foreground"
                    )}
                  />
                </button>
              </div>

              <div className="flex border-t border-border">
                <button
                  type="button"
                  onClick={() => setExpandedSkill(isExpanded ? null : skill.id)}
                  className="flex flex-1 items-center justify-center gap-1.5 py-2 font-mono text-[10px] text-muted-foreground transition-colors hover:text-foreground"
                >
                  {isExpanded ? <ChevronDown className="h-3 w-3" /> : <ChevronRight className="h-3 w-3" />}
                  DETAILS
                </button>
              </div>

              {isExpanded && (
                <div className="border-t border-border p-4">
                  <div className="mb-2 flex justify-end gap-2">
                    <button
                      type="button"
                      onClick={() => void handleRunSkill(skill)}
                      disabled={!skill.enabled || runBusyBySkill[skill.id]}
                      className="inline-flex items-center gap-1 rounded border border-neon-cyan/40 bg-neon-cyan/10 px-2 py-1 font-mono text-[10px] text-neon-cyan disabled:opacity-50"
                    >
                      {runBusyBySkill[skill.id] ? "Running..." : "Run"}
                    </button>
                    <button
                      type="button"
                      onClick={() => void handleExportSkill(skill)}
                      disabled={exportBusyBySkill[skill.id]}
                      className="inline-flex items-center gap-1 rounded border border-border px-2 py-1 font-mono text-[10px] text-muted-foreground disabled:opacity-50"
                    >
                      <Download className="h-3 w-3" />
                      {exportBusyBySkill[skill.id] ? "Exporting..." : "Export"}
                    </button>
                    <button
                      type="button"
                      onClick={() => void handleDeleteSkill(skill)}
                      className="inline-flex items-center gap-1 rounded border border-neon-red/40 bg-neon-red/10 px-2 py-1 font-mono text-[10px] text-neon-red"
                    >
                      <Trash2 className="h-3 w-3" />
                      Delete
                    </button>
                  </div>
                  {exportStatusBySkill[skill.id] && (
                    <p className="mb-2 font-mono text-[10px] text-muted-foreground">{exportStatusBySkill[skill.id]}</p>
                  )}
                  <div className="mb-2 flex items-center gap-2">
                    <Shield className={cn("h-3 w-3", skill.signature_verified ? "text-neon-green" : "text-neon-yellow")} />
                    <span className="font-mono text-[10px] text-muted-foreground">
                      signature: {skill.signature_verified ? "verified" : "not verified"}
                    </span>
                  </div>
                  {skill.checksum && (
                    <div className="mb-2">
                      <span className="font-mono text-[10px] text-muted-foreground">checksum</span>
                      <div className="font-mono text-[10px] text-foreground break-all">{skill.checksum}</div>
                    </div>
                  )}
                  {skill.manifest && (
                    <div className="mt-2">
                      <span className="font-mono text-[10px] text-muted-foreground">MANIFEST</span>
                      <pre className="mt-1 overflow-auto rounded-md bg-background p-2 font-mono text-[10px] text-foreground">
                        {JSON.stringify(skill.manifest, null, 2)}
                      </pre>
                    </div>
                  )}
                  <div className="mt-3 rounded border border-border bg-background/30 p-2">
                    <p className="font-mono text-[10px] text-muted-foreground">Run Skill</p>
                    <div className="mt-2 flex items-center gap-2">
                      <label className="font-mono text-[10px] text-muted-foreground">
                        mode
                        <select
                          value={runModeBySkill[skill.id] ?? "single"}
                          onChange={(e) =>
                            setRunModeBySkill((prev) => ({
                              ...prev,
                              [skill.id]: e.target.value as Mode,
                            }))
                          }
                          className="ml-1 rounded border border-border bg-background/60 px-1 py-0.5 font-mono text-[10px] text-foreground"
                        >
                          {(
                            [
                              ["single", "Single"],
                              ["critique", "Critique"],
                              ["debate", "Debate"],
                              ["consensus", "Consensus"],
                              ["council", "Council"],
                              ["retrieval", "Web"],
                            ] as const
                          ).map(([mode, label]) => (
                            <option key={`run-mode-${skill.id}-${mode}`} value={mode}>
                              {label}
                            </option>
                          ))}
                        </select>
                      </label>
                      <button
                        type="button"
                        onClick={() => void handleRunSkill(skill)}
                        disabled={!skill.enabled || runBusyBySkill[skill.id]}
                        className="rounded border border-neon-cyan/40 bg-neon-cyan/10 px-2 py-1 font-mono text-[10px] text-neon-cyan disabled:opacity-50"
                      >
                        {runBusyBySkill[skill.id] ? "Running..." : "Run"}
                      </button>
                    </div>
                    <textarea
                      value={runInputBySkill[skill.id] ?? "{}"}
                      onChange={(e) =>
                        setRunInputBySkill((prev) => ({
                          ...prev,
                          [skill.id]: e.target.value,
                        }))
                      }
                      rows={4}
                      className="mt-2 w-full rounded border border-border bg-background/60 p-2 font-mono text-[10px] text-foreground"
                    />
                    <p className="mt-1 font-mono text-[10px] text-muted-foreground">input JSON (object)</p>
                    {runStatusBySkill[skill.id] && (
                      <p className="mt-1 font-mono text-[10px] text-neon-yellow">{runStatusBySkill[skill.id]}</p>
                    )}
                    {runOutputBySkill[skill.id] && (
                      <pre className="mt-2 max-h-48 overflow-auto rounded border border-border bg-background p-2 font-mono text-[10px] text-foreground">
                        {runOutputBySkill[skill.id]}
                      </pre>
                    )}
                  </div>
                </div>
              )}
            </div>
          )
        })}
      </div>

      {deleteCandidate && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/45 p-4">
          <div className="w-full max-w-md rounded-lg border border-neon-red/40 bg-card p-4 shadow-xl">
            <h3 className="font-mono text-sm font-bold tracking-wider text-foreground">Delete Skill</h3>
            <p className="mt-1 font-mono text-xs text-muted-foreground">
              Delete <span className="text-foreground">{deleteCandidate.name}</span>? This removes its workflow file from managed
              skills storage.
            </p>
            <div className="mt-4 flex justify-end gap-2">
              <button
                type="button"
                onClick={() => setDeleteCandidate(null)}
                disabled={deleteBusy}
                className="rounded border border-border px-3 py-1.5 font-mono text-xs text-muted-foreground disabled:opacity-50"
              >
                CANCEL
              </button>
              <button
                type="button"
                onClick={() => void confirmDeleteSkill()}
                disabled={deleteBusy}
                className="rounded border border-neon-red/40 bg-neon-red/10 px-3 py-1.5 font-mono text-xs text-neon-red disabled:opacity-50"
              >
                {deleteBusy ? "Deleting..." : "Delete"}
              </button>
            </div>
          </div>
        </div>
      )}
      {exportCandidate && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/45 p-4">
          <div className="w-full max-w-md rounded-lg border border-neon-cyan/40 bg-card p-4 shadow-xl">
            <h3 className="font-mono text-sm font-bold tracking-wider text-foreground">Export SKILL</h3>
            <p className="mt-1 font-mono text-xs text-muted-foreground">
              Export <span className="text-foreground">{exportCandidate.name}</span> as a JSON bundle?
            </p>
            <div className="mt-4 flex justify-end gap-2">
              <button
                type="button"
                onClick={() => setExportCandidate(null)}
                disabled={exportConfirmBusy}
                className="rounded border border-border px-3 py-1.5 font-mono text-xs text-muted-foreground disabled:opacity-50"
              >
                NO
              </button>
              <button
                type="button"
                onClick={() => void confirmExportSkill()}
                disabled={exportConfirmBusy}
                className="rounded border border-neon-cyan/40 bg-neon-cyan/10 px-3 py-1.5 font-mono text-xs text-neon-cyan disabled:opacity-50"
              >
                {exportConfirmBusy ? "Exporting..." : "Yes"}
              </button>
            </div>
          </div>
        </div>
      )}
      {governanceModalOpen && governanceReport && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4">
          <div className="max-h-[92vh] w-full max-w-6xl overflow-hidden rounded-lg border border-neon-cyan/40 bg-card shadow-xl">
            <div className="flex items-center justify-between border-b border-border px-4 py-3">
              <div>
                <h3 className="font-mono text-sm font-bold tracking-wider text-foreground">Skill Governance Report</h3>
                <p className="font-mono text-[10px] text-muted-foreground">
                  analyzed={governanceReport.summary.skills_analyzed} merge={governanceReport.summary.merge_candidates} crossover=
                  {governanceReport.summary.crossover_candidates}
                </p>
              </div>
              <div className="flex items-center gap-2">
                <button
                  type="button"
                  onClick={() => void copyGovernanceReport()}
                  className="rounded border border-neon-cyan/40 bg-neon-cyan/10 px-2 py-1 font-mono text-[10px] text-neon-cyan"
                >
                  Copy JSON
                </button>
                <button
                  type="button"
                  onClick={exportGovernanceReport}
                  className="rounded border border-neon-green/40 bg-neon-green/10 px-2 py-1 font-mono text-[10px] text-neon-green"
                >
                  Export JSON
                </button>
                <button
                  type="button"
                  onClick={() => setGovernanceModalOpen(false)}
                  className="rounded border border-border px-2 py-1 font-mono text-[10px] text-muted-foreground"
                >
                  Close
                </button>
              </div>
            </div>
            <div className="max-h-[calc(92vh-54px)] space-y-4 overflow-auto p-4">
              <div className="rounded border border-border bg-background/30 p-2">
                <p className="font-mono text-[10px] text-muted-foreground">Artifacts</p>
                <div className="mt-1 space-y-1 font-mono text-[10px] text-foreground">
                  <p className="break-all">out: {governanceReport.artifacts.out_dir}</p>
                  <p className="break-all">merge: {governanceReport.artifacts.merge_candidates_path}</p>
                  <p className="break-all">crossover: {governanceReport.artifacts.crossover_candidates_path}</p>
                  <p className="break-all">bloat: {governanceReport.artifacts.skills_bloat_report_path}</p>
                  <p className="break-all">deprecation: {governanceReport.artifacts.deprecation_plan_path}</p>
                </div>
              </div>

              <div className="rounded border border-border bg-background/30 p-2">
                <p className="mb-2 font-mono text-[10px] text-muted-foreground">Merge Candidates</p>
                {governanceReport.merge_candidates.length === 0 ? (
                  <p className="font-mono text-[10px] text-muted-foreground">No merge candidates.</p>
                ) : (
                  <div className="overflow-auto">
                    <table className="min-w-full border-collapse font-mono text-[10px]">
                      <thead>
                        <tr className="border-b border-border text-muted-foreground">
                          <th className="w-8 px-2 py-1 text-left font-normal"> </th>
                          <th className="px-2 py-1 text-left font-normal">Pair</th>
                          <th className="px-2 py-1 text-right font-normal">Score</th>
                          <th className="px-2 py-1 text-right font-normal">Capability</th>
                          <th className="px-2 py-1 text-right font-normal">I/O</th>
                          <th className="px-2 py-1 text-right font-normal">Deps</th>
                          <th className="px-2 py-1 text-left font-normal">Recommendation</th>
                        </tr>
                      </thead>
                      <tbody>
                        {governanceReport.merge_candidates.map((row, idx) => (
                          <Fragment key={`gov-merge-fragment-${idx}`}>
                            <tr className="border-b border-border/60 text-foreground align-top">
                              <td className="px-2 py-1">
                                <button
                                  type="button"
                                  onClick={() => toggleGovernanceRow(`merge-${idx}`)}
                                  className="inline-flex h-5 w-5 items-center justify-center rounded border border-border text-muted-foreground hover:text-foreground"
                                >
                                  {expandedGovernanceRows[`merge-${idx}`] ? (
                                    <ChevronDown className="h-3 w-3" />
                                  ) : (
                                    <ChevronRight className="h-3 w-3" />
                                  )}
                                </button>
                              </td>
                              <td className="px-2 py-1">{row.skill_a} + {row.skill_b}</td>
                              <td className="px-2 py-1 text-right">{row.score.toFixed(2)}</td>
                              <td className="px-2 py-1 text-right">{row.capability_overlap.toFixed(2)}</td>
                              <td className="px-2 py-1 text-right">{row.io_overlap.toFixed(2)}</td>
                              <td className="px-2 py-1 text-right">{row.dependency_overlap.toFixed(2)}</td>
                              <td className="px-2 py-1">{row.recommendation}</td>
                            </tr>
                            {expandedGovernanceRows[`merge-${idx}`] && (
                              <tr className="border-b border-border/40 bg-background/40">
                                <td className="px-2 py-2"> </td>
                                <td colSpan={6} className="px-2 py-2">
                                  <p className="mb-1 text-[10px] text-muted-foreground">Rationale</p>
                                  <p className="text-[10px] text-foreground">{row.rationale || "No rationale provided."}</p>
                                </td>
                              </tr>
                            )}
                          </Fragment>
                        ))}
                      </tbody>
                    </table>
                  </div>
                )}
              </div>

              <div className="rounded border border-border bg-background/30 p-2">
                <p className="mb-2 font-mono text-[10px] text-muted-foreground">Crossover Candidates</p>
                {governanceReport.crossover_candidates.length === 0 ? (
                  <p className="font-mono text-[10px] text-muted-foreground">No crossover candidates.</p>
                ) : (
                  <div className="overflow-auto">
                    <table className="min-w-full border-collapse font-mono text-[10px]">
                      <thead>
                        <tr className="border-b border-border text-muted-foreground">
                          <th className="w-8 px-2 py-1 text-left font-normal"> </th>
                          <th className="px-2 py-1 text-left font-normal">Pair</th>
                          <th className="px-2 py-1 text-right font-normal">Score</th>
                          <th className="px-2 py-1 text-right font-normal">Capability</th>
                          <th className="px-2 py-1 text-right font-normal">I/O</th>
                          <th className="px-2 py-1 text-right font-normal">Deps</th>
                          <th className="px-2 py-1 text-left font-normal">Recommendation</th>
                        </tr>
                      </thead>
                      <tbody>
                        {governanceReport.crossover_candidates.map((row, idx) => (
                          <Fragment key={`gov-cross-fragment-${idx}`}>
                            <tr className="border-b border-border/60 text-foreground align-top">
                              <td className="px-2 py-1">
                                <button
                                  type="button"
                                  onClick={() => toggleGovernanceRow(`cross-${idx}`)}
                                  className="inline-flex h-5 w-5 items-center justify-center rounded border border-border text-muted-foreground hover:text-foreground"
                                >
                                  {expandedGovernanceRows[`cross-${idx}`] ? (
                                    <ChevronDown className="h-3 w-3" />
                                  ) : (
                                    <ChevronRight className="h-3 w-3" />
                                  )}
                                </button>
                              </td>
                              <td className="px-2 py-1">{row.skill_a} + {row.skill_b}</td>
                              <td className="px-2 py-1 text-right">{row.score.toFixed(2)}</td>
                              <td className="px-2 py-1 text-right">{row.capability_overlap.toFixed(2)}</td>
                              <td className="px-2 py-1 text-right">{row.io_overlap.toFixed(2)}</td>
                              <td className="px-2 py-1 text-right">{row.dependency_overlap.toFixed(2)}</td>
                              <td className="px-2 py-1">{row.recommendation}</td>
                            </tr>
                            {expandedGovernanceRows[`cross-${idx}`] && (
                              <tr className="border-b border-border/40 bg-background/40">
                                <td className="px-2 py-2"> </td>
                                <td colSpan={6} className="px-2 py-2">
                                  <p className="mb-1 text-[10px] text-muted-foreground">Rationale</p>
                                  <p className="text-[10px] text-foreground">{row.rationale || "No rationale provided."}</p>
                                </td>
                              </tr>
                            )}
                          </Fragment>
                        ))}
                      </tbody>
                    </table>
                  </div>
                )}
              </div>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
