"use client"

import { useState, useEffect, useMemo } from "react"
import {
  DEFAULT_SETTINGS,
  getSettings,
  saveSettings,
  pickSyncableSettings,
  mergeSettings,
  type AppSettings,
  type McpServerEntry,
  type RoleRoutingConfig,
} from "@/lib/settings"
import { cn, statusMessageClass } from "@/lib/utils"
import { Globe, Key, Palette, Shield, Check, Plus, Trash2, Lock, RefreshCw } from "lucide-react"
import {
  applyRoleRoutingConfig,
  applyProviderConfig,
  clearProviderApiKey,
  configureRemoteAccess,
  fetchUiSettingsProfile,
  fetchRemoteAccessStatus,
  fetchServerSetupStatus,
  fetchProviderCatalog,
  fetchProviderConfig,
  fetchProviderModels,
  runRemoteAccessHealth,
  fetchRoleRoutingConfig,
  fetchProviderKeyStatus,
  fetchDelegationHealth,
  fetchDelegationJobs,
  fetchHealth,
  getAdminPasswordStatus,
  recoverServerToken,
  revokeRemoteAccess,
  rotateServerToken,
  runServerDoctor,
  runMcpHealth,
  fetchMcpServers,
  saveMcpServers,
  runStrictReliabilityCheck,
  saveUiSettingsProfile,
  setAdminPassword,
  deleteDelegationJob,
  deleteDelegationJobs,
  type ServerSetupStatus,
  type RemoteAccessStatus,
  type RemoteAccessHealthReport,
  type ServerDoctorReport,
  type McpHealthReport,
  fetchSecurityAuditEvents,
  setProviderApiKey,
  testProviderConnection,
  type ProviderKeyStatusEntry,
  type SecurityAuditEvent,
} from "@/lib/api"

const demoPolicies = {
  guardian: {
    enabled: true,
    pre_flight: { max_risk: "medium", block_patterns: ["password", "secret"] },
    post_output: {
      check_pii: true,
      check_injection: true,
      max_output_tokens: 8192,
    },
  },
  routing: {
    default_provider: "auto",
    fallback_chain: ["openai", "anthropic", "google", "xai", "local"],
    budget_hard_limit: 50.0,
    budget_soft_limit: 40.0,
  },
}

function parseKeyValueLines(value: string): Record<string, string> {
  const out: Record<string, string> = {}
  for (const line of value.split("\n")) {
    const trimmed = line.trim()
    if (!trimmed) continue
    const idx = trimmed.indexOf(":")
    if (idx <= 0) continue
    const key = trimmed.slice(0, idx).trim()
    const val = trimmed.slice(idx + 1).trim()
    if (key && val) out[key] = val
  }
  return out
}

function formatKeyValueLines(map: Record<string, string> | undefined): string {
  if (!map) return ""
  return Object.entries(map)
    .map(([k, v]) => `${k}: ${v}`)
    .join("\n")
}

function humanizeProviderError(raw: string): string {
  const msg = String(raw || "").trim()
  const lower = msg.toLowerCase()
  if (lower.includes("references disabled provider")) {
    return "Role routing points to a disabled provider. Click Fix Routes, then apply again."
  }
  if (lower.includes("missing api key")) {
    return "One or more enabled cloud providers are missing API keys. Disable unused providers or set keys, then apply again."
  }
  if (lower.includes("provider is not active; enable it and apply provider config")) {
    return "Provider is not active on the server yet. Turn it on and click Apply To Server first."
  }
  if (lower.includes("connection error")) {
    return "Connection failed. Check provider endpoint/network, then retry TEST."
  }
  return msg || "failed"
}

function formatStatusLabel(status: string): string {
  const value = (status || "").trim()
  if (!value) return "Unknown"
  return value.charAt(0) + value.slice(1).toLowerCase()
}

export function SettingsView() {
  const [settings, setSettings] = useState<AppSettings>(DEFAULT_SETTINGS)
  const [activeTab, setActiveTab] = useState<"connection" | "appearance" | "providers" | "security">("connection")
  const [saved, setSaved] = useState(false)
  const [profileSyncStatus, setProfileSyncStatus] = useState<string | null>(null)
  const [providerCatalog, setProviderCatalog] = useState<Record<string, string[]>>({})
  const [providerStatus, setProviderStatus] = useState<string | null>(null)
  const [roleRoutingStatus, setRoleRoutingStatus] = useState<string | null>(null)
  const [providerKeys, setProviderKeys] = useState<Record<string, ProviderKeyStatusEntry>>({})
  const [providerKeyInputs, setProviderKeyInputs] = useState<Record<string, string>>({})
  const [providerKeyStatus, setProviderKeyStatus] = useState<string | null>(null)
  const [providerTestStatus, setProviderTestStatus] = useState<Record<string, string>>({})
  const [testingProviders, setTestingProviders] = useState<Record<string, boolean>>({})
  const [smokeStatus, setSmokeStatus] = useState<string | null>(null)
  const [smokeRunning, setSmokeRunning] = useState(false)
  const [authStatus, setAuthStatus] = useState<string | null>(null)
  const [rotatingToken, setRotatingToken] = useState(false)
  const [freshToken, setFreshToken] = useState<string | null>(null)
  const [adminPassword, setAdminPasswordInput] = useState("")
  const [adminPasswordStatus, setAdminPasswordStatus] = useState<string | null>(null)
  const [adminPasswordConfigured, setAdminPasswordConfigured] = useState<boolean | null>(null)
  const [settingAdminPassword, setSettingAdminPassword] = useState(false)
  const [recoveringToken, setRecoveringToken] = useState(false)
  const [selfTestStatus, setSelfTestStatus] = useState<string | null>(null)
  const [selfTestResult, setSelfTestResult] = useState<{ status: "pass" | "fail"; reason: string } | null>(null)
  const [selfTestRunning, setSelfTestRunning] = useState(false)
  const [doctorStatus, setDoctorStatus] = useState<string | null>(null)
  const [doctorRunning, setDoctorRunning] = useState(false)
  const [doctorReport, setDoctorReport] = useState<ServerDoctorReport | null>(null)
  const [doctorModalOpen, setDoctorModalOpen] = useState(false)
  const [doctorAutoRefresh, setDoctorAutoRefresh] = useState(false)
  const [doctorSmokeProviders, setDoctorSmokeProviders] = useState(true)
  const [doctorGovernance, setDoctorGovernance] = useState(false)
  const [mcpStatus, setMcpStatus] = useState<string | null>(null)
  const [mcpHealthStatus, setMcpHealthStatus] = useState<string | null>(null)
  const [mcpHealthRunning, setMcpHealthRunning] = useState(false)
  const [mcpHealthReport, setMcpHealthReport] = useState<McpHealthReport | null>(null)
  const [mcpModalOpen, setMcpModalOpen] = useState(false)
  const [mcpServerTesting, setMcpServerTesting] = useState<Record<string, boolean>>({})
  const [setupStatus, setSetupStatus] = useState<ServerSetupStatus | null>(null)
  const [setupWizardOpen, setSetupWizardOpen] = useState(false)
  const [setupWizardLoading, setSetupWizardLoading] = useState(false)
  const [setupWizardStatus, setSetupWizardStatus] = useState<string | null>(null)
  const [remoteAccessStatus, setRemoteAccessStatus] = useState<RemoteAccessStatus | null>(null)
  const [remoteAccessBusy, setRemoteAccessBusy] = useState(false)
  const [remoteAccessMessage, setRemoteAccessMessage] = useState<string | null>(null)
  const [remoteAccessHealthBusy, setRemoteAccessHealthBusy] = useState(false)
  const [remoteAccessHealth, setRemoteAccessHealth] = useState<RemoteAccessHealthReport | null>(null)
  const [remoteMode, setRemoteMode] = useState<"lan" | "tailscale" | "cloudflare" | "manual_proxy">("lan")
  const [remoteBindHost, setRemoteBindHost] = useState("127.0.0.1")
  const [remoteBindPort, setRemoteBindPort] = useState("8100")
  const [remotePublicUrl, setRemotePublicUrl] = useState("")
  const [remoteNotes, setRemoteNotes] = useState("")
  const [delegateHealthStatus, setDelegateHealthStatus] = useState<string | null>(null)
  const [delegateHealthRunning, setDelegateHealthRunning] = useState(false)
  const [delegateJobsStatus, setDelegateJobsStatus] = useState<string | null>(null)
  const [delegateJobsBusy, setDelegateJobsBusy] = useState(false)
  const [delegateJobsCount, setDelegateJobsCount] = useState<number | null>(null)
  const [delegateDeleteJobId, setDelegateDeleteJobId] = useState("")
  const [delegateDeleteOlderOpen, setDelegateDeleteOlderOpen] = useState(false)
  const [delegateDeleteOlderDays, setDelegateDeleteOlderDays] = useState("30")
  const [strictCheckRuns, setStrictCheckRuns] = useState(5)
  const [strictCheckStatus, setStrictCheckStatus] = useState<string | null>(null)
  const [strictCheckRunning, setStrictCheckRunning] = useState(false)
  const [strictCheckFailures, setStrictCheckFailures] = useState<Array<{ index: number; answer: string }>>([])
  const [strictFailuresOpen, setStrictFailuresOpen] = useState(false)
  const [securityEvents, setSecurityEvents] = useState<SecurityAuditEvent[]>([])
  const [securityEventsStatus, setSecurityEventsStatus] = useState<string | null>(null)
  const [securityEventsLoading, setSecurityEventsLoading] = useState(false)
  const [securityEventsExporting, setSecurityEventsExporting] = useState(false)
  const [securityVisibleCount, setSecurityVisibleCount] = useState(25)
  const [securityLogPassword, setSecurityLogPassword] = useState("")
  const [securityErrorsOnly, setSecurityErrorsOnly] = useState(false)
  const [securityEventFilter, setSecurityEventFilter] = useState<"all" | "ops" | "remote" | "auth" | "lockout">("all")
  const [securityBadgePulse, setSecurityBadgePulse] = useState(false)
  const [showAutoFixApplyModal, setShowAutoFixApplyModal] = useState(false)
  const [autoFixApplyPending, setAutoFixApplyPending] = useState(false)

  useEffect(() => {
    const local = getSettings()
    setSettings(local)
    void (async () => {
      try {
        const profile = await fetchUiSettingsProfile()
        const merged = mergeSettings(local, profile as Partial<AppSettings>)
        merged.bearerToken = local.bearerToken
        setSettings(merged)
        saveSettings(merged)
        setProfileSyncStatus("Loaded UI settings profile from server.")
      } catch (err) {
        const msg = err instanceof Error ? err.message : "failed"
        setProfileSyncStatus(`Using local UI settings (${msg}).`)
      }
    })()
    void fetchProviderCatalog()
      .then((catalog) => setProviderCatalog(catalog))
      .catch(() => setProviderCatalog({}))
    void loadProviderKeysFromDaemon()
    void loadAdminPasswordStatus()
    void loadRemoteAccessStatus()
    void checkDelegationDaemonHealth()
    void refreshDelegationJobsCount()
    void (async () => {
      try {
        const providers = await fetchProviderConfig()
        setSettings((s) => ({ ...s, providers }))
        saveSettings({ providers })
        setProviderStatus("Loaded provider config from server.")
      } catch {
        // Daemon may be offline or unauthorized at startup; keep local settings.
      }
    })()
    void (async () => {
      try {
        const roleRouting = await fetchRoleRoutingConfig()
        setSettings((s) => ({ ...s, roleRouting }))
        saveSettings({ roleRouting })
        setRoleRoutingStatus("Loaded role routing from server.")
      } catch {
        // Daemon may be offline or unauthorized at startup; keep local settings.
      }
    })()
    void (async () => {
      try {
        const mcpServers = await fetchMcpServers()
        setSettings((s) => ({ ...s, mcpServers }))
        saveSettings({ mcpServers })
        setMcpStatus("Loaded MCP server config from server.")
      } catch {
        // Keep local fallback if server is unavailable.
      }
    })()
    void refreshSetupStatus(true)
  }, [])

  const refreshSetupStatus = async (autoOpen = false) => {
    if (setupWizardLoading) return
    setSetupWizardLoading(true)
    try {
      const status = await fetchServerSetupStatus()
      setSetupStatus(status)
      if (autoOpen && !status.ready) {
        setSetupWizardOpen(true)
        setSetupWizardStatus("Setup is incomplete. Follow the checklist to finish first-run setup.")
      } else if (status.ready) {
        setSetupWizardStatus("Setup is complete.")
      } else {
        setSetupWizardStatus("Setup status refreshed.")
      }
    } catch (err) {
      const msg = err instanceof Error ? err.message : "failed"
      setSetupWizardStatus(`Could not load setup status (${msg}).`)
    } finally {
      setSetupWizardLoading(false)
    }
  }

  const loadAdminPasswordStatus = async () => {
    try {
      const status = await getAdminPasswordStatus()
      setAdminPasswordConfigured(status.configured)
    } catch {
      setAdminPasswordConfigured(null)
    }
  }

  const loadRemoteAccessStatus = async () => {
    try {
      const status = await fetchRemoteAccessStatus()
      setRemoteAccessStatus(status)
      setRemoteMode(status.profile.mode)
      setRemoteBindHost(status.profile.bind_host)
      setRemoteBindPort(String(status.profile.bind_port))
      setRemotePublicUrl(status.profile.public_base_url)
      setRemoteNotes(status.profile.notes)
      setRemoteAccessMessage(status.summary)
    } catch (err) {
      const msg = err instanceof Error ? err.message : "failed"
      setRemoteAccessMessage(`Could not load remote access plan (${msg}).`)
    }
  }

  const loadProviderKeysFromDaemon = async () => {
    setProviderKeyStatus(null)
    try {
      const rows = await fetchProviderKeyStatus()
      setProviderKeys(Object.fromEntries(rows.map((row) => [row.name, row])))
      setProviderKeyStatus("Loaded provider key status from server.")
    } catch (err) {
      const msg = err instanceof Error ? err.message : "failed"
      setProviderKeys({})
      setProviderKeyStatus(`Could not load provider key status from server (${msg}).`)
    }
  }

  const checkDelegationDaemonHealth = async () => {
    if (delegateHealthRunning) return
    setDelegateHealthRunning(true)
    try {
      const status = await fetchDelegationHealth()
      if (status.reachable) {
        setDelegateHealthStatus(`Delegation service online (${status.socket_path}).`)
      } else {
        const detail = status.detail ? `: ${status.detail}` : ""
        setDelegateHealthStatus(`Delegation service ${status.status}${detail} (${status.socket_path}).`)
      }
    } catch (err) {
      const msg = err instanceof Error ? err.message : "failed"
      setDelegateHealthStatus(`Could not check delegation service (${msg}).`)
    } finally {
      setDelegateHealthRunning(false)
      void refreshDelegationJobsCount()
    }
  }

  const requireAdminPasswordIfConfigured = (): string | undefined => {
    if (adminPasswordConfigured && !adminPassword.trim()) {
      throw new Error("Enter admin password above, then try again.")
    }
    return adminPasswordConfigured ? adminPassword.trim() : undefined
  }

  const applyRemoteAccessPlan = async () => {
    if (remoteAccessBusy) return
    setRemoteAccessBusy(true)
    setRemoteAccessMessage(remoteAccessStatus?.enabled ? "Rebinding remote access plan..." : "Configuring remote access plan...")
    try {
      const adminPassword = requireAdminPasswordIfConfigured()
      const status = await configureRemoteAccess({
        admin_password: adminPassword,
        mode: remoteMode,
        bind_host: remoteBindHost.trim() || "127.0.0.1",
        bind_port: Number.parseInt(remoteBindPort.trim(), 10) || 8100,
        public_base_url: remotePublicUrl.trim(),
        notes: remoteNotes.trim(),
      })
      setRemoteAccessStatus(status)
      setRemoteAccessMessage(status.enabled ? "Remote access plan saved." : status.summary)
    } catch (err) {
      const msg = err instanceof Error ? err.message : "failed"
      setRemoteAccessMessage(`Could not configure remote access (${msg}).`)
    } finally {
      setRemoteAccessBusy(false)
    }
  }

  const revokeRemoteAccessPlan = async () => {
    if (remoteAccessBusy) return
    setRemoteAccessBusy(true)
    setRemoteAccessMessage("Revoking remote access plan...")
    try {
      const adminPassword = requireAdminPasswordIfConfigured()
      const status = await revokeRemoteAccess({ admin_password: adminPassword })
      setRemoteAccessStatus(status)
      setRemoteAccessMessage("Remote access plan revoked. Run the rollback command if the daemon is still exposed.")
    } catch (err) {
      const msg = err instanceof Error ? err.message : "failed"
      setRemoteAccessMessage(`Could not revoke remote access (${msg}).`)
    } finally {
      setRemoteAccessBusy(false)
    }
  }

  const copyRemoteCommand = async (value: string, label: string) => {
    try {
      await navigator.clipboard.writeText(value)
      setRemoteAccessMessage(`${label} copied.`)
    } catch {
      setRemoteAccessMessage(`Could not copy ${label.toLowerCase()}.`)
    }
  }

  const runRemoteHealthProbe = async () => {
    if (remoteAccessHealthBusy) return
    setRemoteAccessHealthBusy(true)
    setRemoteAccessMessage("Running remote access health probe...")
    try {
      const report = await runRemoteAccessHealth()
      setRemoteAccessHealth(report)
      if (report.summary.failed > 0) {
        setRemoteAccessMessage(`Remote access probe found ${report.summary.failed} failure(s).`)
      } else if (report.summary.passed > 0) {
        setRemoteAccessMessage("Remote access probe passed.")
      } else {
        setRemoteAccessMessage("Remote access probe completed with no active targets.")
      }
    } catch (err) {
      const msg = err instanceof Error ? err.message : "failed"
      setRemoteAccessMessage(`Could not run remote access probe (${msg}).`)
    } finally {
      setRemoteAccessHealthBusy(false)
    }
  }

  const refreshDelegationJobsCount = async () => {
    try {
      const rows = await fetchDelegationJobs(200)
      setDelegateJobsCount(rows.length)
    } catch {
      setDelegateJobsCount(null)
    }
  }

  const deleteSingleDelegationJob = async () => {
    const jobId = delegateDeleteJobId.trim()
    if (!jobId || delegateJobsBusy) return
    setDelegateJobsBusy(true)
    setDelegateJobsStatus("Deleting delegation job...")
    try {
      const adminPassword = requireAdminPasswordIfConfigured()
      const result = await deleteDelegationJob(jobId, adminPassword, false)
      await refreshDelegationJobsCount()
      setDelegateJobsStatus(`Deleted ${result.deleted} delegation job(s).`)
      setDelegateDeleteJobId("")
    } catch (err) {
      const msg = err instanceof Error ? err.message : "failed"
      setDelegateJobsStatus(`Could not delete delegation job (${msg}).`)
    } finally {
      setDelegateJobsBusy(false)
    }
  }

  const deleteOlderDelegationJobs = async () => {
    if (delegateJobsBusy) return
    const parsed = Number.parseInt(delegateDeleteOlderDays.trim(), 10)
    const olderThanDays = Number.isFinite(parsed) && parsed > 0 ? parsed : 0
    setDelegateJobsBusy(true)
    setDelegateJobsStatus("Deleting delegation jobs...")
    try {
      const adminPassword = requireAdminPasswordIfConfigured()
      const result = await deleteDelegationJobs(
        adminPassword,
        olderThanDays > 0 ? olderThanDays : undefined,
        5000,
        false
      )
      await refreshDelegationJobsCount()
      if (olderThanDays > 0) {
        setDelegateJobsStatus(`Deleted ${result.deleted} delegation job(s) older than ${olderThanDays} day(s).`)
      } else {
        setDelegateJobsStatus(`Deleted ${result.deleted} delegation job(s).`)
      }
      setDelegateDeleteOlderOpen(false)
    } catch (err) {
      const msg = err instanceof Error ? err.message : "failed"
      setDelegateJobsStatus(`Could not delete delegation jobs (${msg}).`)
    } finally {
      setDelegateJobsBusy(false)
    }
  }

  const handleSave = () => {
    saveSettings(settings)
    void (async () => {
      try {
        await saveUiSettingsProfile(pickSyncableSettings(settings) as Record<string, unknown>)
        setProfileSyncStatus("Saved UI settings profile to server.")
      } catch (err) {
        const msg = err instanceof Error ? err.message : "failed"
        setProfileSyncStatus(`Saved locally only (${msg}).`)
      }
      setSaved(true)
      setTimeout(() => setSaved(false), 2000)
    })()
  }

  const persistSettingsSnapshot = async (next: AppSettings, successMessage?: string) => {
    saveSettings(next)
    try {
      await saveUiSettingsProfile(pickSyncableSettings(next) as Record<string, unknown>)
      if (successMessage) setProfileSyncStatus(successMessage)
    } catch (err) {
      const msg = err instanceof Error ? err.message : "failed"
      setProfileSyncStatus(`Saved locally only (${msg}).`)
    }
  }

  const makeMcpServer = (): McpServerEntry => ({
    name: "",
    transport: "stdio",
    enabled: true,
    command: "",
    args: [],
    url: "",
    headers: {},
    header_env_refs: {},
    declared_tools: [],
  })

  const createMcpTemplate = (kind: "filesystem" | "git" | "fetch"): McpServerEntry => {
    if (kind === "filesystem") {
      return {
        name: "filesystem",
        transport: "stdio",
        enabled: true,
        command: "npx",
        args: ["-y", "@modelcontextprotocol/server-filesystem", "."],
        url: "",
        headers: {},
        header_env_refs: {},
        declared_tools: ["read_file", "write_file", "list_directory"],
      }
    }
    if (kind === "git") {
      return {
        name: "git",
        transport: "stdio",
        enabled: true,
        command: "npx",
        args: ["-y", "@modelcontextprotocol/server-git", "."],
        url: "",
        headers: {},
        header_env_refs: {},
        declared_tools: ["git_status", "git_log", "git_diff"],
      }
    }
    return {
      name: "fetch",
      transport: "stdio",
      enabled: true,
      command: "npx",
      args: ["-y", "@modelcontextprotocol/server-fetch"],
      url: "",
      headers: {},
      header_env_refs: {},
      declared_tools: ["fetch_url"],
    }
  }

  const addMcpTemplate = (kind: "filesystem" | "git" | "fetch") => {
    const template = createMcpTemplate(kind)
    setSettings((s) => {
      const existing = new Set((s.mcpServers ?? []).map((row) => row.name))
      let candidate = template.name
      let i = 2
      while (existing.has(candidate)) {
        candidate = `${template.name}-${i}`
        i += 1
      }
      return {
        ...s,
        mcpServers: [...(s.mcpServers ?? []), { ...template, name: candidate }],
      }
    })
    setMcpStatus(`Added MCP template: ${kind}.`)
  }

  const addMcpServer = () => {
    setSettings((s) => ({ ...s, mcpServers: [...(s.mcpServers ?? []), makeMcpServer()] }))
  }

  const removeMcpServer = (index: number) => {
    setSettings((s) => ({
      ...s,
      mcpServers: (s.mcpServers ?? []).filter((_, idx) => idx !== index),
    }))
  }

  const patchMcpServer = (index: number, patch: Partial<McpServerEntry>) => {
    setSettings((s) => ({
      ...s,
      mcpServers: (s.mcpServers ?? []).map((row, idx) => (idx === index ? { ...row, ...patch } : row)),
    }))
  }

  const saveMcpServerConfig = async () => {
    setMcpStatus("Saving MCP server config...")
    try {
      const savedRows = await saveMcpServers(settings.mcpServers ?? [])
      setSettings((s) => ({ ...s, mcpServers: savedRows }))
      saveSettings({ mcpServers: savedRows })
      setMcpStatus(`Saved MCP server config (${savedRows.length} server${savedRows.length === 1 ? "" : "s"}).`)
    } catch (err) {
      const msg = err instanceof Error ? err.message : "failed"
      setMcpStatus(`Could not save MCP server config (${msg}).`)
    }
  }

  const runMcpHealthChecks = async () => {
    if (mcpHealthRunning) return
    setMcpHealthRunning(true)
    setMcpHealthStatus("Running MCP health checks...")
    try {
      const report = await runMcpHealth(false, true)
      setMcpHealthReport(report)
      setMcpHealthStatus(
        `MCP checks complete: ${report.summary.passed} passed, ${report.summary.failed} failed, ${report.summary.skipped} skipped.`
      )
    } catch (err) {
      const msg = err instanceof Error ? err.message : "failed"
      setMcpHealthStatus(`MCP checks failed (${msg}).`)
    } finally {
      setMcpHealthRunning(false)
    }
  }

  const runMcpServerHealthCheck = async (serverName: string) => {
    if (!serverName || mcpServerTesting[serverName]) return
    setMcpServerTesting((s) => ({ ...s, [serverName]: true }))
    setMcpHealthStatus(`Running MCP health for ${serverName}...`)
    try {
      const report = await runMcpHealth(true, true, [serverName])
      setMcpHealthReport(report)
      const row = report.checks.find((item) => item.name === serverName)
      if (row) {
        setMcpHealthStatus(
          `${serverName}: ${row.status} (${row.detail}${row.remediation ? ` | ${row.remediation}` : ""})`
        )
      } else {
        setMcpHealthStatus(`MCP check complete for ${serverName}.`)
      }
    } catch (err) {
      const msg = err instanceof Error ? err.message : "failed"
      setMcpHealthStatus(`MCP check failed for ${serverName} (${msg}).`)
    } finally {
      setMcpServerTesting((s) => ({ ...s, [serverName]: false }))
    }
  }

  const remediationCommandForMcp = (server: McpServerEntry, errorCode?: string) => {
    const envLines = Object.values(server.header_env_refs ?? {})
      .filter((value) => value.trim().length > 0)
      .map((value) => `export ${value}=<value>`)
    const prefix = envLines.length > 0 ? `${envLines.join("\n")}\n` : ""
    if (server.transport === "stdio") {
      const cmd = `${server.command || "<command>"} ${(server.args ?? []).join(" ")}`.trim()
      if (errorCode === "not_found") {
        const bin = (server.command || "").split(/\s+/)[0] || "<command>"
        return `${prefix}which ${bin} || echo "Install ${bin} and add it to PATH"`
      }
      return `${prefix}${cmd || "<command> <args>"}`
    }
    const url = server.url || "<url>"
    if (server.transport === "ws") return `${prefix}wscat -c ${url}`
    return `${prefix}curl -i ${url}`
  }

  const copyMcpRemediationCommand = async (server: McpServerEntry, errorCode?: string) => {
    const command = remediationCommandForMcp(server, errorCode)
    try {
      await navigator.clipboard.writeText(command)
      setMcpHealthStatus(`Copied remediation command for ${server.name || "server"}.`)
    } catch {
      setMcpHealthStatus(`Could not copy remediation command for ${server.name || "server"}.`)
    }
  }

  const exportMcpReport = () => {
    if (!mcpHealthReport) return
    try {
      const blob = new Blob([JSON.stringify(mcpHealthReport, null, 2)], { type: "application/json" })
      const url = URL.createObjectURL(blob)
      const anchor = document.createElement("a")
      anchor.href = url
      anchor.download = `mmy-mcp-health-${new Date().toISOString().replace(/[:.]/g, "-")}.json`
      anchor.click()
      URL.revokeObjectURL(url)
      setMcpHealthStatus("MCP health report exported.")
    } catch {
      setMcpHealthStatus("Could not export MCP health report.")
    }
  }

  const generateBearerToken = async () => {
    if (rotatingToken) return
    setRotatingToken(true)
    setAuthStatus(null)
    try {
      const result = await rotateServerToken()
      setSettings((s) => ({ ...s, bearerToken: result.token }))
      setFreshToken(result.token)
      setAuthStatus(
        result.token_file
          ? `Generated new bearer token and saved it to ${result.token_file}.`
          : "Generated new bearer token."
      )
    } catch (err) {
      const msg = err instanceof Error ? err.message : "failed"
      setAuthStatus(`Could not generate bearer token (${msg}).`)
    } finally {
      setRotatingToken(false)
    }
  }

  const copyFreshToken = async () => {
    if (!freshToken) return
    try {
      await navigator.clipboard.writeText(freshToken)
      setAuthStatus("Token copied to clipboard.")
    } catch {
      setAuthStatus("Could not copy token to clipboard.")
    } finally {
      setFreshToken(null)
    }
  }

  const saveAdminPassword = async () => {
    if (settingAdminPassword) return
    setSettingAdminPassword(true)
    setAdminPasswordStatus(null)
    try {
      await setAdminPassword(adminPassword)
      setAdminPasswordInput("")
      setAdminPasswordConfigured(true)
      setAdminPasswordStatus("Admin password set.")
    } catch (err) {
      const msg = err instanceof Error ? err.message : "failed"
      setAdminPasswordStatus(`Could not set admin password (${msg}).`)
    } finally {
      setSettingAdminPassword(false)
    }
  }

  const recoverTokenWithPassword = async () => {
    if (recoveringToken) return
    setRecoveringToken(true)
    setAuthStatus(null)
    try {
      const result = await recoverServerToken(adminPassword)
      setSettings((s) => ({ ...s, bearerToken: result.token }))
      setFreshToken(result.token)
      setAdminPasswordInput("")
      setAuthStatus(
        result.token_file
          ? `Recovered token and saved it to ${result.token_file}.`
          : "Recovered token."
      )
    } catch (err) {
      const msg = err instanceof Error ? err.message : "failed"
      setAuthStatus(`Could not recover token (${msg}).`)
    } finally {
      setRecoveringToken(false)
    }
  }

  const updateProvider = (index: number, patch: Partial<{ name: string; model: string; enabled: boolean }>) => {
    setSettings((s) => ({
      ...s,
      providers: s.providers.map((provider, i) => (i === index ? { ...provider, ...patch } : provider)),
    }))
  }

  const updateProviderMonthlyBudget = (providerName: string, rawValue: string) => {
    const key = providerName.trim()
    if (!key) return
    setSettings((s) => {
      const next = { ...(s.providerMonthlyBudgets ?? {}) }
      const trimmed = rawValue.trim()
      if (!trimmed) {
        delete next[key]
      } else {
        const parsed = Number(trimmed)
        if (Number.isFinite(parsed) && parsed >= 0) {
          next[key] = parsed
        }
      }
      return { ...s, providerMonthlyBudgets: next }
    })
  }

  const refreshProviderModels = async (providerName: string) => {
    const name = providerName.trim()
    if (!name) {
      setProviderStatus("Enter a provider name first, then refresh models.")
      return
    }
    try {
      const payload = await fetchProviderModels(name)
      setProviderCatalog((prev) => ({ ...prev, [name]: payload.models }))
      if (payload.models.length > 0) {
        setSettings((s) => ({
          ...s,
          providers: s.providers.map((provider) => {
            if (provider.name.trim() !== name) return provider
            if (payload.models.includes(provider.model)) return provider
            return { ...provider, model: payload.models[0] }
          }),
        }))
      }
      const warning = payload.warnings[0] ? ` (${payload.warnings[0]})` : ""
      setProviderStatus(
        `Loaded ${payload.models.length} model(s) for ${name} from ${payload.source}. Active model: ${
          payload.configured_model || "unknown"
        }${warning}`
      )
    } catch (err) {
      const msg = err instanceof Error ? err.message : "failed"
      setProviderStatus(`Could not load models for ${name} (${msg}).`)
    }
  }

  const addProvider = () => {
    const existing = new Set(
      settings.providers
        .map((provider) => provider.name.trim().toLowerCase())
        .filter(Boolean)
    )
    let suffix = 1
    let candidate = "new-provider"
    while (existing.has(candidate.toLowerCase())) {
      suffix += 1
      candidate = `new-provider-${suffix}`
    }
    setSettings((s) => ({
      ...s,
      providers: [...s.providers, { name: candidate, model: "model-name", enabled: false }],
    }))
  }

  const removeProvider = (index: number) => {
    setSettings((s) => ({
      ...s,
      providers: s.providers.filter((_, i) => i !== index),
    }))
  }

  const updateRoleRouting = (patch: Partial<RoleRoutingConfig>) => {
    setSettings((s) => ({
      ...s,
      roleRouting: {
        ...s.roleRouting,
        ...patch,
      },
    }))
  }

  const roleRoutingProviders = (routing: RoleRoutingConfig): Set<string> => {
    const out = new Set<string>()
    const add = (value: string) => {
      const v = value.trim()
      if (v) out.add(v)
    }
    add(routing.critique.drafter_provider)
    add(routing.critique.critic_provider)
    add(routing.critique.refiner_provider)
    add(routing.debate.debater_a_provider)
    add(routing.debate.debater_b_provider)
    add(routing.debate.judge_provider)
    add(routing.debate.synthesizer_provider)
    add(routing.consensus.adjudicator_provider)
    add(routing.council.synthesizer_provider)
    add(routing.council.specialist_roles.coding)
    add(routing.council.specialist_roles.security)
    add(routing.council.specialist_roles.writing)
    add(routing.council.specialist_roles.factual)
    return out
  }

  const providerOptions = useMemo(
    () => [...new Set(settings.providers.map((provider) => provider.name.trim()).filter(Boolean))],
    [settings.providers]
  )

  const buildAutoFixedRoleRouting = useMemo(() => {
    const enabledProviders = settings.providers
      .filter((provider) => provider.enabled && provider.name.trim())
      .map((provider) => provider.name.trim())
    const allProviders = settings.providers
      .map((provider) => provider.name.trim())
      .filter(Boolean)
    const pool = enabledProviders.length > 0 ? enabledProviders : allProviders
    const fallbackProvider = pool[0] ?? ""
    const pick = (index: number) => {
      if (!pool.length) return ""
      return pool[index % pool.length]
    }

    const next: RoleRoutingConfig = {
      critique: {
        drafter_provider: pick(0),
        critic_provider: pick(1),
        refiner_provider: pick(2),
      },
      debate: {
        debater_a_provider: pick(3),
        debater_b_provider: pick(4),
        judge_provider: pick(5),
        synthesizer_provider: pick(6),
      },
      consensus: {
        adjudicator_provider: pick(7),
      },
      council: {
        specialist_roles: {
          coding: pick(8),
          security: pick(9),
          writing: pick(10),
          factual: pick(11),
        },
        synthesizer_provider: pick(12),
      },
    }
    return { next, fallbackProvider }
  }, [settings.providers, settings.roleRouting])

  const activePolicies = {
    ...demoPolicies,
    routing: {
      ...demoPolicies.routing,
      fallback_chain: settings.providers.filter((p) => p.enabled).map((p) => p.name),
    },
  }

  const loadProvidersFromDaemon = async () => {
    setProviderStatus(null)
    try {
      const providers = await fetchProviderConfig()
      setSettings((s) => ({ ...s, providers }))
      saveSettings({ providers })
      await loadProviderKeysFromDaemon()
      setProviderStatus("Loaded provider config from server.")
    } catch (err) {
      const msg = err instanceof Error ? err.message : "failed"
      setProviderStatus(`Could not load provider config from server (${msg}).`)
    }
  }

  const loadRoleRoutingFromDaemon = async () => {
    setRoleRoutingStatus(null)
    try {
      const roleRouting = await fetchRoleRoutingConfig()
      setSettings((s) => ({ ...s, roleRouting }))
      saveSettings({ roleRouting })
      setRoleRoutingStatus("Loaded role routing from server.")
    } catch (err) {
      const msg = err instanceof Error ? err.message : "failed"
      setRoleRoutingStatus(`Could not load role routing from server (${msg}).`)
    }
  }

  const applyRoleRoutingToDaemon = async () => {
    setRoleRoutingStatus(null)
    try {
      const roleRouting = await applyRoleRoutingConfig(settings.roleRouting)
      setSettings((s) => ({ ...s, roleRouting }))
      saveSettings({ roleRouting })
      setRoleRoutingStatus("Applied role routing to server.")
    } catch (err) {
      const msg = humanizeProviderError(err instanceof Error ? err.message : "failed")
      setRoleRoutingStatus(`Could not apply role routing to server (${msg}).`)
    }
  }

  const autoFixRoleRouting = async () => {
    const { next, fallbackProvider } = buildAutoFixedRoleRouting
    if (!fallbackProvider) {
      setRoleRoutingStatus("Cannot auto-fix routes: no providers configured.")
      return
    }
    setSettings((s) => ({ ...s, roleRouting: next }))
    saveSettings({ roleRouting: next })
    setRoleRoutingStatus(`Auto-fixed role routing across enabled providers (fallback: ${fallbackProvider}).`)
    try {
      const applied = await applyRoleRoutingConfig(next)
      setSettings((s) => ({ ...s, roleRouting: applied }))
      saveSettings({ roleRouting: applied })
      setRoleRoutingStatus(`Auto-fix applied on server with provider balancing (fallback: ${fallbackProvider}).`)
    } catch (err) {
      const msg = humanizeProviderError(err instanceof Error ? err.message : "failed")
      setRoleRoutingStatus(`Auto-fix updated local routes but server apply failed (${msg}).`)
    }
  }

  const applyProvidersToDaemon = async () => {
    setProviderStatus(null)
    try {
      await applyProviderConfig(settings.providers)
      saveSettings({ providers: settings.providers })
      setProviderStatus("Applied provider config to server.")
    } catch (err) {
      const rawMsg = err instanceof Error ? err.message : "failed"
      const msg = humanizeProviderError(rawMsg)
      const lower = rawMsg.toLowerCase()
      if (lower.includes("references disabled provider")) {
        setProviderStatus("Route/provider mismatch detected. Auto-fixing routes and retrying apply...")
        await runAutoFixApplyTransition()
        return
      }
      setProviderStatus(`Could not apply provider config to server (${msg}).`)
    }
  }

  const runAutoFixApplyTransition = async () => {
    if (autoFixApplyPending) return
    const { next, fallbackProvider } = buildAutoFixedRoleRouting
    if (!fallbackProvider) {
      setProviderStatus("Cannot auto-fix routes: no providers configured.")
      setShowAutoFixApplyModal(false)
      return
    }
    setAutoFixApplyPending(true)
    try {
      const requiredDuringTransition = roleRoutingProviders(next)
      const interimProviders = settings.providers.map((provider) => {
        const name = provider.name.trim()
        if (!name) return provider
        return requiredDuringTransition.has(name) ? { ...provider, enabled: true } : provider
      })
      await applyProviderConfig(interimProviders)
      const appliedRoutes = await applyRoleRoutingConfig(next)
      setSettings((s) => ({ ...s, roleRouting: appliedRoutes }))
      saveSettings({ roleRouting: appliedRoutes })
      await applyProviderConfig(settings.providers)
      saveSettings({ providers: settings.providers })
      setProviderStatus(
        `Applied provider config via auto-fix transition (fallback: ${fallbackProvider}).`
      )
      setRoleRoutingStatus(
        `Auto-fixed role routing and reconciled provider state (fallback: ${fallbackProvider}).`
      )
      setShowAutoFixApplyModal(false)
    } catch (retryErr) {
      const retryMsg = humanizeProviderError(retryErr instanceof Error ? retryErr.message : "failed")
      setProviderStatus(`Apply failed after auto-fix attempt (${retryMsg}).`)
    } finally {
      setAutoFixApplyPending(false)
    }
  }

  const updateProviderKeyInput = (providerName: string, value: string) => {
    setProviderKeyInputs((prev) => ({ ...prev, [providerName]: value }))
  }

  const saveProviderKey = async (providerName: string) => {
    const apiKey = (providerKeyInputs[providerName] ?? "").trim()
    if (!providerName || !apiKey) {
      setProviderKeyStatus("Provider and API key are required.")
      return
    }
    setProviderKeyStatus(null)
    try {
      await setProviderApiKey(providerName, apiKey)
      setProviderKeyInputs((prev) => ({ ...prev, [providerName]: "" }))
      await loadProviderKeysFromDaemon()
      setProviderKeyStatus(`Stored API key for ${providerName}.`)
    } catch (err) {
      const msg = err instanceof Error ? err.message : "failed"
      setProviderKeyStatus(`Could not store API key for ${providerName} (${msg}).`)
    }
  }

  const clearKey = async (providerName: string) => {
    if (!providerName) return
    setProviderKeyStatus(null)
    try {
      await clearProviderApiKey(providerName)
      await loadProviderKeysFromDaemon()
      setProviderKeyStatus(`Cleared API key for ${providerName}.`)
    } catch (err) {
      const msg = err instanceof Error ? err.message : "failed"
      setProviderKeyStatus(`Could not clear API key for ${providerName} (${msg}).`)
    }
  }

  const runProviderConnectionTest = async (providerName: string, model?: string) => {
    try {
      const result = await testProviderConnection(providerName, model)
      return { ok: true, message: `Connection OK (${result.latency_ms} ms)` }
    } catch (err) {
      const msg = humanizeProviderError(err instanceof Error ? err.message : "failed")
      return { ok: false, message: `Connection failed (${msg})` }
    }
  }

  const testConnection = async (providerName: string, model?: string) => {
    if (!providerName) return
    setTestingProviders((prev) => ({ ...prev, [providerName]: true }))
    setProviderTestStatus((prev) => ({ ...prev, [providerName]: "Testing..." }))
    try {
      const outcome = await runProviderConnectionTest(providerName, model)
      setProviderTestStatus((prev) => ({ ...prev, [providerName]: outcome.message }))
    } finally {
      setTestingProviders((prev) => ({ ...prev, [providerName]: false }))
    }
  }

  const applyAndSmokeTest = async () => {
    if (smokeRunning) return
    setSmokeRunning(true)
    setSmokeStatus("Applying provider config...")
    setProviderStatus(null)
    try {
      await applyProviderConfig(settings.providers)
      setProviderStatus("Applied provider config to server.")
    } catch (err) {
      const msg = humanizeProviderError(err instanceof Error ? err.message : "failed")
      setSmokeStatus(`Apply failed (${msg}).`)
      setSmokeRunning(false)
      return
    }

    let latestKeys: Record<string, ProviderKeyStatusEntry> = {}
    try {
      const rows = await fetchProviderKeyStatus()
      latestKeys = Object.fromEntries(rows.map((row) => [row.name, row]))
      setProviderKeys(latestKeys)
    } catch {
      latestKeys = providerKeys
    }

    const enabledProviders = settings.providers.filter((provider) => provider.enabled && provider.name.trim())
    if (enabledProviders.length === 0) {
      setSmokeStatus("Applied config. No enabled providers to smoke test.")
      setSmokeRunning(false)
      return
    }

    const runnableProviders = enabledProviders.filter((provider) => latestKeys[provider.name]?.key_set)
    const skippedProviders = enabledProviders.filter((provider) => !latestKeys[provider.name]?.key_set)
    for (const provider of skippedProviders) {
      setProviderTestStatus((prev) => ({
        ...prev,
        [provider.name]: "Skipped (no key set)",
      }))
    }
    if (runnableProviders.length === 0) {
      setSmokeStatus(`Smoke test skipped: 0 tested, ${skippedProviders.length} skipped (no keys set).`)
      setSmokeRunning(false)
      return
    }

    let passed = 0
    let failed = 0
    for (const provider of runnableProviders) {
      const name = provider.name.trim()
      setTestingProviders((prev) => ({ ...prev, [name]: true }))
      setProviderTestStatus((prev) => ({ ...prev, [name]: "Testing..." }))
      const outcome = await runProviderConnectionTest(name, provider.model)
      setProviderTestStatus((prev) => ({ ...prev, [name]: outcome.message }))
      setTestingProviders((prev) => ({ ...prev, [name]: false }))
      if (outcome.ok) passed += 1
      else failed += 1
    }
    setSmokeStatus(
      `Smoke test complete: ${passed} passed, ${failed} failed, ${skippedProviders.length} skipped (no keys).`
    )
    setSmokeRunning(false)
  }

  const runSystemSelfTest = async () => {
    if (selfTestRunning) return
    setSelfTestRunning(true)
    setSelfTestResult(null)
    setSelfTestStatus("Checking server auth and health...")
    try {
      await fetchHealth()
    } catch (err) {
      const msg = err instanceof Error ? err.message : "failed"
      const reason = `Self-test failed at health check (${msg}).`
      setSelfTestStatus(reason)
      setSelfTestResult({ status: "fail", reason })
      setSelfTestRunning(false)
      return
    }

    let providersFromDaemon = settings.providers
    let keyRows: ProviderKeyStatusEntry[] = []
    let delegationReachable = false
    let delegationSummary = "delegation unknown"
    try {
      providersFromDaemon = await fetchProviderConfig()
      keyRows = await fetchProviderKeyStatus()
      setSettings((s) => ({ ...s, providers: providersFromDaemon }))
      saveSettings({ providers: providersFromDaemon })
      setProviderKeys(Object.fromEntries(keyRows.map((row) => [row.name, row])))
    } catch (err) {
      const msg = err instanceof Error ? err.message : "failed"
      const reason = `Self-test failed while loading provider state (${msg}).`
      setSelfTestStatus(reason)
      setSelfTestResult({ status: "fail", reason })
      setSelfTestRunning(false)
      return
    }

    try {
      const status = await fetchDelegationHealth()
      delegationReachable = status.reachable
      delegationSummary = status.reachable ? "delegation OK" : `delegation ${status.status}`
      if (status.reachable) {
        setDelegateHealthStatus(`Delegation service online (${status.socket_path}).`)
      } else {
        const detail = status.detail ? `: ${status.detail}` : ""
        setDelegateHealthStatus(`Delegation service ${status.status}${detail} (${status.socket_path}).`)
      }
    } catch (err) {
      const msg = err instanceof Error ? err.message : "failed"
      delegationSummary = `delegation check failed (${msg})`
      setDelegateHealthStatus(`Could not check delegation service (${msg}).`)
    }

    const keyMap = Object.fromEntries(keyRows.map((row) => [row.name, row]))
    const enabledProviders = providersFromDaemon.filter((provider) => provider.enabled && provider.name.trim())
    const testableProviders = enabledProviders.filter((provider) => keyMap[provider.name]?.key_set)
    const skippedProviders = enabledProviders.filter((provider) => !keyMap[provider.name]?.key_set)

    let passed = 0
    let failed = 0
    for (const provider of testableProviders) {
      const outcome = await runProviderConnectionTest(provider.name, provider.model)
      setProviderTestStatus((prev) => ({ ...prev, [provider.name]: outcome.message }))
      if (outcome.ok) passed += 1
      else failed += 1
    }
    for (const provider of skippedProviders) {
      setProviderTestStatus((prev) => ({ ...prev, [provider.name]: "Skipped (no key set)" }))
    }
    const finalMessage =
      `Self-test complete: auth OK, ${delegationSummary}, ${passed} provider tests passed, ${failed} failed, ${skippedProviders.length} skipped (no keys).` +
      (delegationReachable ? "" : " Start the delegation service if you plan to use delegate jobs.")
    setSelfTestStatus(finalMessage)
    setSelfTestResult({ status: failed > 0 ? "fail" : "pass", reason: finalMessage })
    void refreshSetupStatus(false)
    setSelfTestRunning(false)
  }

  const runStrictProfileReliabilityTest = async () => {
    if (strictCheckRunning) return
    setStrictCheckRunning(true)
    setStrictCheckStatus("Running strict reliability check...")
    setStrictCheckFailures([])
    setStrictFailuresOpen(false)
    try {
      const enabledProviders = settings.providers.filter((provider) => provider.enabled && provider.name.trim())
      const targetProvider = enabledProviders[0]?.name
      const targetModel = enabledProviders[0]?.model
      const result = await runStrictReliabilityCheck({
        runs: strictCheckRuns,
        provider: targetProvider,
        model: targetModel,
      })
      const report = {
        provider: targetProvider,
        model: targetModel,
        runs: result.runs,
        passed: result.passed,
        failed: result.failed,
        checked_at: new Date().toISOString(),
      }
      saveSettings({ strictReliabilityLastReport: report })
      setSettings((s) => ({ ...s, strictReliabilityLastReport: report }))
      setStrictCheckStatus(
        `Strict reliability: ${result.passed}/${result.runs} passed` +
          (targetProvider ? ` (${targetProvider}${targetModel ? `:${targetModel}` : ""})` : "")
      )
      const failures = result.details.filter((row) => !row.ok).map((row) => ({ index: row.index, answer: row.answer }))
      setStrictCheckFailures(failures)
      setStrictFailuresOpen(failures.length > 0)
    } catch (err) {
      const msg = err instanceof Error ? err.message : "failed"
      setStrictCheckStatus(`Strict reliability check failed (${msg}).`)
    } finally {
      setStrictCheckRunning(false)
    }
  }

  const runDoctor = async (resetView = true) => {
    if (doctorRunning) return
    setDoctorRunning(true)
    setDoctorStatus("Running doctor checks...")
    if (resetView) setDoctorReport(null)
    try {
      const report = await runServerDoctor(doctorSmokeProviders, doctorGovernance)
      setDoctorReport(report)
      setDoctorStatus(
        `Doctor complete: ${report.summary.passed} passed, ${report.summary.failed} failed, ${report.summary.skipped} skipped.`
      )
    } catch (err) {
      const msg = err instanceof Error ? err.message : "failed"
      setDoctorStatus(`Doctor failed (${msg}).`)
    } finally {
      setDoctorRunning(false)
    }
  }

  useEffect(() => {
    if (!doctorModalOpen || !doctorAutoRefresh) return
    const timer = window.setInterval(() => {
      if (doctorRunning) return
      setDoctorRunning(true)
      setDoctorStatus("Running doctor checks...")
      void runServerDoctor(doctorSmokeProviders, doctorGovernance)
        .then((report) => {
          setDoctorReport(report)
          setDoctorStatus(
            `Doctor complete: ${report.summary.passed} passed, ${report.summary.failed} failed, ${report.summary.skipped} skipped.`
          )
        })
        .catch((err) => {
          const msg = err instanceof Error ? err.message : "failed"
          setDoctorStatus(`Doctor failed (${msg}).`)
        })
        .finally(() => setDoctorRunning(false))
    }, 30000)
    return () => window.clearInterval(timer)
  }, [doctorModalOpen, doctorAutoRefresh, doctorSmokeProviders, doctorGovernance, doctorRunning])

  const exportDoctorReport = () => {
    if (!doctorReport) return
    try {
      const blob = new Blob([JSON.stringify(doctorReport, null, 2)], { type: "application/json" })
      const url = URL.createObjectURL(blob)
      const anchor = document.createElement("a")
      anchor.href = url
      anchor.download = `mmy-doctor-${new Date().toISOString().replace(/[:.]/g, "-")}.json`
      anchor.click()
      URL.revokeObjectURL(url)
      setDoctorStatus("Doctor report exported.")
    } catch {
      setDoctorStatus("Could not export doctor report.")
    }
  }

  const copyStrictFailure = async (item: { index: number; answer: string }) => {
    try {
      await navigator.clipboard.writeText(item.answer)
      setStrictCheckStatus(`Copied failed response #${item.index}.`)
    } catch {
      setStrictCheckStatus(`Could not copy failed response #${item.index}.`)
    }
  }

  const loadSecurityEvents = async () => {
    if (securityEventsLoading) return
    if (adminPasswordConfigured && !securityLogPassword.trim()) {
      setSecurityEventsStatus("Enter admin password in Security Events, then load security log.")
      return
    }
    setSecurityEventsLoading(true)
    setSecurityEventsStatus(null)
    try {
      const rows = await fetchSecurityAuditEvents(securityLogPassword || undefined, 40)
      setSecurityEvents(rows)
      setSecurityVisibleCount(25)
      setSecurityEventsStatus(`Loaded ${rows.length} security event(s).`)
      setSecurityBadgePulse(true)
      window.setTimeout(() => setSecurityBadgePulse(false), 1200)
    } catch (err) {
      const msg = err instanceof Error ? err.message : "failed"
      setSecurityEvents([])
      setSecurityEventsStatus(`Could not load security events (${msg}).`)
    } finally {
      setSecurityEventsLoading(false)
    }
  }

  const exportSecurityEvents = async () => {
    if (securityEventsExporting) return
    if (adminPasswordConfigured && !securityLogPassword.trim()) {
      setSecurityEventsStatus("Enter admin password in Security Events, then export security log.")
      return
    }
    setSecurityEventsExporting(true)
    setSecurityEventsStatus(null)
    try {
      const rows = await fetchSecurityAuditEvents(securityLogPassword || undefined, 200)
      const blob = new Blob(
        [
          JSON.stringify(
            {
              exported_at: new Date().toISOString(),
              count: rows.length,
              events: rows,
            },
            null,
            2
          ),
        ],
        { type: "application/json" }
      )
      const url = URL.createObjectURL(blob)
      const anchor = document.createElement("a")
      anchor.href = url
      anchor.download = `mmy-security-events-${new Date().toISOString().replace(/[:.]/g, "-")}.json`
      anchor.click()
      URL.revokeObjectURL(url)
      setSecurityEventsStatus(`Exported ${rows.length} security event(s).`)
    } catch (err) {
      const msg = err instanceof Error ? err.message : "failed"
      setSecurityEventsStatus(`Could not export security events (${msg}).`)
    } finally {
      setSecurityEventsExporting(false)
    }
  }

  const filteredSecurityEvents = useMemo(() => {
    const byType = securityEvents.filter((event) => {
      if (securityEventFilter === "all") return true
      if (securityEventFilter === "ops") {
        return (
          event.event_type.startsWith("ui.export_") ||
          event.event_type.startsWith("artifact_export_") ||
          event.event_type.startsWith("artifact_delete_") ||
          event.event_type.startsWith("delegate_job_delete_")
        )
      }
      if (securityEventFilter === "remote") {
        return event.event_type.startsWith("remote_access_")
      }
      if (securityEventFilter === "auth") {
        return event.event_type.startsWith("admin_password_verify_") || event.event_type.startsWith("admin_password_recover_")
      }
      if (securityEventFilter === "lockout") return event.event_type.includes("locked")
      return true
    })
    if (!securityErrorsOnly) return byType
    return byType.filter((event) => event.event_type.includes("failed") || event.event_type.includes("locked"))
  }, [securityEvents, securityEventFilter, securityErrorsOnly])

  const visibleSecurityEvents = useMemo(
    () => filteredSecurityEvents.slice(0, securityVisibleCount),
    [filteredSecurityEvents, securityVisibleCount]
  )

  const formatSecurityTimestamp = (value: string | null | undefined) => {
    const text = (value ?? "").trim()
    if (!text) return "unknown-time"
    const parsed = new Date(text)
    if (Number.isNaN(parsed.getTime())) return text
    return parsed.toLocaleString()
  }

  const securityFilterCounts = useMemo(() => {
    const base = securityErrorsOnly
      ? securityEvents.filter((event) => event.event_type.includes("failed") || event.event_type.includes("locked"))
      : securityEvents
    return {
      all: base.length,
      ops: base.filter(
        (event) =>
          event.event_type.startsWith("ui.export_") ||
          event.event_type.startsWith("artifact_export_") ||
          event.event_type.startsWith("artifact_delete_") ||
          event.event_type.startsWith("delegate_job_delete_")
      ).length,
      remote: base.filter((event) => event.event_type.startsWith("remote_access_")).length,
      auth: base.filter(
        (event) =>
          event.event_type.startsWith("admin_password_verify_") || event.event_type.startsWith("admin_password_recover_")
      ).length,
      lockout: base.filter((event) => event.event_type.includes("locked")).length,
    }
  }, [securityEvents, securityErrorsOnly])

  const copySecurityEvent = async (event: SecurityAuditEvent) => {
    try {
      await navigator.clipboard.writeText(JSON.stringify(event, null, 2))
      setSecurityEventsStatus("Copied event JSON to clipboard.")
    } catch {
      setSecurityEventsStatus("Could not copy event JSON.")
    }
  }

  return (
    <div className="p-6">
      <div className="mb-6">
        <h1 className="font-mono text-lg font-bold tracking-wider text-foreground">
          SETTINGS
        </h1>
        <p className="text-sm text-muted-foreground">
          Configuration and system preferences
        </p>
        {profileSyncStatus && <p className={statusMessageClass(profileSyncStatus, "mt-1 font-mono text-[10px]")}>{profileSyncStatus}</p>}
      </div>

      <div className={cn("mx-auto space-y-6", activeTab === "providers" ? "max-w-7xl" : "max-w-2xl")}>
        <section className="glass-card rounded-lg p-3">
          <div className="mb-2 flex items-center justify-between">
            <div className="font-mono text-[10px] text-muted-foreground">
              {setupStatus?.ready ? "Setup complete" : "Setup incomplete"}
            </div>
            <button
              type="button"
              onClick={() => {
                setSetupWizardOpen(true)
                void refreshSetupStatus(false)
              }}
              className="rounded border border-neon-cyan/40 bg-neon-cyan/10 px-2 py-1 font-mono text-[10px] text-neon-cyan hover:bg-neon-cyan/20"
            >
              Open Setup Wizard
            </button>
          </div>
          <div className="grid gap-2 sm:grid-cols-4">
            {([
              { key: "connection", label: "Connection" },
              { key: "appearance", label: "Appearance" },
              { key: "providers", label: "Providers" },
              { key: "security", label: "Security" },
            ] as const).map((tab) => (
              <button
                key={tab.key}
                type="button"
                onClick={() => setActiveTab(tab.key)}
                className={cn(
                  "rounded border px-3 py-2 font-mono text-[11px] uppercase transition-all",
                  activeTab === tab.key
                    ? "border-neon-cyan/50 bg-neon-cyan/10 text-neon-cyan"
                    : "border-border text-muted-foreground hover:text-foreground"
                )}
              >
                {tab.label}
              </button>
            ))}
          </div>
        </section>

        {/* API Connection */}
        {activeTab === "connection" && <section className="glass-card rounded-lg p-5">
          <div className="mb-4 flex items-center gap-2">
            <Globe className="h-4 w-4 text-neon-cyan" />
            <h2 className="font-mono text-sm font-bold tracking-wider text-foreground">
              API CONNECTION
            </h2>
          </div>
          <div className="space-y-3">
            <div>
              <label
                className="mb-1 block font-mono text-[10px] text-muted-foreground"
                htmlFor="base-url"
              >
                BASE URL
              </label>
              <input
                id="base-url"
                type="text"
                value={settings.apiBaseUrl}
                onChange={(e) =>
                  setSettings((s) => ({ ...s, apiBaseUrl: e.target.value }))
                }
                className="neon-border w-full rounded-md bg-background/50 px-3 py-2 font-mono text-sm text-foreground focus:outline-none focus:glow-cyan"
              />
            </div>
            <div>
              <label
                className="mb-1 block font-mono text-[10px] text-muted-foreground"
                htmlFor="bearer-token"
              >
                BEARER TOKEN
              </label>
              <div className="flex gap-2">
                <div className="neon-border flex flex-1 items-center rounded-md bg-background/50">
                  <Key className="ml-3 h-3 w-3 text-muted-foreground" />
                  <input
                    id="bearer-token"
                    type="password"
                    value={settings.bearerToken}
                    onChange={(e) =>
                      setSettings((s) => ({
                        ...s,
                        bearerToken: e.target.value,
                      }))
                    }
                    placeholder="Enter bearer token..."
                    className="flex-1 bg-transparent px-3 py-2 font-mono text-sm text-foreground placeholder:text-muted-foreground/50 focus:outline-none"
                  />
                </div>
                <button
                  type="button"
                  onClick={() => void generateBearerToken()}
                  disabled={rotatingToken}
                  className="rounded-md border border-neon-yellow/40 bg-neon-yellow/10 px-2.5 py-1.5 font-mono text-[10px] text-neon-yellow hover:bg-neon-yellow/20 disabled:opacity-50"
                >
                  GENERATE
                </button>
              </div>
              <div className="mt-1 flex items-center gap-2">
                {authStatus && <p className={statusMessageClass(authStatus, "font-mono text-[10px]")}>{authStatus}</p>}
                {freshToken && (
                  <button
                    type="button"
                    onClick={() => void copyFreshToken()}
                    className="rounded border border-border px-2 py-0.5 font-mono text-[10px] text-muted-foreground hover:text-foreground"
                  >
                    COPY TOKEN
                  </button>
                )}
              </div>
              <div className="mt-3 rounded-md border border-border bg-background/40 p-2">
                <p className="mb-2 font-mono text-[10px] text-muted-foreground">ASSISTANT PROFILE</p>
                <div className="grid gap-2">
                  <input
                    type="text"
                    value={settings.assistantName}
                    onChange={(e) =>
                      setSettings((s) => ({
                        ...s,
                        assistantName: e.target.value,
                      }))
                    }
                    placeholder="Assistant name (e.g., CerbiBot)"
                    className="rounded border border-border bg-background/60 px-2 py-1 font-mono text-xs text-foreground placeholder:text-muted-foreground/50 focus:outline-none"
                  />
                  <textarea
                    value={settings.assistantInstructions}
                    onChange={(e) =>
                      setSettings((s) => ({
                        ...s,
                        assistantInstructions: e.target.value,
                      }))
                    }
                    placeholder="Behavior/style instructions for the assistant..."
                    rows={3}
                    className="rounded border border-border bg-background/60 px-2 py-1 font-mono text-xs text-foreground placeholder:text-muted-foreground/50 focus:outline-none"
                  />
                  <label className="inline-flex items-center gap-2 font-mono text-[10px] text-muted-foreground">
                    <input
                      type="checkbox"
                      checked={settings.assistantStrictProfile}
                      onChange={(e) =>
                        setSettings((s) => ({
                          ...s,
                          assistantStrictProfile: e.target.checked,
                        }))
                      }
                      className="h-3 w-3 accent-[var(--neon-cyan)]"
                    />
                    STRICT PROFILE (retry once if format is not followed)
                  </label>
                  <label className="inline-flex items-center gap-2 font-mono text-[10px] text-muted-foreground">
                    <input
                      type="checkbox"
                      checked={settings.debugRetrievalWarnings}
                      onChange={(e) =>
                        setSettings((s) => ({
                          ...s,
                          debugRetrievalWarnings: e.target.checked,
                        }))
                      }
                      className="h-3 w-3 accent-[var(--neon-yellow)]"
                    />
                    DEBUG RETRIEVAL WARNINGS
                  </label>
                  <label className="grid gap-1 font-mono text-[10px] text-muted-foreground">
                    <span>WEB MAX SOURCES ({settings.webMaxSources})</span>
                    <input
                      type="range"
                      min={1}
                      max={10}
                      step={1}
                      value={settings.webMaxSources}
                      onChange={(e) =>
                        setSettings((s) => ({
                          ...s,
                          webMaxSources: Number(e.target.value),
                        }))
                      }
                      className="accent-[var(--neon-cyan)]"
                    />
                  </label>
                  <label className="grid gap-1 font-mono text-[10px] text-muted-foreground">
                    <span>WEB ASSIST MODE</span>
                    <select
                      value={settings.webAssistMode}
                      onChange={(e) => {
                        const value = e.target.value as "off" | "auto" | "confirm"
                        const next: AppSettings = { ...settings, webAssistMode: value }
                        setSettings(next)
                        void persistSettingsSnapshot(next, `Web assist mode set to ${value}.`)
                      }}
                      className="rounded border border-border bg-background/60 px-2 py-1 font-mono text-xs text-foreground focus:outline-none"
                    >
                      <option value="off">Off</option>
                      <option value="auto">Auto (search when needed)</option>
                      <option value="confirm">Ask before search</option>
                    </select>
                  </label>
                  <label className="grid gap-1 font-mono text-[10px] text-muted-foreground">
                    <span>WEB ANSWER STYLE</span>
                    <select
                      value={settings.retrievalAnswerStyle}
                      onChange={(e) => {
                        const value = e.target.value as "concise_ranked" | "full_details" | "source_first"
                        const next: AppSettings = { ...settings, retrievalAnswerStyle: value }
                        setSettings(next)
                        void persistSettingsSnapshot(next, `Web answer style set to ${value}.`)
                      }}
                      className="rounded border border-border bg-background/60 px-2 py-1 font-mono text-xs text-foreground focus:outline-none"
                    >
                      <option value="concise_ranked">Concise (ranked)</option>
                      <option value="full_details">Detailed (actionable)</option>
                      <option value="source_first">Source-first</option>
                    </select>
                  </label>
                </div>
              </div>
              <div className="mt-3 rounded-md border border-border bg-background/40 p-3">
                <div className="mb-2 flex items-center justify-between gap-2">
                  <div>
                    <p className="font-mono text-[10px] text-muted-foreground">REMOTE ACCESS ASSISTANT</p>
                    <p className="font-mono text-[10px] text-muted-foreground/80">
                      Saves a reviewed exposure plan with exact launch and rollback commands. It does not open ports or create tunnels automatically.
                    </p>
                  </div>
                  <div className="font-mono text-[10px] text-muted-foreground">
                    {remoteAccessStatus?.enabled ? "plan active" : "plan inactive"}
                  </div>
                </div>
                {remoteAccessMessage && (
                  <p className={statusMessageClass(remoteAccessMessage, "mb-2 font-mono text-[10px]")}>{remoteAccessMessage}</p>
                )}
                <div className="grid gap-2 md:grid-cols-2">
                  <label className="grid gap-1 font-mono text-[10px] text-muted-foreground">
                    <span>EXPOSURE MODE</span>
                    <select
                      value={remoteMode}
                      onChange={(e) => setRemoteMode(e.target.value as "lan" | "tailscale" | "cloudflare" | "manual_proxy")}
                      className="rounded border border-border bg-background/60 px-2 py-1 font-mono text-xs text-foreground focus:outline-none"
                    >
                      <option value="lan">LAN / direct port</option>
                      <option value="tailscale">Tailscale / private mesh</option>
                      <option value="cloudflare">Cloudflare Tunnel</option>
                      <option value="manual_proxy">Manual reverse proxy</option>
                    </select>
                  </label>
                  <label className="grid gap-1 font-mono text-[10px] text-muted-foreground">
                    <span>PUBLIC BASE URL</span>
                    <input
                      type="text"
                      value={remotePublicUrl}
                      onChange={(e) => setRemotePublicUrl(e.target.value)}
                      placeholder="https://assistant.example.com"
                      className="rounded border border-border bg-background/60 px-2 py-1 font-mono text-xs text-foreground placeholder:text-muted-foreground/50 focus:outline-none"
                    />
                  </label>
                  <label className="grid gap-1 font-mono text-[10px] text-muted-foreground">
                    <span>BIND HOST</span>
                    <input
                      type="text"
                      value={remoteBindHost}
                      onChange={(e) => setRemoteBindHost(e.target.value)}
                      placeholder="127.0.0.1 or 0.0.0.0"
                      className="rounded border border-border bg-background/60 px-2 py-1 font-mono text-xs text-foreground placeholder:text-muted-foreground/50 focus:outline-none"
                    />
                  </label>
                  <label className="grid gap-1 font-mono text-[10px] text-muted-foreground">
                    <span>BIND PORT</span>
                    <input
                      type="number"
                      min={1}
                      max={65535}
                      value={remoteBindPort}
                      onChange={(e) => setRemoteBindPort(e.target.value)}
                      className="rounded border border-border bg-background/60 px-2 py-1 font-mono text-xs text-foreground focus:outline-none"
                    />
                  </label>
                </div>
                <label className="mt-2 grid gap-1 font-mono text-[10px] text-muted-foreground">
                  <span>NOTES</span>
                  <textarea
                    rows={2}
                    value={remoteNotes}
                    onChange={(e) => setRemoteNotes(e.target.value)}
                    placeholder="Proxy hostname, firewall notes, or rollout reminders..."
                    className="rounded border border-border bg-background/60 px-2 py-1 font-mono text-xs text-foreground placeholder:text-muted-foreground/50 focus:outline-none"
                  />
                </label>
                <div className="mt-2 flex flex-wrap gap-2">
                  <button
                    type="button"
                    onClick={() => void applyRemoteAccessPlan()}
                    disabled={remoteAccessBusy}
                    className="rounded border border-neon-cyan/40 bg-neon-cyan/10 px-2 py-1 font-mono text-[10px] text-neon-cyan hover:bg-neon-cyan/20 disabled:opacity-50"
                  >
                    {remoteAccessBusy ? "Saving..." : remoteAccessStatus?.enabled ? "Rebind Plan" : "Enable Plan"}
                  </button>
                  <button
                    type="button"
                    onClick={() => void revokeRemoteAccessPlan()}
                    disabled={remoteAccessBusy || !remoteAccessStatus?.enabled}
                    className="rounded border border-neon-red/40 bg-neon-red/10 px-2 py-1 font-mono text-[10px] text-neon-red hover:bg-neon-red/20 disabled:opacity-50"
                  >
                    Revoke Plan
                  </button>
                  <button
                    type="button"
                    onClick={() => void loadRemoteAccessStatus()}
                    disabled={remoteAccessBusy}
                    className="rounded border border-border px-2 py-1 font-mono text-[10px] text-muted-foreground hover:text-foreground disabled:opacity-50"
                  >
                    Refresh
                  </button>
                  <button
                    type="button"
                    onClick={() => void runRemoteHealthProbe()}
                    disabled={remoteAccessBusy || remoteAccessHealthBusy}
                    className="rounded border border-neon-yellow/40 bg-neon-yellow/10 px-2 py-1 font-mono text-[10px] text-neon-yellow hover:bg-neon-yellow/20 disabled:opacity-50"
                  >
                    {remoteAccessHealthBusy ? "Probing..." : "Run Probe"}
                  </button>
                </div>
                {remoteAccessStatus && (
                  <div className="mt-3 grid gap-3 md:grid-cols-2">
                    <div className="rounded border border-border/60 bg-background/30 p-2">
                      <div className="mb-1 flex items-center justify-between gap-2">
                        <p className="font-mono text-[10px] text-muted-foreground">LAUNCH COMMAND</p>
                        <button
                          type="button"
                          onClick={() => void copyRemoteCommand(remoteAccessStatus.launch_command, "Launch command")}
                          className="rounded border border-border px-2 py-0.5 font-mono text-[10px] text-muted-foreground hover:text-foreground"
                        >
                          COPY
                        </button>
                      </div>
                      <pre className="overflow-x-auto whitespace-pre-wrap font-mono text-[10px] text-foreground">{remoteAccessStatus.launch_command}</pre>
                    </div>
                    <div className="rounded border border-border/60 bg-background/30 p-2">
                      <div className="mb-1 flex items-center justify-between gap-2">
                        <p className="font-mono text-[10px] text-muted-foreground">ROLLBACK COMMAND</p>
                        <button
                          type="button"
                          onClick={() => void copyRemoteCommand(remoteAccessStatus.rollback_command, "Rollback command")}
                          className="rounded border border-border px-2 py-0.5 font-mono text-[10px] text-muted-foreground hover:text-foreground"
                        >
                          COPY
                        </button>
                      </div>
                      <pre className="overflow-x-auto whitespace-pre-wrap font-mono text-[10px] text-foreground">{remoteAccessStatus.rollback_command}</pre>
                    </div>
                    <div className="rounded border border-border/60 bg-background/30 p-2">
                      <p className="mb-1 font-mono text-[10px] text-muted-foreground">CHECKLIST</p>
                      <div className="space-y-1">
                        {remoteAccessStatus.steps.map((step, idx) => (
                          <p key={`remote-step-${idx}`} className="font-mono text-[10px] text-foreground/90">
                            {idx + 1}. {step}
                          </p>
                        ))}
                      </div>
                    </div>
                    <div className="rounded border border-border/60 bg-background/30 p-2">
                      <p className="mb-1 font-mono text-[10px] text-muted-foreground">SAFETY NOTES</p>
                      <div className="space-y-1">
                        {remoteAccessStatus.warnings.map((warning, idx) => (
                          <p key={`remote-warning-${idx}`} className="font-mono text-[10px] text-foreground/90">
                            {warning}
                          </p>
                        ))}
                      </div>
                      <p className="mt-2 font-mono text-[10px] text-muted-foreground">
                        Mode: {remoteAccessStatus.mode_label}
                        {remoteAccessStatus.public_url ? ` | URL: ${remoteAccessStatus.public_url}` : ""}
                      </p>
                    </div>
                    <div className="rounded border border-border/60 bg-background/30 p-2 md:col-span-2">
                      <p className="mb-1 font-mono text-[10px] text-muted-foreground">HEALTH PROBE</p>
                      {!remoteAccessHealth ? (
                        <p className="font-mono text-[10px] text-muted-foreground">
                          No probe run yet. Use Run Probe after saving the plan and rebinding the daemon.
                        </p>
                      ) : (
                        <div className="space-y-2">
                          <p className="font-mono text-[10px] text-muted-foreground">
                            PASS {remoteAccessHealth.summary.passed} / FAIL {remoteAccessHealth.summary.failed} / SKIP {remoteAccessHealth.summary.skipped}
                          </p>
                          <div className="grid gap-2 md:grid-cols-2">
                            {remoteAccessHealth.checks.map((check) => (
                              <div key={check.name} className="rounded border border-border bg-background/40 p-2">
                                <p className="font-mono text-[10px] text-foreground">
                                  {check.name} [{check.status}]
                                </p>
                                <p className="font-mono text-[10px] text-muted-foreground">{check.url || "n/a"}</p>
                                <p className="font-mono text-[10px] text-muted-foreground">{check.detail}</p>
                                <p className="font-mono text-[10px] text-foreground/90">{check.remediation}</p>
                                <p className="font-mono text-[10px] text-muted-foreground">{check.latency_ms} ms</p>
                              </div>
                            ))}
                          </div>
                        </div>
                      )}
                    </div>
                  </div>
                )}
              </div>
              <div className="mt-3 rounded-md border border-border bg-background/40 p-2">
                <div className="mb-2 flex items-center justify-between gap-2">
                  <div>
                    <p className="font-mono text-[10px] text-muted-foreground">MCP SERVERS</p>
                    <p className="font-mono text-[10px] text-muted-foreground/80">
                      Configure Model Context Protocol servers and test tool availability.
                    </p>
                  </div>
                  <div className="flex flex-wrap items-center gap-2">
                    <button
                      type="button"
                      onClick={() => addMcpTemplate("filesystem")}
                      className="rounded border border-border px-2 py-1 font-mono text-[10px] text-muted-foreground hover:text-foreground"
                    >
                      + Filesystem
                    </button>
                    <button
                      type="button"
                      onClick={() => addMcpTemplate("git")}
                      className="rounded border border-border px-2 py-1 font-mono text-[10px] text-muted-foreground hover:text-foreground"
                    >
                      + Git
                    </button>
                    <button
                      type="button"
                      onClick={() => addMcpTemplate("fetch")}
                      className="rounded border border-border px-2 py-1 font-mono text-[10px] text-muted-foreground hover:text-foreground"
                    >
                      + Fetch
                    </button>
                    <button
                      type="button"
                      onClick={addMcpServer}
                      className="rounded border border-neon-cyan/40 bg-neon-cyan/10 px-2 py-1 font-mono text-[10px] text-neon-cyan hover:bg-neon-cyan/20"
                    >
                      + Add MCP Server
                    </button>
                  </div>
                </div>
                <div className="mb-2 rounded border border-border/60 bg-background/30 px-2 py-1">
                  <p className="font-mono text-[10px] text-muted-foreground">
                    Quick templates: start with Filesystem/Git/Fetch, then adjust command/args or URL.
                  </p>
                </div>
                <div className="space-y-2">
                  {(settings.mcpServers ?? []).length === 0 && (
                    <p className="font-mono text-[10px] text-muted-foreground">
                      No MCP servers configured yet. Add one to test connectivity and tool inventory.
                    </p>
                  )}
                  {(settings.mcpServers ?? []).map((server, idx) => (
                    <div key={`mcp-server-${idx}`} className="rounded border border-border bg-background/60 p-2">
                      <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
                        <div className="flex items-center gap-2">
                          <label className="inline-flex items-center gap-1 font-mono text-[10px] text-muted-foreground">
                            <input
                              type="checkbox"
                              checked={Boolean(server.enabled)}
                              onChange={(e) => patchMcpServer(idx, { enabled: e.target.checked })}
                              className="h-3 w-3"
                            />
                            enabled
                          </label>
                          <button
                            type="button"
                            onClick={() => void runMcpServerHealthCheck(server.name)}
                            disabled={!server.name.trim() || Boolean(mcpServerTesting[server.name])}
                            className="rounded border border-neon-cyan/40 bg-neon-cyan/10 px-2 py-1 font-mono text-[10px] text-neon-cyan hover:bg-neon-cyan/20 disabled:opacity-50"
                          >
                            {mcpServerTesting[server.name] ? "Testing..." : "Test Server"}
                          </button>
                          <button
                            type="button"
                            onClick={() => void copyMcpRemediationCommand(server)}
                            className="rounded border border-border px-2 py-1 font-mono text-[10px] text-muted-foreground hover:text-foreground"
                          >
                            Copy Fix Command
                          </button>
                        </div>
                        <button
                          type="button"
                          onClick={() => removeMcpServer(idx)}
                          className="rounded border border-neon-red/40 bg-neon-red/10 px-2 py-1 font-mono text-[10px] text-neon-red hover:bg-neon-red/20"
                        >
                          Remove
                        </button>
                      </div>
                      <div className="grid gap-2 md:grid-cols-3">
                        <input
                          type="text"
                          value={server.name}
                          onChange={(e) => patchMcpServer(idx, { name: e.target.value })}
                          placeholder="name (e.g. filesystem)"
                          className="rounded border border-border bg-background/60 px-2 py-1 font-mono text-xs text-foreground placeholder:text-muted-foreground/50 focus:outline-none"
                        />
                        <select
                          value={server.transport}
                          onChange={(e) =>
                            patchMcpServer(idx, {
                              transport: e.target.value as "stdio" | "http" | "sse" | "ws",
                            })
                          }
                          className="rounded border border-border bg-background/60 px-2 py-1 font-mono text-xs text-foreground focus:outline-none"
                        >
                          <option value="stdio">stdio</option>
                          <option value="http">http</option>
                          <option value="sse">sse</option>
                          <option value="ws">ws</option>
                        </select>
                        <input
                          type="text"
                          value={(server.declared_tools ?? []).join(", ")}
                          onChange={(e) =>
                            patchMcpServer(idx, {
                              declared_tools: e.target.value
                                .split(",")
                                .map((chunk) => chunk.trim())
                                .filter(Boolean),
                            })
                          }
                          placeholder="declared tools (comma separated)"
                          className="rounded border border-border bg-background/60 px-2 py-1 font-mono text-xs text-foreground placeholder:text-muted-foreground/50 focus:outline-none"
                        />
                      </div>
                      {server.transport === "stdio" ? (
                        <div className="mt-2 grid gap-2 md:grid-cols-2">
                          <input
                            type="text"
                            value={server.command}
                            onChange={(e) => patchMcpServer(idx, { command: e.target.value })}
                            placeholder="command (e.g. npx @modelcontextprotocol/server-filesystem)"
                            className="rounded border border-border bg-background/60 px-2 py-1 font-mono text-xs text-foreground placeholder:text-muted-foreground/50 focus:outline-none"
                          />
                          <input
                            type="text"
                            value={(server.args ?? []).join(" ")}
                            onChange={(e) =>
                              patchMcpServer(idx, {
                                args: e.target.value
                                  .split(/\s+/)
                                  .map((chunk) => chunk.trim())
                                  .filter(Boolean),
                              })
                            }
                            placeholder="args (space separated)"
                            className="rounded border border-border bg-background/60 px-2 py-1 font-mono text-xs text-foreground placeholder:text-muted-foreground/50 focus:outline-none"
                          />
                        </div>
                      ) : (
                        <div className="mt-2">
                          <input
                            type="text"
                            value={server.url}
                            onChange={(e) => patchMcpServer(idx, { url: e.target.value })}
                            placeholder="URL (e.g. http://127.0.0.1:3001)"
                            className="w-full rounded border border-border bg-background/60 px-2 py-1 font-mono text-xs text-foreground placeholder:text-muted-foreground/50 focus:outline-none"
                          />
                        </div>
                      )}
                      <div className="mt-2 grid gap-2 md:grid-cols-2">
                        <label className="grid gap-1 font-mono text-[10px] text-muted-foreground">
                          <span>HTTP HEADERS (one per line: Header: value)</span>
                          <textarea
                            value={formatKeyValueLines(server.headers)}
                            onChange={(e) =>
                              patchMcpServer(idx, {
                                headers: parseKeyValueLines(e.target.value),
                              })
                            }
                            rows={3}
                            className="rounded border border-border bg-background/60 px-2 py-1 font-mono text-xs text-foreground placeholder:text-muted-foreground/50 focus:outline-none"
                          />
                        </label>
                        <label className="grid gap-1 font-mono text-[10px] text-muted-foreground">
                          <span>HEADER ENV REFS (Header: ENV_VAR)</span>
                          <textarea
                            value={formatKeyValueLines(server.header_env_refs)}
                            onChange={(e) =>
                              patchMcpServer(idx, {
                                header_env_refs: parseKeyValueLines(e.target.value),
                              })
                            }
                            rows={3}
                            className="rounded border border-border bg-background/60 px-2 py-1 font-mono text-xs text-foreground placeholder:text-muted-foreground/50 focus:outline-none"
                          />
                        </label>
                      </div>
                    </div>
                  ))}
                </div>
                <div className="mt-2 flex flex-wrap items-center gap-2">
                  <button
                    type="button"
                    onClick={() => void saveMcpServerConfig()}
                    className="rounded border border-neon-green/40 bg-neon-green/10 px-2.5 py-1.5 font-mono text-[10px] text-neon-green hover:bg-neon-green/20"
                  >
                    Save MCP Servers
                  </button>
                  <button
                    type="button"
                    onClick={() => void runMcpHealthChecks()}
                    disabled={mcpHealthRunning}
                    className="rounded border border-neon-cyan/40 bg-neon-cyan/10 px-2.5 py-1.5 font-mono text-[10px] text-neon-cyan hover:bg-neon-cyan/20 disabled:opacity-50"
                  >
                    {mcpHealthRunning ? "Running..." : "Run MCP Health Check"}
                  </button>
                  <button
                    type="button"
                    onClick={() => setMcpModalOpen(true)}
                    disabled={!mcpHealthReport}
                    className="rounded border border-border px-2 py-1 font-mono text-[10px] text-muted-foreground hover:text-foreground disabled:opacity-50"
                  >
                    Show Report
                  </button>
                  <button
                    type="button"
                    onClick={exportMcpReport}
                    disabled={!mcpHealthReport}
                    className="rounded border border-border px-2 py-1 font-mono text-[10px] text-muted-foreground hover:text-foreground disabled:opacity-50"
                  >
                    Export
                  </button>
                </div>
                {mcpStatus && <p className={statusMessageClass(mcpStatus, "mt-1 font-mono text-[10px]")}>{mcpStatus}</p>}
                {mcpHealthStatus && (
                  <p className={statusMessageClass(mcpHealthStatus, "mt-1 font-mono text-[10px]")}>{mcpHealthStatus}</p>
                )}
                {mcpHealthReport && (
                  <p className="mt-1 font-mono text-[10px] text-muted-foreground">
                    Latest MCP report: {mcpHealthReport.summary.passed} pass / {mcpHealthReport.summary.failed} fail /{" "}
                    {mcpHealthReport.summary.skipped} skip.
                  </p>
                )}
              </div>
              <div className="mt-3 rounded-md border border-border bg-background/40 p-2">
                <div className="mb-1 flex items-center gap-2">
                  <p className="font-mono text-[10px] text-muted-foreground">ADMIN PASSWORD</p>
                  <span
                    className={cn(
                      "inline-flex rounded border px-1.5 py-0.5 font-mono text-[10px]",
                      adminPasswordConfigured === true
                        ? "border-neon-green/40 bg-neon-green/10 text-neon-green"
                        : "border-neon-red/40 bg-neon-red/10 text-neon-red"
                    )}
                  >
                    {adminPasswordConfigured === true ? "Configured" : "Not configured"}
                  </span>
                </div>
                <div className="flex flex-wrap gap-2">
                  <input
                    type="password"
                    value={adminPassword}
                    onChange={(e) => setAdminPasswordInput(e.target.value)}
                    placeholder="Set or enter admin password..."
                    className="min-w-[220px] flex-1 rounded border border-border bg-background/60 px-2 py-1 font-mono text-xs text-foreground placeholder:text-muted-foreground/50 focus:outline-none"
                  />
                  <button
                    type="button"
                    onClick={() => void saveAdminPassword()}
                    disabled={settingAdminPassword || !adminPassword}
                    className="rounded border border-neon-cyan/40 bg-neon-cyan/10 px-2.5 py-1.5 font-mono text-[10px] text-neon-cyan hover:bg-neon-cyan/20 disabled:opacity-50"
                  >
                    SET PASSWORD
                  </button>
                  <button
                    type="button"
                    onClick={() => void recoverTokenWithPassword()}
                    disabled={recoveringToken || !adminPassword}
                    className="rounded border border-neon-yellow/40 bg-neon-yellow/10 px-2.5 py-1.5 font-mono text-[10px] text-neon-yellow hover:bg-neon-yellow/20 disabled:opacity-50"
                  >
                    RECOVER TOKEN
                  </button>
                </div>
                {adminPasswordStatus && (
                  <p className={statusMessageClass(adminPasswordStatus, "mt-1 font-mono text-[10px]")}>{adminPasswordStatus}</p>
                )}
              </div>
              <div className="mt-2">
                <button
                  type="button"
                  onClick={() => void runSystemSelfTest()}
                  disabled={selfTestRunning}
                  className="rounded-md border border-neon-green/40 bg-neon-green/10 px-2.5 py-1.5 font-mono text-[10px] text-neon-green hover:bg-neon-green/20 disabled:opacity-50"
                >
                  Run Quick Check
                </button>
                {selfTestStatus && <p className={statusMessageClass(selfTestStatus, "mt-1 font-mono text-[10px]")}>{selfTestStatus}</p>}
                <div className="mt-2">
                  <button
                    type="button"
                    onClick={() => void runDoctor()}
                    disabled={doctorRunning}
                    className="rounded-md border border-neon-cyan/40 bg-neon-cyan/10 px-2.5 py-1.5 font-mono text-[10px] text-neon-cyan hover:bg-neon-cyan/20 disabled:opacity-50"
                  >
                    Run System Check
                  </button>
                  {doctorStatus && <p className={statusMessageClass(doctorStatus, "mt-1 font-mono text-[10px]")}>{doctorStatus}</p>}
                  <div className="mt-2 flex flex-wrap items-center gap-2">
                    <label className="inline-flex items-center gap-1 font-mono text-[10px] text-muted-foreground">
                      <input
                        type="checkbox"
                        checked={doctorSmokeProviders}
                        onChange={(e) => setDoctorSmokeProviders(e.target.checked)}
                        className="h-3 w-3"
                      />
                      Provider smoke
                    </label>
                    <label className="inline-flex items-center gap-1 font-mono text-[10px] text-muted-foreground">
                      <input
                        type="checkbox"
                        checked={doctorGovernance}
                        onChange={(e) => setDoctorGovernance(e.target.checked)}
                        className="h-3 w-3"
                      />
                      Governance
                    </label>
                    <button
                      type="button"
                      onClick={() => setDoctorModalOpen(true)}
                      disabled={!doctorReport}
                      className="rounded-md border border-border px-2 py-1 font-mono text-[10px] text-muted-foreground hover:text-foreground disabled:opacity-50"
                    >
                      SHOW REPORT
                    </button>
                    <button
                      type="button"
                      onClick={exportDoctorReport}
                      disabled={!doctorReport}
                      className="rounded-md border border-border px-2 py-1 font-mono text-[10px] text-muted-foreground hover:text-foreground disabled:opacity-50"
                    >
                      EXPORT
                    </button>
                  </div>
                </div>
                <div className="mt-2 flex flex-wrap items-center gap-2">
                  <button
                    type="button"
                    onClick={() => void checkDelegationDaemonHealth()}
                    disabled={delegateHealthRunning}
                    className="rounded-md border border-neon-cyan/40 bg-neon-cyan/10 px-2.5 py-1.5 font-mono text-[10px] text-neon-cyan hover:bg-neon-cyan/20 disabled:opacity-50"
                  >
                    Check Delegation Service
                  </button>
                  {delegateHealthStatus && (
                    <p className={statusMessageClass(delegateHealthStatus, "font-mono text-[10px]")}>{delegateHealthStatus}</p>
                  )}
                </div>
                <div className="mt-2 rounded-md border border-border bg-background/40 p-2">
                  <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
                    <p className="font-mono text-[10px] text-muted-foreground">
                      Delegation Jobs
                      {delegateJobsCount != null ? ` (${delegateJobsCount})` : ""}
                    </p>
                    <button
                      type="button"
                      onClick={() => void refreshDelegationJobsCount()}
                      disabled={delegateJobsBusy}
                      className="rounded border border-border px-2 py-1 font-mono text-[10px] text-muted-foreground hover:text-foreground disabled:opacity-50"
                    >
                      Refresh Count
                    </button>
                  </div>
                  <div className="flex flex-wrap items-center gap-2">
                    <input
                      type="text"
                      value={delegateDeleteJobId}
                      onChange={(e) => setDelegateDeleteJobId(e.target.value)}
                      placeholder="job-xxxxxxxxxxxx"
                      className="min-w-[220px] flex-1 rounded border border-border bg-background/60 px-2 py-1 font-mono text-xs text-foreground placeholder:text-muted-foreground/50 focus:outline-none"
                    />
                    <button
                      type="button"
                      onClick={() => void deleteSingleDelegationJob()}
                      disabled={delegateJobsBusy || !delegateDeleteJobId.trim()}
                      className="rounded border border-neon-red/40 bg-neon-red/10 px-2.5 py-1.5 font-mono text-[10px] text-neon-red hover:bg-neon-red/20 disabled:opacity-50"
                    >
                      Delete Job
                    </button>
                    <button
                      type="button"
                      onClick={() => setDelegateDeleteOlderOpen(true)}
                      disabled={delegateJobsBusy}
                      className="rounded border border-neon-red/40 bg-neon-red/10 px-2.5 py-1.5 font-mono text-[10px] text-neon-red hover:bg-neon-red/20 disabled:opacity-50"
                    >
                      Delete Older...
                    </button>
                  </div>
                  <p className="mt-2 font-mono text-[10px] text-muted-foreground">
                    Use admin password above if configured.
                  </p>
                  {delegateJobsStatus && (
                    <p className={statusMessageClass(delegateJobsStatus, "mt-1 font-mono text-[10px]")}>{delegateJobsStatus}</p>
                  )}
                </div>
              </div>
              <div className="mt-3 rounded-md border border-border bg-background/40 p-2">
                <div className="mb-2 flex items-center justify-between gap-2">
                  <p className="font-mono text-[10px] text-muted-foreground">STRICT PROFILE RELIABILITY</p>
                  <div className="flex items-center gap-2">
                    <span className="font-mono text-[10px] text-muted-foreground">runs</span>
                    <input
                      type="number"
                      min={1}
                      max={20}
                      value={strictCheckRuns}
                      onChange={(e) => setStrictCheckRuns(Math.max(1, Math.min(20, Number(e.target.value) || 1)))}
                      className="w-14 rounded border border-border bg-background/60 px-1.5 py-0.5 font-mono text-[10px] text-foreground focus:outline-none"
                    />
                  </div>
                </div>
                <button
                  type="button"
                  onClick={() => void runStrictProfileReliabilityTest()}
                  disabled={strictCheckRunning}
                  className="rounded-md border border-neon-yellow/40 bg-neon-yellow/10 px-2.5 py-1.5 font-mono text-[10px] text-neon-yellow hover:bg-neon-yellow/20 disabled:opacity-50"
                >
                  Run Strict Reliability Check
                </button>
                {strictCheckStatus && <p className={statusMessageClass(strictCheckStatus, "mt-1 font-mono text-[10px]")}>{strictCheckStatus}</p>}
                {settings.strictReliabilityLastReport && (
                  <p className="mt-1 font-mono text-[10px] text-muted-foreground">
                    Last: {settings.strictReliabilityLastReport.passed}/{settings.strictReliabilityLastReport.runs} passed
                    {settings.strictReliabilityLastReport.provider
                      ? ` (${settings.strictReliabilityLastReport.provider}${
                          settings.strictReliabilityLastReport.model ? `:${settings.strictReliabilityLastReport.model}` : ""
                        })`
                      : ""}
                  </p>
                )}
                {strictCheckFailures.length > 0 && (
                  <div className="mt-2 rounded border border-neon-red/30 bg-neon-red/5 p-2">
                    <div className="flex items-center justify-between gap-2">
                      <p className="font-mono text-[10px] text-neon-red">
                        Failures: {strictCheckFailures.length}
                      </p>
                      <button
                        type="button"
                        onClick={() => setStrictFailuresOpen((v) => !v)}
                        className="rounded border border-border px-1.5 py-0.5 font-mono text-[10px] text-muted-foreground hover:text-foreground"
                      >
                        {strictFailuresOpen ? "Hide Failures" : "View Failures"}
                      </button>
                    </div>
                    {strictFailuresOpen && (
                      <div className="mt-2 max-h-48 space-y-2 overflow-auto pr-1">
                        {strictCheckFailures.map((item) => (
                          <div key={`strict-failure-${item.index}`} className="rounded border border-border bg-background/40 p-2">
                            <div className="mb-1 flex items-center justify-between gap-2">
                              <span className="font-mono text-[10px] text-muted-foreground">
                                Run #{item.index}
                              </span>
                              <button
                                type="button"
                                onClick={() => void copyStrictFailure(item)}
                                className="rounded border border-border px-1.5 py-0.5 font-mono text-[10px] text-muted-foreground hover:text-foreground"
                              >
                                Copy
                              </button>
                            </div>
                            <pre className="whitespace-pre-wrap break-words font-mono text-[10px] text-foreground">
                              {item.answer}
                            </pre>
                          </div>
                        ))}
                      </div>
                    )}
                  </div>
                )}
              </div>
            </div>
          </div>
        </section>}

        {/* Theme */}
        {activeTab === "appearance" && <section className="glass-card rounded-lg p-5">
          <div className="mb-4 flex items-center gap-2">
            <Palette className="h-4 w-4 text-neon-magenta" />
            <h2 className="font-mono text-sm font-bold tracking-wider text-foreground">
              THEME
            </h2>
          </div>
          <div className="flex gap-3">
            {(["cyberpunk", "stealth", "light"] as const).map((theme) => (
              <button
                key={theme}
                type="button"
                onClick={() => setSettings((s) => ({ ...s, theme }))}
                className={cn(
                  "flex-1 rounded-md border px-4 py-3 font-mono text-xs uppercase transition-all",
                  settings.theme === theme
                    ? theme === "cyberpunk"
                      ? "border-neon-cyan/50 bg-neon-cyan/10 text-neon-cyan glow-cyan"
                      : theme === "stealth"
                        ? "border-foreground/30 bg-foreground/5 text-foreground"
                        : "border-neon-yellow/40 bg-neon-yellow/10 text-neon-yellow"
                    : "border-border text-muted-foreground hover:border-border hover:text-foreground"
                )}
              >
                {theme}
                {theme === "cyberpunk" && (
                  <p className="mt-1 text-[9px] normal-case text-muted-foreground">
                    Neon accents, grid bg
                  </p>
                )}
                {theme === "stealth" && (
                  <p className="mt-1 text-[9px] normal-case text-muted-foreground">
                    Muted tactical palette
                  </p>
                )}
                {theme === "light" && (
                  <p className="mt-1 text-[9px] normal-case text-muted-foreground">
                    High-contrast light mode
                  </p>
                )}
              </button>
            ))}
          </div>

          <div className="mt-4 space-y-2">
            <span className="font-mono text-[10px] text-muted-foreground">THEME PREVIEW</span>
            <div className="grid gap-2 sm:grid-cols-3">
              {(["cyberpunk", "stealth", "light"] as const).map((theme) => (
                <button
                  key={`preview-${theme}`}
                  type="button"
                  onClick={() => setSettings((s) => ({ ...s, theme }))}
                  className={cn(
                    "overflow-hidden rounded-md border text-left transition-all",
                    settings.theme === theme
                      ? "border-neon-cyan/50 shadow-[0_0_8px_rgba(0,240,255,0.2)]"
                      : "border-border hover:border-neon-cyan/30"
                  )}
                >
                  <div className={cn("p-2", theme === "cyberpunk" ? "theme-cyberpunk" : theme === "stealth" ? "theme-stealth" : "theme-light")}>
                    <div className="rounded border border-border bg-card p-2">
                      <div className="mb-2 flex items-center justify-between">
                        <span className="font-mono text-[10px] text-foreground">{theme}</span>
                        <span className="h-2 w-2 rounded-full bg-neon-cyan" />
                      </div>
                      <div className="mb-2 h-1.5 w-full rounded bg-secondary" />
                      <div className="mb-2 h-1.5 w-2/3 rounded bg-muted" />
                      <div className="inline-flex rounded border border-neon-cyan/40 bg-neon-cyan/10 px-1.5 py-0.5 font-mono text-[8px] text-neon-cyan">
                        SAMPLE
                      </div>
                    </div>
                  </div>
                </button>
              ))}
            </div>
          </div>
        </section>}

        {/* Layout */}
        {activeTab === "appearance" && <section className="glass-card rounded-lg p-5">
          <div className="mb-4 flex items-center gap-2">
            <Palette className="h-4 w-4 text-neon-cyan" />
            <h2 className="font-mono text-sm font-bold tracking-wider text-foreground">
              LAYOUT
            </h2>
          </div>
          <div className="space-y-2">
            <span className="font-mono text-[10px] text-muted-foreground">SESSION PANEL</span>
            <div className="flex gap-2">
              {(["left", "right"] as const).map((side) => (
                <button
                  key={side}
                  type="button"
                  onClick={() => setSettings((s) => ({ ...s, sessionPanelSide: side }))}
                  className={cn(
                    "flex-1 rounded-md border px-3 py-2 font-mono text-xs uppercase transition-all",
                    settings.sessionPanelSide === side
                      ? "border-neon-cyan/50 bg-neon-cyan/10 text-neon-cyan"
                      : "border-border text-muted-foreground hover:text-foreground"
                  )}
                >
                  {side}
                </button>
              ))}
            </div>
            <span className="mt-2 block font-mono text-[10px] text-muted-foreground">SESSION ORDER</span>
            <div className="flex gap-2">
              {(["newest", "oldest"] as const).map((order) => (
                <button
                  key={order}
                  type="button"
                  onClick={() => setSettings((s) => ({ ...s, sessionSortOrder: order }))}
                  className={cn(
                    "flex-1 rounded-md border px-3 py-2 font-mono text-xs uppercase transition-all",
                    settings.sessionSortOrder === order
                      ? "border-neon-cyan/50 bg-neon-cyan/10 text-neon-cyan"
                      : "border-border text-muted-foreground hover:text-foreground"
                  )}
                >
                  {order}
                </button>
              ))}
            </div>
          </div>
        </section>}

        {/* Provider layout */}
        {activeTab === "providers" && <div className="mx-auto grid w-full gap-4 xl:grid-cols-2">
          <section className="glass-card rounded-lg p-5">
            <div className="mb-4 flex items-center justify-between gap-2">
              <div className="flex items-center gap-2">
                <Shield className="h-4 w-4 text-neon-green" />
                <h2 className="font-mono text-sm font-bold tracking-wider text-foreground">
                  PROVIDER CONFIG
                </h2>
              </div>
              <button
                type="button"
                onClick={addProvider}
                className="inline-flex items-center gap-1 rounded-md border border-neon-cyan/40 bg-neon-cyan/10 px-2 py-1 font-mono text-[10px] text-neon-cyan hover:bg-neon-cyan/20"
              >
                <Plus className="h-3 w-3" />
                ADD
              </button>
            </div>
            <div className="grid gap-3">
              {settings.providers.map((provider, index) => (
                <div
                  key={`${provider.name}-${index}`}
                  className="h-full rounded-md border border-border bg-background/30 px-3 py-2"
                >
                  <div className="mb-2 flex items-center justify-between">
                    <div className="flex items-center gap-2">
                      <div
                        className={cn(
                          "h-2 w-2 rounded-full",
                          provider.enabled
                            ? "bg-neon-green shadow-[0_0_4px_rgba(0,255,136,0.5)]"
                            : "bg-neon-red"
                        )}
                      />
                      <span className="font-mono text-[10px] text-muted-foreground">provider</span>
                    </div>
                    <button
                      type="button"
                      onClick={() => removeProvider(index)}
                      className="text-neon-red/80 hover:text-neon-red"
                      aria-label={`Remove provider ${provider.name}`}
                    >
                      <Trash2 className="h-3.5 w-3.5" />
                    </button>
                  </div>
                  <div className="grid gap-2 2xl:grid-cols-2">
                    <input
                      type="text"
                      value={provider.name}
                      onChange={(e) => updateProvider(index, { name: e.target.value })}
                      className="rounded border border-border bg-background/60 px-2 py-1 font-mono text-xs text-foreground focus:outline-none"
                      placeholder="provider id"
                    />
                    <input
                      type="text"
                      list={`models-${provider.name || "provider"}-${index}`}
                      value={provider.model}
                      onChange={(e) => updateProvider(index, { model: e.target.value })}
                      className="rounded border border-border bg-background/60 px-2 py-1 font-mono text-xs text-foreground focus:outline-none"
                      placeholder="model"
                    />
                    <datalist id={`models-${provider.name || "provider"}-${index}`}>
                      {(providerCatalog[provider.name] ?? []).map((modelName) => (
                        <option key={modelName} value={modelName} />
                      ))}
                    </datalist>
                  </div>
                  <div className="mt-2">
                    <label className="font-mono text-[10px] text-muted-foreground">
                      Monthly budget (USD)
                      <input
                        type="number"
                        min="0"
                        step="0.01"
                        value={provider.name.trim() ? (settings.providerMonthlyBudgets?.[provider.name.trim()] ?? "") : ""}
                        onChange={(e) => updateProviderMonthlyBudget(provider.name, e.target.value)}
                        className="mt-1 w-full rounded border border-border bg-background/60 px-2 py-1 font-mono text-xs text-foreground focus:outline-none"
                        placeholder="0.00"
                      />
                    </label>
                  </div>
                  {(providerCatalog[provider.name] ?? []).length > 0 && (
                    <div className="mt-2">
                      <label className="font-mono text-[10px] text-muted-foreground">
                        Detected models
                        <select
                          value={
                            (providerCatalog[provider.name] ?? []).includes(provider.model)
                              ? provider.model
                              : ""
                          }
                          onChange={(e) => updateProvider(index, { model: e.target.value })}
                          className="mt-1 w-full rounded border border-border bg-background/60 px-2 py-1 font-mono text-xs text-foreground focus:outline-none"
                        >
                          <option value="" disabled>
                            Select a detected model...
                          </option>
                          {(providerCatalog[provider.name] ?? []).map((modelName) => (
                            <option key={`detected-${provider.name}-${modelName}`} value={modelName}>
                              {modelName}
                            </option>
                          ))}
                        </select>
                      </label>
                    </div>
                  )}
                  <div className="mt-2 flex justify-end">
                    <button
                      type="button"
                      onClick={() => void refreshProviderModels(provider.name)}
                      className="rounded border border-border px-2 py-1 font-mono text-[10px] text-muted-foreground hover:text-foreground"
                    >
                      Refresh Models
                    </button>
                  </div>
                  <div className="mt-1 flex items-center justify-end gap-2">
                    <span className="font-mono text-[10px] text-muted-foreground">enabled</span>
                    <button
                      type="button"
                      role="switch"
                      aria-checked={provider.enabled}
                      onClick={() => updateProvider(index, { enabled: !provider.enabled })}
                      className={cn(
                        "relative h-5 w-10 rounded-full transition-colors self-center",
                        provider.enabled ? "bg-neon-cyan/30" : "bg-secondary"
                      )}
                    >
                      <span
                        className={cn(
                          "absolute top-0.5 left-0.5 h-4 w-4 rounded-full transition-all",
                          provider.enabled ? "translate-x-5 bg-neon-cyan" : "bg-muted-foreground"
                        )}
                      />
                    </button>
                  </div>
                  <div className="mt-2 grid gap-2">
                    <div className="flex items-center gap-2 rounded border border-border bg-background/60 px-2 py-1">
                      <Lock className="h-3.5 w-3.5 text-muted-foreground" />
                      <input
                        type="password"
                        value={providerKeyInputs[provider.name] ?? ""}
                        onChange={(e) => updateProviderKeyInput(provider.name, e.target.value)}
                        className="w-full bg-transparent font-mono text-xs text-foreground placeholder:text-muted-foreground/50 focus:outline-none"
                        placeholder={`Set ${provider.name} API key`}
                      />
                    </div>
                    <div className="flex flex-wrap gap-2">
                    <button
                      type="button"
                      onClick={() => void saveProviderKey(provider.name)}
                      className="rounded border border-neon-cyan/40 bg-neon-cyan/10 px-2 py-1 font-mono text-[10px] text-neon-cyan hover:bg-neon-cyan/20"
                    >
                      SAVE KEY
                    </button>
                    <button
                      type="button"
                      onClick={() => void clearKey(provider.name)}
                      className="rounded border border-border px-2 py-1 font-mono text-[10px] text-muted-foreground hover:text-foreground"
                    >
                      CLEAR
                    </button>
                    <button
                      type="button"
                      disabled={Boolean(testingProviders[provider.name])}
                      onClick={() => void testConnection(provider.name, provider.model)}
                      className="rounded border border-neon-green/40 bg-neon-green/10 px-2 py-1 font-mono text-[10px] text-neon-green hover:bg-neon-green/20 disabled:opacity-50"
                    >
                      TEST
                    </button>
                    </div>
                  </div>
                  <div className="mt-1 flex flex-col gap-1 sm:flex-row sm:items-center sm:justify-between">
                    <span
                      className={cn(
                        "inline-flex w-fit rounded border px-1.5 py-0.5 font-mono text-[10px]",
                        providerKeys[provider.name]?.key_set
                          ? "border-neon-green/40 bg-neon-green/10 text-neon-green"
                          : "border-neon-red/40 bg-neon-red/10 text-neon-red"
                      )}
                    >
                      {providerKeys[provider.name]?.key_set
                        ? `Key set (${providerKeys[provider.name]?.source ?? "unknown"})`
                        : "Key missing"}
                    </span>
                    <span className="break-all font-mono text-[10px] text-muted-foreground">
                      env: {providerKeys[provider.name]?.api_key_env ?? "unknown"}
                    </span>
                  </div>
                  {providerTestStatus[provider.name] && (
                    <p className="mt-1 font-mono text-[10px] text-muted-foreground">
                      {providerTestStatus[provider.name]}
                    </p>
                  )}
                </div>
              ))}
            </div>
            <div className="mt-3 flex gap-2">
              <button
                type="button"
                onClick={() => void loadProvidersFromDaemon()}
                className="rounded-md border border-border px-2.5 py-1.5 font-mono text-[10px] text-muted-foreground hover:text-foreground"
              >
                Load From Server
              </button>
              <button
                type="button"
                onClick={() => void applyProvidersToDaemon()}
                className="rounded-md border border-neon-cyan/40 bg-neon-cyan/10 px-2.5 py-1.5 font-mono text-[10px] text-neon-cyan hover:bg-neon-cyan/20"
              >
                Apply To Server
              </button>
              <button
                type="button"
                disabled={smokeRunning}
                onClick={() => void applyAndSmokeTest()}
                className="rounded-md border border-neon-green/40 bg-neon-green/10 px-2.5 py-1.5 font-mono text-[10px] text-neon-green hover:bg-neon-green/20 disabled:opacity-50"
              >
                Apply + Test
              </button>
              <button
                type="button"
                onClick={() => void loadProviderKeysFromDaemon()}
                className="inline-flex items-center gap-1 rounded-md border border-border px-2.5 py-1.5 font-mono text-[10px] text-muted-foreground hover:text-foreground"
              >
                <RefreshCw className="h-3 w-3" />
                Refresh Keys
              </button>
            </div>
            {providerStatus && <p className={statusMessageClass(providerStatus, "mt-2 font-mono text-[10px]")}>{providerStatus}</p>}
            {providerKeyStatus && <p className={statusMessageClass(providerKeyStatus, "mt-1 font-mono text-[10px]")}>{providerKeyStatus}</p>}
            {smokeStatus && (
              <p className="mt-1 font-mono text-[10px] text-muted-foreground">{smokeStatus}</p>
            )}
          </section>

          <section className="glass-card rounded-lg p-5">
          <div className="mb-4 flex items-center gap-2">
            <Shield className="h-4 w-4 text-neon-cyan" />
            <h2 className="font-mono text-sm font-bold tracking-wider text-foreground">
              ROLE ROUTING
            </h2>
          </div>
          <div className="grid gap-3">
            <div className="grid gap-2 sm:grid-cols-3">
              <label className="font-mono text-[10px] text-muted-foreground">
                Critique drafter
                <select
                  value={settings.roleRouting.critique.drafter_provider}
                  onChange={(e) =>
                    setSettings((s) => ({
                      ...s,
                      roleRouting: {
                        ...s.roleRouting,
                        critique: { ...s.roleRouting.critique, drafter_provider: e.target.value },
                      },
                    }))
                  }
                  className="mt-1 w-full rounded border border-border bg-background/60 px-2 py-1 font-mono text-xs text-foreground focus:outline-none"
                >
                  {providerOptions.map((name) => (
                    <option key={`critique-drafter-${name}`} value={name}>{name}</option>
                  ))}
                </select>
              </label>
              <label className="font-mono text-[10px] text-muted-foreground">
                Critique critic
                <select
                  value={settings.roleRouting.critique.critic_provider}
                  onChange={(e) =>
                    setSettings((s) => ({
                      ...s,
                      roleRouting: {
                        ...s.roleRouting,
                        critique: { ...s.roleRouting.critique, critic_provider: e.target.value },
                      },
                    }))
                  }
                  className="mt-1 w-full rounded border border-border bg-background/60 px-2 py-1 font-mono text-xs text-foreground focus:outline-none"
                >
                  {providerOptions.map((name) => (
                    <option key={`critique-critic-${name}`} value={name}>{name}</option>
                  ))}
                </select>
              </label>
              <label className="font-mono text-[10px] text-muted-foreground">
                Critique refiner
                <select
                  value={settings.roleRouting.critique.refiner_provider}
                  onChange={(e) =>
                    setSettings((s) => ({
                      ...s,
                      roleRouting: {
                        ...s.roleRouting,
                        critique: { ...s.roleRouting.critique, refiner_provider: e.target.value },
                      },
                    }))
                  }
                  className="mt-1 w-full rounded border border-border bg-background/60 px-2 py-1 font-mono text-xs text-foreground focus:outline-none"
                >
                  {providerOptions.map((name) => (
                    <option key={`critique-refiner-${name}`} value={name}>{name}</option>
                  ))}
                </select>
              </label>
            </div>

            <div className="grid gap-2 sm:grid-cols-2">
              <label className="font-mono text-[10px] text-muted-foreground">
                Debate A
                <select
                  value={settings.roleRouting.debate.debater_a_provider}
                  onChange={(e) => updateRoleRouting({ debate: { ...settings.roleRouting.debate, debater_a_provider: e.target.value } })}
                  className="mt-1 w-full rounded border border-border bg-background/60 px-2 py-1 font-mono text-xs text-foreground focus:outline-none"
                >
                  <option value="">Auto</option>
                  {providerOptions.map((name) => (
                    <option key={`debate-a-${name}`} value={name}>{name}</option>
                  ))}
                </select>
              </label>
              <label className="font-mono text-[10px] text-muted-foreground">
                Debate B
                <select
                  value={settings.roleRouting.debate.debater_b_provider}
                  onChange={(e) => updateRoleRouting({ debate: { ...settings.roleRouting.debate, debater_b_provider: e.target.value } })}
                  className="mt-1 w-full rounded border border-border bg-background/60 px-2 py-1 font-mono text-xs text-foreground focus:outline-none"
                >
                  <option value="">Auto</option>
                  {providerOptions.map((name) => (
                    <option key={`debate-b-${name}`} value={name}>{name}</option>
                  ))}
                </select>
              </label>
              <label className="font-mono text-[10px] text-muted-foreground">
                Debate judge
                <select
                  value={settings.roleRouting.debate.judge_provider}
                  onChange={(e) => updateRoleRouting({ debate: { ...settings.roleRouting.debate, judge_provider: e.target.value } })}
                  className="mt-1 w-full rounded border border-border bg-background/60 px-2 py-1 font-mono text-xs text-foreground focus:outline-none"
                >
                  <option value="">Auto</option>
                  {providerOptions.map((name) => (
                    <option key={`debate-judge-${name}`} value={name}>{name}</option>
                  ))}
                </select>
              </label>
              <label className="font-mono text-[10px] text-muted-foreground">
                Debate synthesizer
                <select
                  value={settings.roleRouting.debate.synthesizer_provider}
                  onChange={(e) =>
                    updateRoleRouting({ debate: { ...settings.roleRouting.debate, synthesizer_provider: e.target.value } })
                  }
                  className="mt-1 w-full rounded border border-border bg-background/60 px-2 py-1 font-mono text-xs text-foreground focus:outline-none"
                >
                  <option value="">Auto</option>
                  {providerOptions.map((name) => (
                    <option key={`debate-synth-${name}`} value={name}>{name}</option>
                  ))}
                </select>
              </label>
            </div>

            <div className="grid gap-2 sm:grid-cols-2">
              <label className="font-mono text-[10px] text-muted-foreground">
                Consensus adjudicator
                <select
                  value={settings.roleRouting.consensus.adjudicator_provider}
                  onChange={(e) =>
                    updateRoleRouting({ consensus: { ...settings.roleRouting.consensus, adjudicator_provider: e.target.value } })
                  }
                  className="mt-1 w-full rounded border border-border bg-background/60 px-2 py-1 font-mono text-xs text-foreground focus:outline-none"
                >
                  <option value="">Auto</option>
                  {providerOptions.map((name) => (
                    <option key={`consensus-adj-${name}`} value={name}>{name}</option>
                  ))}
                </select>
              </label>
              <label className="font-mono text-[10px] text-muted-foreground">
                Council synthesizer
                <select
                  value={settings.roleRouting.council.synthesizer_provider}
                  onChange={(e) =>
                    setSettings((s) => ({
                      ...s,
                      roleRouting: {
                        ...s.roleRouting,
                        council: { ...s.roleRouting.council, synthesizer_provider: e.target.value },
                      },
                    }))
                  }
                  className="mt-1 w-full rounded border border-border bg-background/60 px-2 py-1 font-mono text-xs text-foreground focus:outline-none"
                >
                  <option value="">Auto</option>
                  {providerOptions.map((name) => (
                    <option key={`council-synth-${name}`} value={name}>{name}</option>
                  ))}
                </select>
              </label>
            </div>

            <div className="grid gap-2 sm:grid-cols-2">
              {(["coding", "security", "writing", "factual"] as const).map((role) => (
                <label key={`council-role-${role}`} className="font-mono text-[10px] text-muted-foreground">
                  Council {role}
                  <select
                    value={settings.roleRouting.council.specialist_roles[role]}
                    onChange={(e) =>
                      setSettings((s) => ({
                        ...s,
                        roleRouting: {
                          ...s.roleRouting,
                          council: {
                            ...s.roleRouting.council,
                            specialist_roles: {
                              ...s.roleRouting.council.specialist_roles,
                              [role]: e.target.value,
                            },
                          },
                        },
                      }))
                    }
                    className="mt-1 w-full rounded border border-border bg-background/60 px-2 py-1 font-mono text-xs text-foreground focus:outline-none"
                  >
                    <option value="">Auto</option>
                    {providerOptions.map((name) => (
                      <option key={`council-${role}-${name}`} value={name}>{name}</option>
                    ))}
                  </select>
                </label>
              ))}
            </div>
          </div>
          <div className="mt-3 flex gap-2">
            <button
              type="button"
              onClick={() => void loadRoleRoutingFromDaemon()}
              className="rounded-md border border-border px-2.5 py-1.5 font-mono text-[10px] text-muted-foreground hover:text-foreground"
            >
              Load Routes
            </button>
            <button
              type="button"
              onClick={() => void autoFixRoleRouting()}
              className="rounded-md border border-neon-yellow/40 bg-neon-yellow/10 px-2.5 py-1.5 font-mono text-[10px] text-neon-yellow hover:bg-neon-yellow/20"
            >
              Fix Routes
            </button>
            <button
              type="button"
              onClick={() => void applyRoleRoutingToDaemon()}
              className="rounded-md border border-neon-cyan/40 bg-neon-cyan/10 px-2.5 py-1.5 font-mono text-[10px] text-neon-cyan hover:bg-neon-cyan/20"
            >
              Apply Routes
            </button>
          </div>
          {roleRoutingStatus && <p className={statusMessageClass(roleRoutingStatus, "mt-2 font-mono text-[10px]")}>{roleRoutingStatus}</p>}
          </section>

          {/* Policies (read-only) */}
          <section className="glass-card rounded-lg p-5">
          <div className="mb-4 flex items-center gap-2">
            <Shield className="h-4 w-4 text-neon-yellow" />
            <h2 className="font-mono text-sm font-bold tracking-wider text-foreground">
              ACTIVE POLICIES
            </h2>
            <span className="rounded-full border border-border px-2 py-0.5 font-mono text-[9px] text-muted-foreground">
              READ ONLY
            </span>
          </div>
          <pre className="overflow-auto rounded-md bg-background/50 p-3 font-mono text-xs leading-relaxed text-foreground">
            {JSON.stringify(activePolicies, null, 2)}
          </pre>
          </section>

          <section className="glass-card rounded-lg p-5">
          <div className="mb-3 flex items-center gap-2">
            <Shield className="h-4 w-4 text-neon-cyan" />
            <h2 className="font-mono text-sm font-bold tracking-wider text-foreground">
              CHECKER PROFILES
            </h2>
          </div>
          <div className="grid gap-2">
            <div className="rounded border border-border bg-background/40 p-2">
              <p className="font-mono text-[10px] text-muted-foreground">SECURITY GUARDIAN</p>
              <p className="mt-1 font-mono text-xs text-foreground">
                Screens prompts/tool plans/outputs for policy and leakage risk before execution or return.
              </p>
            </div>
            <div className="rounded border border-border bg-background/40 p-2">
              <p className="font-mono text-[10px] text-muted-foreground">FACT CHECKER</p>
              <p className="mt-1 font-mono text-xs text-foreground">
                Verifies draft claims against evidence context and forces repair when unsupported.
              </p>
            </div>
            <div className="rounded border border-neon-yellow/30 bg-neon-yellow/5 p-2">
              <p className="font-mono text-[10px] text-neon-yellow">ENFORCEMENT MODEL</p>
              <p className="mt-1 font-mono text-xs text-foreground">
                Checker models are advisory; deterministic policy gates still decide block/allow for risky actions.
              </p>
            </div>
          </div>
          </section>
        </div>}

        {/* Save button */}
        {activeTab === "security" && <section className="glass-card rounded-lg p-5">
          <div className="mb-3 flex items-center gap-2">
            <Shield className="h-4 w-4 text-neon-red" />
            <h2 className="font-mono text-sm font-bold tracking-wider text-foreground">
              SECURITY EVENTS
            </h2>
          </div>
          <div className="mb-2 flex items-center gap-2">
            <button
              type="button"
              onClick={() => void loadSecurityEvents()}
              disabled={securityEventsLoading}
              className="rounded-md border border-neon-red/40 bg-neon-red/10 px-2.5 py-1.5 font-mono text-[10px] text-neon-red hover:bg-neon-red/20 disabled:opacity-50"
            >
              Load Security Log
            </button>
            <button
              type="button"
              onClick={() => void exportSecurityEvents()}
              disabled={securityEventsExporting}
              className="rounded-md border border-neon-cyan/40 bg-neon-cyan/10 px-2.5 py-1.5 font-mono text-[10px] text-neon-cyan hover:bg-neon-cyan/20 disabled:opacity-50"
            >
              Save Security Log
            </button>
            <span className="font-mono text-[10px] text-muted-foreground">
              {adminPasswordConfigured ? "Admin password required if configured." : "No admin password required."}
            </span>
          </div>
          {adminPasswordConfigured && (
            <div className="mb-2 flex items-center gap-2 rounded border border-border bg-background/40 px-2 py-1.5">
              <Lock className="h-3.5 w-3.5 text-muted-foreground" />
              <input
                type="password"
                value={securityLogPassword}
                onChange={(e) => setSecurityLogPassword(e.target.value)}
                placeholder="Enter admin password for security log access"
                className="w-full bg-transparent font-mono text-xs text-foreground placeholder:text-muted-foreground/50 focus:outline-none"
              />
            </div>
          )}
          <div className="mb-2 flex flex-wrap items-center gap-2">
            <label className="inline-flex items-center gap-1 font-mono text-[10px] text-muted-foreground">
              <input
                type="checkbox"
                checked={securityErrorsOnly}
                onChange={(e) => {
                  setSecurityErrorsOnly(e.target.checked)
                  setSecurityVisibleCount(25)
                }}
                className="h-3 w-3 accent-[var(--neon-red)]"
              />
              Errors Only
            </label>
            {([
              { key: "all", label: "All" },
              { key: "ops", label: "Ops" },
              { key: "remote", label: "Remote" },
              { key: "auth", label: "Auth" },
              { key: "lockout", label: "Lockout" },
            ] as const).map((item) => (
              <button
                key={item.key}
                type="button"
                onClick={() => {
                  setSecurityEventFilter(item.key)
                  setSecurityVisibleCount(25)
                }}
                className={cn(
                  "inline-flex items-center gap-1 rounded border px-2 py-0.5 font-mono text-[10px]",
                  securityEventFilter === item.key
                    ? "border-neon-red/40 bg-neon-red/10 text-neon-red"
                    : "border-border text-muted-foreground hover:text-foreground"
                )}
                >
                  <span>{item.label}</span>
                  <span
                    className={cn(
                      "rounded-full border px-1 py-0 font-mono text-[9px]",
                      securityBadgePulse && "animate-pulse-neon",
                      securityEventFilter === item.key
                        ? "border-neon-red/40 bg-neon-red/10 text-neon-red"
                        : "border-border text-muted-foreground"
                    )}
                  >
                    {securityFilterCounts[item.key]}
                  </span>
                </button>
              ))}
          </div>
          {securityEventsStatus && (
            <p className={statusMessageClass(securityEventsStatus, "mb-2 font-mono text-[10px]")}>{securityEventsStatus}</p>
          )}
          <div className="max-h-64 overflow-auto rounded border border-border bg-background/40 p-2">
            {securityEventsLoading ? (
              <p className="font-mono text-[10px] text-muted-foreground">Loading security events...</p>
            ) : filteredSecurityEvents.length === 0 ? (
              <p className="font-mono text-[10px] text-muted-foreground">No security events match current filters.</p>
            ) : (
              <div className="space-y-2">
                {visibleSecurityEvents.map((event, idx) => (
                  <div key={`${event.timestamp}-${event.event_type}-${idx}`} className="rounded border border-border p-2">
                    <div className="flex items-center justify-between gap-2">
                      <p className="font-mono text-[10px] text-foreground">
                        {formatSecurityTimestamp(event.timestamp)} • {event.event_type}
                      </p>
                      <button
                        type="button"
                        onClick={() => void copySecurityEvent(event)}
                        className="rounded border border-border px-1.5 py-0.5 font-mono text-[10px] text-muted-foreground hover:text-foreground"
                      >
                        Copy JSON
                      </button>
                    </div>
                    <p className="mt-1 break-all font-mono text-[10px] text-muted-foreground">
                      {JSON.stringify(event.payload)}
                    </p>
                  </div>
                ))}
              </div>
            )}
          </div>
          {!securityEventsLoading && filteredSecurityEvents.length > securityVisibleCount && (
            <div className="mt-2 flex items-center justify-between">
              <span className="font-mono text-[10px] text-muted-foreground">
                Showing {visibleSecurityEvents.length} of {filteredSecurityEvents.length}
              </span>
              <button
                type="button"
                onClick={() => setSecurityVisibleCount((prev) => prev + 25)}
                className="rounded border border-border px-2 py-1 font-mono text-[10px] text-muted-foreground hover:text-foreground"
              >
                Load More
              </button>
            </div>
          )}
        </section>}

        <button
          type="button"
          onClick={handleSave}
          className={cn(
            "flex w-full items-center justify-center gap-2 rounded-md border px-4 py-3 font-mono text-sm font-bold transition-all",
            saved
              ? "border-neon-green/50 bg-neon-green/10 text-neon-green"
              : "border-neon-cyan/50 bg-neon-cyan/10 text-neon-cyan hover:bg-neon-cyan/20 hover:shadow-[0_0_12px_rgba(0,240,255,0.3)]"
          )}
        >
          {saved ? (
            <>
              <Check className="h-4 w-4" />
              Saved
            </>
          ) : (
            "Save Settings"
          )}
        </button>
        {showAutoFixApplyModal && (
          <div className="fixed inset-0 z-[80] flex items-center justify-center bg-background/70 px-4 backdrop-blur-sm">
            <div className="w-full max-w-md rounded-lg border border-neon-yellow/40 bg-card p-4 shadow-[0_0_24px_rgba(255,170,0,0.2)]">
              <h3 className="font-mono text-sm font-bold tracking-wider text-foreground">AUTO-FIX ROLE ROUTING</h3>
              <p className="mt-2 font-mono text-xs text-muted-foreground">
                Role routing currently points to a disabled provider. Auto-fix routes to enabled providers and retry apply?
              </p>
              <div className="mt-4 flex justify-end gap-2">
                <button
                  type="button"
                  onClick={() => setShowAutoFixApplyModal(false)}
                  disabled={autoFixApplyPending}
                  className="rounded-md border border-border px-3 py-1.5 font-mono text-[10px] text-muted-foreground hover:text-foreground disabled:opacity-50"
                >
                  Cancel
                </button>
                <button
                  type="button"
                  onClick={() => void runAutoFixApplyTransition()}
                  disabled={autoFixApplyPending}
                  className="rounded-md border border-neon-yellow/40 bg-neon-yellow/10 px-3 py-1.5 font-mono text-[10px] text-neon-yellow hover:bg-neon-yellow/20 disabled:opacity-50"
                >
                  {autoFixApplyPending ? "Applying..." : "Fix And Retry"}
                </button>
              </div>
            </div>
          </div>
        )}
        {setupWizardOpen && (
          <div className="fixed inset-0 z-[82] flex items-center justify-center bg-background/70 px-4 backdrop-blur-sm">
            <div className="w-full max-w-3xl rounded-lg border border-neon-cyan/40 bg-card p-4 shadow-[0_0_24px_rgba(0,240,255,0.18)]">
              <div className="mb-3 flex items-center justify-between gap-2">
                <div>
                  <h3 className="font-mono text-sm font-bold tracking-wider text-foreground">FIRST-RUN SETUP WIZARD</h3>
                  <p className="font-mono text-[10px] text-muted-foreground">
                    {setupStatus?.ready ? "All required checks are complete." : "Complete required checks to finish setup."}
                  </p>
                </div>
                <div className="flex items-center gap-2">
                  <button
                    type="button"
                    onClick={() => void refreshSetupStatus(false)}
                    disabled={setupWizardLoading}
                    className="rounded border border-neon-cyan/40 bg-neon-cyan/10 px-2 py-1 font-mono text-[10px] text-neon-cyan hover:bg-neon-cyan/20 disabled:opacity-50"
                  >
                    {setupWizardLoading ? "Refreshing..." : "Refresh"}
                  </button>
                  <button
                    type="button"
                    onClick={() => setSetupWizardOpen(false)}
                    className="rounded border border-border px-2 py-1 font-mono text-[10px] text-muted-foreground hover:text-foreground"
                  >
                    Close
                  </button>
                </div>
              </div>
              {setupWizardStatus && <p className={statusMessageClass(setupWizardStatus, "mb-2 font-mono text-[10px]")}>{setupWizardStatus}</p>}
              <div className="space-y-2">
                {[
                  {
                    key: "token_configured",
                    label: "Server token configured",
                    ok: Boolean(setupStatus?.checks.token_configured),
                    action: () => setActiveTab("connection"),
                    actionLabel: "Open Connection",
                  },
                  {
                    key: "enabled_provider_present",
                    label: "At least one provider enabled",
                    ok: Boolean(setupStatus?.checks.enabled_provider_present),
                    action: () => setActiveTab("providers"),
                    actionLabel: "Open Providers",
                  },
                  {
                    key: "enabled_provider_has_key",
                    label: "Enabled provider has API key",
                    ok: Boolean(setupStatus?.checks.enabled_provider_has_key),
                    action: () => setActiveTab("providers"),
                    actionLabel: "Open Providers",
                  },
                  {
                    key: "role_routing_valid",
                    label: "Role routing targets enabled providers",
                    ok: Boolean(setupStatus?.checks.role_routing_valid),
                    action: () => void autoFixRoleRouting(),
                    actionLabel: "Fix Routes",
                  },
                  {
                    key: "delegation_reachable",
                    label: "Delegation service reachable (optional)",
                    ok: Boolean(setupStatus?.checks.delegation_reachable),
                    action: () => void checkDelegationDaemonHealth(),
                    actionLabel: "Check Delegation",
                  },
                ].map((item) => (
                  <div key={`setup-step-${item.key}`} className="flex items-center justify-between rounded border border-border bg-background/40 px-3 py-2">
                    <div className="flex items-center gap-2">
                      <span
                        className={cn(
                          "inline-flex h-2.5 w-2.5 rounded-full",
                          item.ok ? "bg-neon-green" : "bg-neon-red"
                        )}
                      />
                      <span className="font-mono text-[11px] text-foreground">{item.label}</span>
                    </div>
                    {!item.ok && (
                      <button
                        type="button"
                        onClick={item.action}
                        className="rounded border border-neon-yellow/40 bg-neon-yellow/10 px-2 py-1 font-mono text-[10px] text-neon-yellow hover:bg-neon-yellow/20"
                      >
                        {item.actionLabel}
                      </button>
                    )}
                  </div>
                ))}
              </div>
              {setupStatus?.details.invalid_routes && setupStatus.details.invalid_routes.length > 0 && (
                <p className="mt-2 font-mono text-[10px] text-neon-yellow">
                  Invalid routes: {setupStatus.details.invalid_routes.join(", ")}
                </p>
              )}
              <div className="mt-3 flex items-center justify-between">
                <div className="font-mono text-[10px] text-muted-foreground">
                  Enabled providers: {setupStatus?.details.enabled_providers.join(", ") || "none"}
                </div>
                <button
                  type="button"
                  onClick={() => void runSystemSelfTest()}
                  disabled={selfTestRunning}
                  className="rounded border border-neon-green/40 bg-neon-green/10 px-2 py-1 font-mono text-[10px] text-neon-green hover:bg-neon-green/20 disabled:opacity-50"
                >
                  Run Quick Check
                </button>
              </div>
              {(selfTestRunning || selfTestResult) && (
                <div className="mt-2 rounded border border-border bg-background/40 px-3 py-2">
                  <div className="mb-1 flex items-center gap-2">
                    <span
                      className={cn(
                        "rounded border px-2 py-0.5 font-mono text-[10px]",
                        selfTestRunning
                          ? "border-neon-cyan/40 bg-neon-cyan/10 text-neon-cyan"
                          : selfTestResult?.status === "pass"
                            ? "border-neon-green/40 bg-neon-green/10 text-neon-green"
                            : "border-neon-red/40 bg-neon-red/10 text-neon-red"
                      )}
                    >
                      {selfTestRunning ? "Running" : selfTestResult?.status === "pass" ? "Pass" : "Fail"}
                    </span>
                    <span className="font-mono text-[10px] text-muted-foreground">Quick self-test result</span>
                  </div>
                  <p className="font-mono text-[10px] text-muted-foreground">
                    {selfTestRunning ? "Running quick self-test..." : selfTestResult?.reason}
                  </p>
                </div>
              )}
            </div>
          </div>
        )}
        {delegateDeleteOlderOpen && (
          <div className="fixed inset-0 z-[84] flex items-center justify-center bg-background/70 px-4 backdrop-blur-sm">
            <div className="w-full max-w-md rounded-lg border border-neon-red/40 bg-card p-4 shadow-[0_0_24px_rgba(255,64,64,0.2)]">
              <h3 className="font-mono text-sm font-bold tracking-wider text-foreground">DELETE Delegation Jobs</h3>
              <p className="mt-2 font-mono text-xs text-muted-foreground">
                Delete jobs older than this many days.
              </p>
              <input
                type="number"
                min={0}
                value={delegateDeleteOlderDays}
                onChange={(e) => setDelegateDeleteOlderDays(e.target.value)}
                className="mt-3 w-full rounded border border-border bg-background/60 px-2 py-1.5 font-mono text-xs text-foreground placeholder:text-muted-foreground/50 focus:outline-none"
              />
              <p className="mt-2 font-mono text-[10px] text-muted-foreground">
                Use <span className="text-foreground">0</span> to delete all jobs.
              </p>
              <div className="mt-4 flex justify-end gap-2">
                <button
                  type="button"
                  onClick={() => setDelegateDeleteOlderOpen(false)}
                  disabled={delegateJobsBusy}
                  className="rounded border border-border px-3 py-1.5 font-mono text-[10px] text-muted-foreground hover:text-foreground disabled:opacity-50"
                >
                  Cancel
                </button>
                <button
                  type="button"
                  onClick={() => void deleteOlderDelegationJobs()}
                  disabled={delegateJobsBusy}
                  className="rounded border border-neon-red/40 bg-neon-red/10 px-3 py-1.5 font-mono text-[10px] text-neon-red hover:bg-neon-red/20 disabled:opacity-50"
                >
                  {delegateJobsBusy ? "Deleting..." : "Delete"}
                </button>
              </div>
            </div>
          </div>
        )}
        {mcpModalOpen && mcpHealthReport && (
          <div className="fixed inset-0 z-[85] flex items-center justify-center bg-background/70 px-4 backdrop-blur-sm">
            <div className="flex max-h-[85vh] w-full max-w-5xl flex-col rounded-lg border border-neon-cyan/40 bg-card p-4 shadow-[0_0_24px_rgba(0,240,255,0.18)]">
              <div className="mb-2 flex items-center justify-between gap-3">
                <div>
                  <h3 className="font-mono text-sm font-bold tracking-wider text-foreground">MCP HEALTH REPORT</h3>
                  <p className="font-mono text-[10px] text-muted-foreground">
                    passed={mcpHealthReport.summary.passed} failed={mcpHealthReport.summary.failed} skipped=
                    {mcpHealthReport.summary.skipped} total={mcpHealthReport.summary.total}
                  </p>
                  <p className="font-mono text-[10px] text-muted-foreground/80">
                    generated: {mcpHealthReport.generated_at || "unknown"}
                  </p>
                </div>
                <div className="flex items-center gap-2">
                  <button
                    type="button"
                    onClick={() => void runMcpHealthChecks()}
                    disabled={mcpHealthRunning}
                    className="rounded border border-neon-cyan/40 bg-neon-cyan/10 px-2 py-1 font-mono text-[10px] text-neon-cyan hover:bg-neon-cyan/20 disabled:opacity-50"
                  >
                    {mcpHealthRunning ? "Running..." : "Refresh"}
                  </button>
                  <button
                    type="button"
                    onClick={exportMcpReport}
                    className="rounded border border-border px-2 py-1 font-mono text-[10px] text-muted-foreground hover:text-foreground"
                  >
                    Export
                  </button>
                  <button
                    type="button"
                    onClick={() => setMcpModalOpen(false)}
                    className="rounded border border-border px-2 py-1 font-mono text-[10px] text-muted-foreground hover:text-foreground"
                  >
                    Close
                  </button>
                </div>
              </div>
              <div className="overflow-auto rounded-md border border-border bg-background/40 p-2">
                <table className="w-full border-collapse font-mono text-[10px]">
                  <thead>
                    <tr className="text-left text-muted-foreground">
                      <th className="pb-1 pr-2">SERVER</th>
                      <th className="pb-1 pr-2">STATUS</th>
                      <th className="pb-1 pr-2">TRANSPORT</th>
                      <th className="pb-1 pr-2">LATENCY</th>
                      <th className="pb-1 pr-2">TOOLS</th>
                      <th className="pb-1 pr-2">DETAIL</th>
                      <th className="pb-1">REMEDIATION</th>
                    </tr>
                  </thead>
                  <tbody>
                    {mcpHealthReport.checks.map((row, idx) => (
                      <tr key={`mcp-modal-${row.name}-${idx}`} className="border-t border-border/50 align-top">
                        <td className="py-1 pr-2 text-foreground">{row.name}</td>
                        <td
                          className={cn(
                            "py-1 pr-2",
                            row.status === "PASS"
                              ? "text-neon-green"
                              : row.status === "SKIP"
                                ? "text-neon-yellow"
                                : "text-neon-red"
                          )}
                        >
                          {formatStatusLabel(row.status)}
                        </td>
                        <td className="py-1 pr-2 text-muted-foreground">{row.transport}</td>
                        <td className="py-1 pr-2 text-muted-foreground">{row.latency_ms}ms</td>
                        <td className="py-1 pr-2 text-muted-foreground">{(row.tools ?? []).join(", ") || "none"}</td>
                        <td className="py-1 pr-2 text-muted-foreground">{row.detail}</td>
                        <td className="py-1 text-muted-foreground">
                          <div className="flex flex-col gap-1">
                            <span>{row.remediation || "-"}</span>
                            {(() => {
                              const server = (settings.mcpServers ?? []).find((entry) => entry.name === row.name)
                              if (!server) return null
                              return (
                                <button
                                  type="button"
                                  onClick={() => void copyMcpRemediationCommand(server, row.error_code)}
                                  className="w-fit rounded border border-border px-1.5 py-0.5 font-mono text-[10px] text-muted-foreground hover:text-foreground"
                                >
                                  COPY COMMAND
                                </button>
                              )
                            })()}
                          </div>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          </div>
        )}
        {doctorModalOpen && doctorReport && (
          <div className="fixed inset-0 z-[85] flex items-center justify-center bg-background/70 px-4 backdrop-blur-sm">
            <div className="flex max-h-[85vh] w-full max-w-5xl flex-col rounded-lg border border-neon-cyan/40 bg-card p-4 shadow-[0_0_24px_rgba(0,240,255,0.18)]">
              <div className="mb-2 flex items-center justify-between gap-3">
                <div>
                  <h3 className="font-mono text-sm font-bold tracking-wider text-foreground">SYSTEM DOCTOR REPORT</h3>
                  <p className="font-mono text-[10px] text-muted-foreground">
                    passed={doctorReport.summary.passed} failed={doctorReport.summary.failed} skipped={doctorReport.summary.skipped} total={doctorReport.summary.total}
                  </p>
                </div>
                <div className="flex items-center gap-2">
                  <label className="inline-flex items-center gap-1 font-mono text-[10px] text-muted-foreground">
                    <input
                      type="checkbox"
                      checked={doctorAutoRefresh}
                      onChange={(e) => setDoctorAutoRefresh(e.target.checked)}
                      className="h-3 w-3"
                    />
                    Auto-refresh (30s)
                  </label>
                  <button
                    type="button"
                    onClick={() => void runDoctor(false)}
                    disabled={doctorRunning}
                    className="rounded border border-neon-cyan/40 bg-neon-cyan/10 px-2 py-1 font-mono text-[10px] text-neon-cyan hover:bg-neon-cyan/20 disabled:opacity-50"
                  >
                    {doctorRunning ? "Running..." : "Refresh"}
                  </button>
                  <button
                    type="button"
                    onClick={exportDoctorReport}
                    className="rounded border border-border px-2 py-1 font-mono text-[10px] text-muted-foreground hover:text-foreground"
                  >
                    Export
                  </button>
                  <button
                    type="button"
                    onClick={() => {
                      setDoctorAutoRefresh(false)
                      setDoctorModalOpen(false)
                    }}
                    className="rounded border border-border px-2 py-1 font-mono text-[10px] text-muted-foreground hover:text-foreground"
                  >
                    Close
                  </button>
                </div>
              </div>
              <div className="overflow-auto rounded-md border border-border bg-background/40 p-2">
                <table className="w-full border-collapse font-mono text-[10px]">
                  <thead>
                    <tr className="text-left text-muted-foreground">
                      <th className="pb-1 pr-2">CHECK</th>
                      <th className="pb-1 pr-2">STATUS</th>
                      <th className="pb-1 pr-2">LATENCY</th>
                      <th className="pb-1">DETAIL</th>
                    </tr>
                  </thead>
                  <tbody>
                    {doctorReport.checks.map((row) => (
                      <tr key={`doctor-modal-${row.name}`} className="border-t border-border/50 align-top">
                        <td className="py-1 pr-2 text-foreground">{row.name}</td>
                        <td
                          className={cn(
                            "py-1 pr-2",
                            row.status === "PASS" ? "text-neon-green" : row.status === "SKIP" ? "text-neon-yellow" : "text-neon-red"
                          )}
                        >
                          {formatStatusLabel(row.status)}
                        </td>
                        <td className="py-1 pr-2 text-muted-foreground">{row.latency_ms}ms</td>
                        <td className="py-1 text-muted-foreground">{row.detail}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          </div>
        )}
      </div>
    </div>
  )
}
