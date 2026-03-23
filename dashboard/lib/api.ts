"use client"

import { getSettings } from "./settings"
import type { ProviderConfigEntry } from "./settings"
import type { RoleRoutingConfig } from "./settings"
import type {
  ChatMessage,
  CostData,
  HealthData,
  MemoryEntry,
  Session,
  Mode,
  ArtifactRun,
  Skill,
  SkillCatalogEntry,
  ToolCall,
  RunInfo,
  RunDag,
  RunTrigger,
} from "./types"

function headers(): HeadersInit {
  const { bearerToken } = getSettings()
  const h: HeadersInit = { "Content-Type": "application/json" }
  if (bearerToken) h["Authorization"] = `Bearer ${bearerToken}`
  return h
}

function baseUrl(): string {
  return getSettings().apiBaseUrl
}

async function throwApiError(res: Response, fallback: string): Promise<never> {
  let detail = ""
  try {
    const payload = (await res.json()) as { detail?: string }
    detail = payload.detail ? String(payload.detail) : ""
  } catch {
    detail = ""
  }
  if (detail) throw new Error(`${fallback}: ${detail}`)
  throw new Error(`${fallback} (HTTP ${res.status})`)
}

// --- Projects ---
export async function fetchProjects(): Promise<string[]> {
  const res = await fetch(`${baseUrl()}/v1/projects`, { headers: headers() })
  if (!res.ok) throw new Error("Failed to fetch projects")
  const payload = (await res.json()) as { projects?: Array<{ project_id?: string }> }
  const ids = (payload.projects ?? [])
    .map((item) => String(item.project_id ?? "").trim())
    .filter(Boolean)
  return ids.length > 0 ? ids : ["default"]
}

// --- Sessions ---
export async function fetchSessions(projectId?: string): Promise<Session[]> {
  const query = new URLSearchParams()
  if (projectId && projectId.trim()) query.set("project_id", projectId.trim())
  const qs = query.toString()
  const res = await fetch(`${baseUrl()}/v1/sessions${qs ? `?${qs}` : ""}`, { headers: headers() })
  if (!res.ok) throw new Error("Failed to fetch sessions")
  const payload = (await res.json()) as {
    sessions?: Array<{ session_id: string; project_id?: string; title?: string; messages?: number }>
  }
  const sessions = payload.sessions ?? []
  return sessions
    .map((item) => ({
      id: item.session_id,
      project_id: item.project_id ? String(item.project_id) : undefined,
      title: item.title || item.session_id,
      message_count: item.messages ?? 0,
    }))
    .reverse()
}

export async function fetchSession(id: string, projectId?: string): Promise<ChatMessage[]> {
  const query = new URLSearchParams()
  if (projectId && projectId.trim()) query.set("project_id", projectId.trim())
  const qs = query.toString()
  const res = await fetch(`${baseUrl()}/v1/sessions/${id}${qs ? `?${qs}` : ""}`, { headers: headers() })
  if (!res.ok) throw new Error("Failed to fetch session")
  const payload = (await res.json()) as {
    session_id?: string
    messages?: Array<{
      role?: "user" | "assistant"
      content?: string
      metadata?: {
        mode?: string
        provider?: string
        tokens?: number
        cost?: number
        status?: string
        warnings?: string[]
        tool_outputs?: Array<Record<string, unknown>>
      }
    }>
  }
  return (payload.messages ?? []).map((msg) => ({
    role: msg.role === "assistant" ? "assistant" : "user",
    content: msg.content ?? "",
    metadata: msg.metadata,
  }))
}

export async function deleteSession(id: string, projectId?: string): Promise<void> {
  const query = new URLSearchParams()
  if (projectId && projectId.trim()) query.set("project_id", projectId.trim())
  const qs = query.toString()
  await fetch(`${baseUrl()}/v1/sessions/${id}${qs ? `?${qs}` : ""}`, {
    method: "DELETE",
    headers: headers(),
  })
}

// --- Chat (SSE streaming) ---
export function streamChat(
  params: {
    session_id: string
    project_id?: string
    run_id?: string
    message?: string
    mode: Mode
    provider?: string
    tools?: boolean
    fact_check?: boolean
    tool_approval_id?: string
    assistant_name?: string
    assistant_instructions?: string
    strict_profile?: boolean
    web_assist_mode?: "off" | "auto" | "confirm"
  },
  onChunk: (text: string) => void,
  onMeta: (meta: Record<string, unknown>) => void,
  onDone: () => void,
  onError: (err: Error) => void
): AbortController {
  const controller = new AbortController()

  const body = {
    session_id: params.session_id,
    project_id: params.project_id,
    run_id: params.run_id,
    message: params.message,
    mode: params.mode,
    provider: params.provider,
    tools: params.tools,
    fact_check: params.fact_check,
    tool_approval_id: params.tool_approval_id,
    assistant_name: params.assistant_name,
    assistant_instructions: params.assistant_instructions,
    strict_profile: params.strict_profile,
    web_assist_mode: params.web_assist_mode,
    verbose: true,
  }
  fetch(`${baseUrl()}/v1/chat`, {
    method: "POST",
    headers: headers(),
    body: JSON.stringify(body),
    signal: controller.signal,
  })
    .then(async (res) => {
      if (!res.ok) {
        let detail = `HTTP ${res.status}`
        try {
          const payload = (await res.json()) as { detail?: string }
          if (payload.detail) detail = String(payload.detail)
        } catch {
          // keep fallback
        }
        onError(new Error(detail))
        return
      }
      const payload = (await res.json()) as {
        session_id?: string
        run_id?: string
        result?: {
          answer?: string
          status?: string
          mode?: string
          provider?: string
          tokens_in?: number
          tokens_out?: number
          cost?: number
          warnings?: string[]
          tool_outputs?: Array<Record<string, unknown>>
          pending_tool?: {
            approval_id?: string
            tool_name?: string
            arguments?: Record<string, unknown>
            risk_level?: "low" | "medium" | "high"
            reason?: string
            status?: "pending" | "approved" | "denied" | "consumed"
          }
        }
      }
      const result = payload.result ?? {}
      const answer = result.answer ?? ""
      if (answer) onChunk(answer)
      onMeta({
        status: result.status,
        mode: result.mode,
        provider: result.provider,
        tokens: (result.tokens_in ?? 0) + (result.tokens_out ?? 0),
        cost: result.cost ?? 0,
        warnings: result.warnings ?? [],
        tool_outputs: result.tool_outputs ?? [],
        pending_tool: result.pending_tool,
        session_id: payload.session_id,
        run_id: payload.run_id,
      })
      onDone()
    })
    .catch((err) => {
      if (err.name !== "AbortError") onError(err)
    })

  return controller
}

export function streamRunEvents(
  onEvent: (event: { type: string; run_id?: string; run?: RunInfo; sent_at?: string }) => void,
  onError?: (err: Error) => void
): () => void {
  const controller = new AbortController()
  let active = true

  const pump = async () => {
    while (active) {
      try {
        const res = await fetch(`${baseUrl()}/v1/runs/events`, {
          method: "GET",
          headers: {
            ...headers(),
            Accept: "text/event-stream",
          },
          signal: controller.signal,
        })
        if (!res.ok || !res.body) {
          throw new Error(`runs event stream failed (HTTP ${res.status})`)
        }
        const reader = res.body.getReader()
        const decoder = new TextDecoder()
        let buffer = ""
        while (active) {
          const chunk = await reader.read()
          if (chunk.done) break
          buffer += decoder.decode(chunk.value, { stream: true })
          let boundary = buffer.indexOf("\n\n")
          while (boundary >= 0) {
            const frame = buffer.slice(0, boundary)
            buffer = buffer.slice(boundary + 2)
            const lines = frame.split("\n")
            let data = ""
            for (const rawLine of lines) {
              const line = rawLine.trimEnd()
              if (!line || line.startsWith(":")) continue
              if (line.startsWith("data:")) data += line.slice(5).trim()
            }
            if (data) {
              try {
                const parsed = JSON.parse(data) as { type: string; run_id?: string; run?: RunInfo; sent_at?: string }
                onEvent(parsed)
              } catch {
                // ignore malformed frame
              }
            }
            boundary = buffer.indexOf("\n\n")
          }
        }
      } catch (err) {
        if (!active) break
        if ((err as { name?: string }).name === "AbortError") break
        onError?.(err instanceof Error ? err : new Error(String(err)))
        await new Promise((resolve) => window.setTimeout(resolve, 1200))
      }
    }
  }

  void pump()
  return () => {
    active = false
    controller.abort()
  }
}

// --- Runs ---
export async function fetchRuns(
  params?: { status?: string; limit?: number; blocked?: boolean; dependency?: string }
): Promise<RunInfo[]> {
  const query = new URLSearchParams()
  if (params?.status) query.set("status", params.status)
  if (typeof params?.limit === "number") query.set("limit", String(params.limit))
  if (typeof params?.blocked === "boolean") query.set("blocked", params.blocked ? "true" : "false")
  if (params?.dependency) query.set("dependency", params.dependency)
  const qs = query.toString()
  const res = await fetch(`${baseUrl()}/v1/runs${qs ? `?${qs}` : ""}`, { headers: headers() })
  if (!res.ok) await throwApiError(res, "Failed to fetch runs")
  const payload = (await res.json()) as { runs?: RunInfo[] }
  return payload.runs ?? []
}

export async function fetchRun(runId: string): Promise<RunInfo> {
  const res = await fetch(`${baseUrl()}/v1/runs/${encodeURIComponent(runId)}`, {
    headers: headers(),
  })
  if (!res.ok) await throwApiError(res, "Failed to fetch run")
  const payload = (await res.json()) as { run?: RunInfo }
  if (!payload.run) throw new Error("Invalid run response")
  return payload.run
}

export async function resumeRun(runId: string): Promise<{
  run?: RunInfo
  resume?: {
    session_id?: string
    run_id?: string
    result?: {
      answer?: string
      mode?: string
      provider?: string
      tokens_in?: number
      tokens_out?: number
      cost?: number
      warnings?: string[]
      tool_outputs?: Array<Record<string, unknown>>
    }
    answer?: string
  }
}> {
  const res = await fetch(`${baseUrl()}/v1/runs/${encodeURIComponent(runId)}/resume`, {
    method: "POST",
    headers: headers(),
    body: JSON.stringify({}),
  })
  if (!res.ok) await throwApiError(res, "Failed to resume run")
  return (await res.json()) as {
    run?: RunInfo
    resume?: {
      session_id?: string
      run_id?: string
      result?: {
        answer?: string
        mode?: string
        provider?: string
        tokens_in?: number
        tokens_out?: number
        cost?: number
        warnings?: string[]
        tool_outputs?: Array<Record<string, unknown>>
      }
      answer?: string
    }
  }
}

export async function deleteRun(runId: string): Promise<void> {
  const res = await fetch(`${baseUrl()}/v1/runs/${encodeURIComponent(runId)}`, {
    method: "DELETE",
    headers: headers(),
  })
  if (!res.ok) await throwApiError(res, "Failed to delete run")
}

export async function clearRuns(status?: string): Promise<{ deleted: number; status: string }> {
  const res = await fetch(`${baseUrl()}/v1/runs/clear`, {
    method: "POST",
    headers: headers(),
    body: JSON.stringify({ status: status ?? "" }),
  })
  if (!res.ok) await throwApiError(res, "Failed to clear runs")
  return (await res.json()) as { deleted: number; status: string }
}

export async function updateRunDependencies(
  runId: string,
  payload: {
    dependencies?: string[]
    blockers?: Array<{
      blocker_id: string
      code?: string
      message?: string
      severity?: "low" | "medium" | "high" | string
      status?: "open" | "resolved" | string
    }>
    status?: string
  }
): Promise<RunInfo> {
  const res = await fetch(`${baseUrl()}/v1/runs/${encodeURIComponent(runId)}/dependencies`, {
    method: "POST",
    headers: headers(),
    body: JSON.stringify(payload),
  })
  if (!res.ok) await throwApiError(res, "Failed to update run dependencies")
  const json = (await res.json()) as { run?: RunInfo }
  if (!json.run) throw new Error("Invalid dependency update response")
  return json.run
}

export async function fetchRunsDag(limit = 100): Promise<RunDag> {
  const query = new URLSearchParams()
  query.set("limit", String(Math.max(1, Math.min(500, Math.floor(limit)))))
  const res = await fetch(`${baseUrl()}/v1/runs/dag?${query.toString()}`, {
    headers: headers(),
  })
  if (!res.ok) await throwApiError(res, "Failed to fetch run DAG")
  const payload = (await res.json()) as { dag?: RunDag }
  if (!payload.dag) throw new Error("Invalid run DAG response")
  return payload.dag
}

export async function runStrictReliabilityCheck(params: {
  runs: number
  provider?: string
  model?: string
}): Promise<{
  runs: number
  passed: number
  failed: number
  details: Array<{ index: number; ok: boolean; answer: string }>
}> {
  const runs = Math.max(1, Math.min(20, Math.floor(params.runs || 1)))
  const provider = params.provider?.trim() || undefined
  const model = params.model?.trim() || undefined
  const sessionPrefix = `strict-check-${Date.now()}`
  const message = "Greet me briefly and confirm readiness."
  const assistantName = "CerbiBot"
  const assistantInstructions =
    'Always answer in exactly 2 bullet points. Start first bullet with "CerbiBot:" and start second bullet with "Status:"'

  const details: Array<{ index: number; ok: boolean; answer: string }> = []
  let passed = 0
  for (let i = 0; i < runs; i += 1) {
    const body: Record<string, unknown> = {
      session_id: `${sessionPrefix}-${i + 1}`,
      message,
      mode: "single",
      assistant_name: assistantName,
      assistant_instructions: assistantInstructions,
      strict_profile: true,
      verbose: true,
    }
    if (provider) body.provider = provider
    // Model pinning uses active daemon provider model config; this field is currently informational.
    void model
    const res = await fetch(`${baseUrl()}/v1/chat`, {
      method: "POST",
      headers: headers(),
      body: JSON.stringify(body),
    })
    if (!res.ok) await throwApiError(res, "Strict reliability check failed")
    const payload = (await res.json()) as {
      result?: {
        answer?: string
      }
    }
    const answer = payload.result?.answer ?? ""
    const lines = answer
      .split("\n")
      .map((line) => line.trim())
      .filter(Boolean)
    const ok =
      lines.length === 2 &&
      lines[0].startsWith("- CerbiBot:") &&
      lines[1].startsWith("- Status:") &&
      !/(we are|remember:|example|context says|assistant must|instruction|meta text)/i.test(answer)
    if (ok) passed += 1
    details.push({ index: i + 1, ok, answer })
  }
  return { runs, passed, failed: runs - passed, details }
}

// --- Cost ---
export async function fetchCost(): Promise<CostData> {
  const res = await fetch(`${baseUrl()}/v1/cost`, { headers: headers() })
  if (!res.ok) throw new Error("Failed to fetch cost")
  const payload = (await res.json()) as {
    remaining?: { monthly?: number }
    state?: { daily_spend?: number; monthly_spend?: number }
    totals?: {
      daily_totals?: {
        requests?: number
        providers?: Record<string, { requests?: number }>
      }
      monthly_totals?: {
        cost?: number
        requests?: number
        providers?: Record<string, { cost?: number; requests?: number }>
      }
    }
    rate_limits?: Record<
      string,
      {
        rpm_limit: number
        tpm_limit: number
        rpm_used: number
        tpm_used: number
        rpm_headroom: number
        tpm_headroom: number
      }
    >
    router_weights?: Record<
      string,
      Record<
        string,
        {
          score: number
          count: number
          p50_latency_ms: number
          p95_latency_ms: number
        }
      >
    >
  }
  const rates = payload.rate_limits ?? {}
  const settings = getSettings()
  const dailyTotals = payload.totals?.daily_totals
  const dailyProviders = dailyTotals?.providers ?? {}
  const reqProviderNames = Object.keys(dailyProviders)
  const perProviderFromTotals = Object.fromEntries(
    reqProviderNames.map((name) => [name, Number(dailyProviders[name]?.requests ?? 0)])
  )
  const perProviderFromRates = Object.fromEntries(
    Object.keys(rates).map((name) => [name, Number(rates[name]?.rpm_used ?? 0)])
  )
  const perProvider =
    reqProviderNames.length > 0 ? perProviderFromTotals : perProviderFromRates
  const totalReq =
    typeof dailyTotals?.requests === "number"
      ? Number(dailyTotals.requests)
      : Object.values(perProvider).reduce((sum, value) => sum + Number(value ?? 0), 0)
  const monthlyProviders = payload.totals?.monthly_totals?.providers ?? {}
  const monthlySpendPerProvider = Object.fromEntries(
    Object.keys(monthlyProviders).map((name) => [name, Number(monthlyProviders[name]?.cost ?? 0)])
  )
  const monthlyBudgetPerProvider: Record<string, number> = {}
  for (const [name, value] of Object.entries(settings.providerMonthlyBudgets ?? {})) {
    const numeric = Math.max(0, Number(value ?? 0))
    if (Number.isFinite(numeric)) {
      monthlyBudgetPerProvider[String(name)] = numeric
    }
  }
  const monthlyBudgetTotal = Object.values(monthlyBudgetPerProvider).reduce((sum: number, value: number) => sum + value, 0)
  const effectiveBudgetRemaining =
    monthlyBudgetTotal > 0
      ? Math.max(0, monthlyBudgetTotal - (payload.state?.monthly_spend ?? 0))
      : (payload.remaining?.monthly ?? 0)
  return {
    today: payload.state?.daily_spend ?? 0,
    month: payload.state?.monthly_spend ?? 0,
    budget_remaining: effectiveBudgetRemaining,
    monthly_budget_total: monthlyBudgetTotal > 0 ? monthlyBudgetTotal : undefined,
    per_provider: perProvider,
    monthly_spend_per_provider: monthlySpendPerProvider,
    monthly_budget_per_provider: monthlyBudgetPerProvider,
    requests_today: totalReq,
    rate_limits: rates,
    router_weights: payload.router_weights ?? {},
  }
}

// --- Health ---
export async function fetchHealth(): Promise<HealthData> {
  const res = await fetch(`${baseUrl()}/v1/health`, { headers: headers() })
  if (!res.ok) throw new Error("Failed to fetch health")
  const payload = (await res.json()) as {
    providers?: string[]
    budget_remaining?: { session?: number; daily?: number; monthly?: number }
  }
  const providers = (payload.providers ?? []).map((name) => ({
    name,
    status: "healthy" as const,
    latency_ms: 0,
    error_rate: 0,
  }))
  const budget = payload.budget_remaining ?? {}
  const budget_ok = (budget.session ?? 0) > 0 && (budget.daily ?? 0) > 0 && (budget.monthly ?? 0) > 0
  return { providers, budget_ok }
}

export async function fetchDelegationHealth(): Promise<{
  status: string
  reachable: boolean
  socket_path: string
  detail?: string
}> {
  const res = await fetch(`${baseUrl()}/v1/server/delegate/health`, { headers: headers() })
  if (!res.ok) await throwApiError(res, "Failed to fetch delegation health")
  const payload = (await res.json()) as {
    status?: string
    reachable?: boolean
    socket_path?: string
    detail?: string
  }
  return {
    status: String(payload.status ?? "unknown"),
    reachable: Boolean(payload.reachable),
    socket_path: String(payload.socket_path ?? ""),
    detail: payload.detail ? String(payload.detail) : undefined,
  }
}

export async function fetchDelegationJobs(limit = 50): Promise<Array<Record<string, unknown>>> {
  const q = new URLSearchParams()
  q.set("limit", String(Math.max(1, Math.min(500, Math.floor(limit)))))
  const res = await fetch(`${baseUrl()}/v1/server/delegate/jobs?${q.toString()}`, { headers: headers() })
  if (!res.ok) await throwApiError(res, "Failed to fetch delegation jobs")
  const payload = (await res.json()) as { jobs?: Array<Record<string, unknown>> }
  return Array.isArray(payload.jobs) ? payload.jobs : []
}

export async function deleteDelegationJob(
  jobId: string,
  adminPassword?: string,
  allowRunning = false
): Promise<{ deleted: number; job_ids: string[] }> {
  const res = await fetch(`${baseUrl()}/v1/server/delegate/jobs/${encodeURIComponent(jobId)}/delete`, {
    method: "POST",
    headers: headers(),
    body: JSON.stringify({
      admin_password: adminPassword ?? "",
      allow_running: Boolean(allowRunning),
    }),
  })
  if (!res.ok) await throwApiError(res, "Failed to delete delegation job")
  const payload = (await res.json()) as { deleted?: number; job_ids?: string[] }
  return {
    deleted: Number(payload.deleted ?? 0),
    job_ids: Array.isArray(payload.job_ids) ? payload.job_ids.map((item) => String(item)) : [],
  }
}

export async function deleteDelegationJobs(
  adminPassword?: string,
  olderThanDays?: number,
  limit = 1000,
  allowRunning = false
): Promise<{ deleted: number; job_ids: string[]; older_than_days: number | null; skipped: Array<Record<string, unknown>> }> {
  const body: Record<string, unknown> = {
    admin_password: adminPassword ?? "",
    limit: Math.max(1, Math.min(5000, Math.floor(limit))),
    allow_running: Boolean(allowRunning),
  }
  if (typeof olderThanDays === "number" && Number.isFinite(olderThanDays) && olderThanDays > 0) {
    body.older_than_days = Math.floor(olderThanDays)
  }
  const res = await fetch(`${baseUrl()}/v1/server/delegate/jobs/delete-all`, {
    method: "POST",
    headers: headers(),
    body: JSON.stringify(body),
  })
  if (!res.ok) await throwApiError(res, "Failed to delete delegation jobs")
  const payload = (await res.json()) as {
    deleted?: number
    job_ids?: string[]
    older_than_days?: number | null
    skipped?: Array<Record<string, unknown>>
  }
  return {
    deleted: Number(payload.deleted ?? 0),
    job_ids: Array.isArray(payload.job_ids) ? payload.job_ids.map((item) => String(item)) : [],
    older_than_days:
      payload.older_than_days == null ? null : Number.isFinite(Number(payload.older_than_days)) ? Number(payload.older_than_days) : null,
    skipped: Array.isArray(payload.skipped) ? payload.skipped : [],
  }
}

// --- Memory ---
export async function fetchMemory(projectId?: string): Promise<MemoryEntry[]> {
  const query = new URLSearchParams()
  if (projectId && projectId.trim()) query.set("project_id", projectId.trim())
  const qs = query.toString()
  const res = await fetch(`${baseUrl()}/v1/memory${qs ? `?${qs}` : ""}`, { headers: headers() })
  if (!res.ok) throw new Error("Failed to fetch memory")
  const payload = (await res.json()) as { memories?: MemoryEntry[] }
  return payload.memories ?? []
}

export async function addMemory(
  statement: string,
  source_type?: string,
  source_ref?: string,
  projectId?: string
): Promise<MemoryEntry> {
  const res = await fetch(`${baseUrl()}/v1/memory`, {
    method: "POST",
    headers: headers(),
    body: JSON.stringify({ statement, source_type, source_ref, project_id: projectId }),
  })
  if (!res.ok) throw new Error("Failed to add memory")
  const payload = (await res.json()) as { id?: string | number }
  return {
    id: payload.id ?? String(Date.now()),
    statement,
    source_type: source_type ?? "api",
    created_at: new Date().toISOString(),
  }
}

export async function suggestMemoryFromSession(
  sessionId: string,
  provider?: string,
  projectId?: string
): Promise<{
  suggested: boolean
  reason?: string
  candidate?: {
    statement: string
    source_type: string
    confidence: number
    reason: string
    session_id: string
    project_id?: string
  }
}> {
  const res = await fetch(`${baseUrl()}/v1/memory/suggest`, {
    method: "POST",
    headers: headers(),
    body: JSON.stringify({ session_id: sessionId, provider, project_id: projectId }),
  })
  if (!res.ok) await throwApiError(res, "Failed to suggest memory")
  const payload = (await res.json()) as {
    suggested?: boolean
    reason?: string
    candidate?: {
      statement?: string
      source_type?: string
      confidence?: number
      reason?: string
      session_id?: string
      project_id?: string
    }
  }
  return {
    suggested: Boolean(payload.suggested),
    reason: payload.reason ? String(payload.reason) : undefined,
    candidate: payload.candidate
      ? {
          statement: String(payload.candidate.statement ?? ""),
          source_type: String(payload.candidate.source_type ?? "chat_inferred"),
          confidence: Number(payload.candidate.confidence ?? 0),
          reason: String(payload.candidate.reason ?? ""),
          session_id: String(payload.candidate.session_id ?? sessionId),
          project_id: payload.candidate.project_id ? String(payload.candidate.project_id) : undefined,
        }
      : undefined,
  }
}

export async function deleteMemory(id: string | number, projectId?: string): Promise<void> {
  const query = new URLSearchParams()
  if (projectId && projectId.trim()) query.set("project_id", projectId.trim())
  const qs = query.toString()
  const res = await fetch(`${baseUrl()}/v1/memory/${id}${qs ? `?${qs}` : ""}`, {
    method: "DELETE",
    headers: headers(),
  })
  if (!res.ok && res.status !== 404 && res.status !== 405) {
    throw new Error(`Failed to delete memory (${res.status})`)
  }
}

// --- Artifacts ---
export async function fetchArtifacts(): Promise<ArtifactRun[]> {
  const res = await fetch(`${baseUrl()}/v1/artifacts`, { headers: headers() })
  if (!res.ok) throw new Error("Failed to fetch artifacts")
  const payload = (await res.json()) as { artifacts?: ArtifactRun[] }
  return payload.artifacts ?? []
}

export async function fetchArtifact(id: string): Promise<ArtifactRun> {
  const res = await fetch(`${baseUrl()}/v1/artifacts/${id}`, { headers: headers() })
  if (!res.ok) throw new Error("Failed to fetch artifact")
  const payload = (await res.json()) as { run?: ArtifactRun }
  if (!payload.run) throw new Error("Invalid artifact response")
  return payload.run
}

export async function fetchRunTriggers(): Promise<RunTrigger[]> {
  const res = await fetch(`${baseUrl()}/v1/server/run-triggers`, { headers: headers() })
  if (!res.ok) await throwApiError(res, "Failed to fetch run triggers")
  const payload = (await res.json()) as { triggers?: RunTrigger[] }
  return payload.triggers ?? []
}

export async function saveRunTriggers(
  triggers: RunTrigger[],
  adminPassword?: string
): Promise<RunTrigger[]> {
  const res = await fetch(`${baseUrl()}/v1/server/run-triggers`, {
    method: "PUT",
    headers: headers(),
    body: JSON.stringify({ triggers, admin_password: adminPassword ?? "" }),
  })
  if (!res.ok) await throwApiError(res, "Failed to save run triggers")
  const payload = (await res.json()) as { triggers?: RunTrigger[] }
  return payload.triggers ?? []
}

export async function rotateRunTriggerSecret(triggerId: string, adminPassword?: string): Promise<RunTrigger> {
  const res = await fetch(`${baseUrl()}/v1/server/run-triggers/${encodeURIComponent(triggerId)}/rotate-secret`, {
    method: "POST",
    headers: headers(),
    body: JSON.stringify({ admin_password: adminPassword ?? "" }),
  })
  if (!res.ok) await throwApiError(res, "Failed to rotate run trigger secret")
  const payload = (await res.json()) as { trigger?: RunTrigger }
  if (!payload.trigger) throw new Error("Invalid run trigger response")
  return payload.trigger
}

export async function sweepRunTriggers(): Promise<{ checked: number; due: number; fired: number; runs: Array<Record<string, unknown>> }> {
  const res = await fetch(`${baseUrl()}/v1/server/run-triggers/sweep`, {
    method: "POST",
    headers: headers(),
    body: JSON.stringify({}),
  })
  if (!res.ok) await throwApiError(res, "Failed to sweep run triggers")
  return (await res.json()) as { checked: number; due: number; fired: number; runs: Array<Record<string, unknown>> }
}

export async function exportArtifact(
  id: string,
  adminPassword?: string
): Promise<Record<string, unknown>> {
  const res = await fetch(`${baseUrl()}/v1/artifacts/${encodeURIComponent(id)}/export`, {
    method: "POST",
    headers: headers(),
    body: JSON.stringify({ admin_password: adminPassword ?? "" }),
  })
  if (!res.ok) await throwApiError(res, "Failed to export artifact")
  const payload = (await res.json()) as { artifact?: Record<string, unknown> }
  if (!payload.artifact) throw new Error("Invalid artifact export response")
  return payload.artifact
}

export async function exportAllArtifacts(
  adminPassword?: string,
  limit = 200
): Promise<Array<{ request_id: string; artifact: Record<string, unknown> }>> {
  const res = await fetch(`${baseUrl()}/v1/artifacts/export-all`, {
    method: "POST",
    headers: headers(),
    body: JSON.stringify({ admin_password: adminPassword ?? "", limit }),
  })
  if (!res.ok) await throwApiError(res, "Failed to export artifacts")
  const payload = (await res.json()) as {
    artifacts?: Array<{ request_id?: string; artifact?: Record<string, unknown> }>
  }
  return (payload.artifacts ?? []).map((row) => ({
    request_id: row.request_id ?? "",
    artifact: row.artifact ?? {},
  }))
}

export async function deleteArtifact(
  id: string,
  adminPassword?: string
): Promise<{ deleted: number; request_ids: string[] }> {
  const res = await fetch(`${baseUrl()}/v1/artifacts/${encodeURIComponent(id)}/delete`, {
    method: "POST",
    headers: headers(),
    body: JSON.stringify({ admin_password: adminPassword ?? "" }),
  })
  if (!res.ok) await throwApiError(res, "Failed to delete artifact")
  const payload = (await res.json()) as { deleted?: number; request_ids?: string[] }
  return {
    deleted: Number(payload.deleted ?? 0),
    request_ids: Array.isArray(payload.request_ids) ? payload.request_ids.map((item) => String(item)) : [],
  }
}

export async function deleteAllArtifacts(
  adminPassword?: string,
  olderThanDays?: number,
  limit = 1000
): Promise<{ deleted: number; request_ids: string[]; older_than_days: number | null }> {
  const body: Record<string, unknown> = { admin_password: adminPassword ?? "", limit }
  if (typeof olderThanDays === "number" && Number.isFinite(olderThanDays) && olderThanDays > 0) {
    body.older_than_days = Math.floor(olderThanDays)
  }
  const res = await fetch(`${baseUrl()}/v1/artifacts/delete-all`, {
    method: "POST",
    headers: headers(),
    body: JSON.stringify(body),
  })
  if (!res.ok) await throwApiError(res, "Failed to delete artifacts")
  const payload = (await res.json()) as { deleted?: number; request_ids?: string[]; older_than_days?: number | null }
  return {
    deleted: Number(payload.deleted ?? 0),
    request_ids: Array.isArray(payload.request_ids) ? payload.request_ids.map((item) => String(item)) : [],
    older_than_days:
      payload.older_than_days == null ? null : Number.isFinite(Number(payload.older_than_days)) ? Number(payload.older_than_days) : null,
  }
}

export async function fetchArtifactsEncryptionStatus(): Promise<{
  enabled: boolean
  directory: string
  sampled_files: number
  encrypted_files: number
}> {
  const res = await fetch(`${baseUrl()}/v1/artifacts/encryption/status`, { headers: headers() })
  if (!res.ok) await throwApiError(res, "Failed to fetch artifact encryption status")
  const payload = (await res.json()) as {
    enabled?: boolean
    directory?: string
    sampled_files?: number
    encrypted_files?: number
  }
  return {
    enabled: Boolean(payload.enabled),
    directory: payload.directory ?? "",
    sampled_files: payload.sampled_files ?? 0,
    encrypted_files: payload.encrypted_files ?? 0,
  }
}

// --- Skills ---
export async function fetchSkills(): Promise<Skill[]> {
  const res = await fetch(`${baseUrl()}/v1/skills`, { headers: headers() })
  if (!res.ok) throw new Error("Failed to fetch skills")
  const payload = (await res.json()) as { skills?: Skill[] }
  return payload.skills ?? []
}

export async function fetchSkillCatalog(): Promise<SkillCatalogEntry[]> {
  const res = await fetch(`${baseUrl()}/v1/skills/catalog`, { headers: headers() })
  if (!res.ok) await throwApiError(res, "Failed to fetch skills catalog")
  const payload = (await res.json()) as { catalog?: Array<Record<string, unknown>> }
  const rows = Array.isArray(payload.catalog) ? payload.catalog : []
  return rows.map((row) => ({
    id: String(row.id ?? ""),
    title: String(row.title ?? ""),
    description: String(row.description ?? ""),
    trust: String(row.trust ?? ""),
    official: Boolean(row.official),
    tested: row.tested === "smoke" ? "smoke" : "schema",
    risk_level: row.risk_level === "high" ? "high" : row.risk_level === "medium" ? "medium" : "low",
    workflow_text: String(row.workflow_text ?? ""),
    installed: Boolean(row.installed),
    enabled: Boolean(row.enabled),
    signature_verified: Boolean(row.signature_verified),
    checksum: String(row.checksum ?? ""),
  }))
}

export async function setSkillEnabled(name: string, enabled: boolean): Promise<void> {
  const action = enabled ? "enable" : "disable"
  const res = await fetch(`${baseUrl()}/v1/skills/${encodeURIComponent(name)}/${action}`, {
    method: "POST",
    headers: headers(),
  })
  if (!res.ok) throw new Error(`Failed to ${action} skill`)
}

export async function deleteSkill(name: string): Promise<void> {
  const res = await fetch(`${baseUrl()}/v1/skills/${encodeURIComponent(name)}`, {
    method: "DELETE",
    headers: headers(),
  })
  if (!res.ok) await throwApiError(res, "Failed to delete skill")
}

export async function validateSkillDraft(workflowText: string): Promise<{
  valid: boolean
  errors: string[]
  name: string
  risk_level: "low" | "medium" | "high"
  manifest: Record<string, unknown>
}> {
  const res = await fetch(`${baseUrl()}/v1/skills/draft/validate`, {
    method: "POST",
    headers: headers(),
    body: JSON.stringify({ workflow_text: workflowText }),
  })
  if (!res.ok) await throwApiError(res, "Failed to validate skill draft")
  const payload = (await res.json()) as {
    valid?: boolean
    errors?: string[]
    name?: string
    risk_level?: string
    manifest?: Record<string, unknown>
  }
  const risk = payload.risk_level
  return {
    valid: Boolean(payload.valid),
    errors: Array.isArray(payload.errors) ? payload.errors.map((x) => String(x)) : [],
    name: String(payload.name ?? ""),
    risk_level: risk === "medium" || risk === "high" ? risk : "low",
    manifest: payload.manifest ?? {},
  }
}

export async function saveSkillDraft(
  workflowText: string,
  overwrite = false
): Promise<{ saved: boolean; name: string; path: string; enabled: boolean; overwrote: boolean }> {
  const res = await fetch(`${baseUrl()}/v1/skills/draft/save`, {
    method: "POST",
    headers: headers(),
    body: JSON.stringify({ workflow_text: workflowText, overwrite }),
  })
  if (!res.ok) await throwApiError(res, "Failed to save skill draft")
  const payload = (await res.json()) as {
    saved?: boolean
    name?: string
    path?: string
    enabled?: boolean
    overwrote?: boolean
  }
  return {
    saved: Boolean(payload.saved),
    name: String(payload.name ?? ""),
    path: String(payload.path ?? ""),
    enabled: Boolean(payload.enabled),
    overwrote: Boolean(payload.overwrote),
  }
}

export async function testSkillDraft(workflowText: string): Promise<{
  validation: { valid: boolean; errors: string[] }
  run?: { skill_name: string; steps_executed: number; total_cost: number; outputs: Record<string, unknown> }
}> {
  const res = await fetch(`${baseUrl()}/v1/skill-drafts/test`, {
    method: "POST",
    headers: headers(),
    body: JSON.stringify({ workflow_text: workflowText, run: true }),
  })
  if (!res.ok) await throwApiError(res, "Failed to test skill draft")
  const payload = (await res.json()) as {
    validation?: { valid?: boolean; errors?: string[] }
    run?: { skill_name?: string; steps_executed?: number; total_cost?: number; outputs?: Record<string, unknown> }
  }
  return {
    validation: {
      valid: Boolean(payload.validation?.valid),
      errors: Array.isArray(payload.validation?.errors)
        ? payload.validation!.errors!.map((x) => String(x))
        : [],
    },
    run: payload.run
      ? {
          skill_name: String(payload.run.skill_name ?? ""),
          steps_executed: Number(payload.run.steps_executed ?? 0),
          total_cost: Number(payload.run.total_cost ?? 0),
          outputs: payload.run.outputs ?? {},
        }
      : undefined,
  }
}

export async function runInstalledSkill(
  name: string,
  options?: { mode?: Mode; provider?: string; input?: Record<string, unknown> }
): Promise<{
  skill: string
  validation: { valid: boolean; errors: string[] }
  run?: { skill_name: string; steps_executed: number; total_cost: number; outputs: Record<string, unknown> }
}> {
  const body: Record<string, unknown> = {
    run: true,
    mode: options?.mode ?? "single",
  }
  if (options?.provider) body.provider = options.provider
  if (options?.input) body.input = options.input
  const res = await fetch(`${baseUrl()}/v1/skills/${encodeURIComponent(name)}/test`, {
    method: "POST",
    headers: headers(),
    body: JSON.stringify(body),
  })
  if (!res.ok) await throwApiError(res, "Failed to run skill")
  const payload = (await res.json()) as {
    skill?: string
    validation?: { valid?: boolean; errors?: string[] }
    run?: { skill_name?: string; steps_executed?: number; total_cost?: number; outputs?: Record<string, unknown> }
  }
  return {
    skill: String(payload.skill ?? name),
    validation: {
      valid: Boolean(payload.validation?.valid),
      errors: Array.isArray(payload.validation?.errors)
        ? payload.validation!.errors!.map((x) => String(x))
        : [],
    },
    run: payload.run
      ? {
          skill_name: String(payload.run.skill_name ?? name),
          steps_executed: Number(payload.run.steps_executed ?? 0),
          total_cost: Number(payload.run.total_cost ?? 0),
          outputs: payload.run.outputs ?? {},
        }
      : undefined,
  }
}

export async function exportSkill(name: string): Promise<{
  name: string
  path: string
  enabled: boolean
  checksum: string
  signature_verified: boolean
  workflow_text: string
}> {
  const res = await fetch(`${baseUrl()}/v1/skills/${encodeURIComponent(name)}/export`, {
    method: "GET",
    headers: headers(),
  })
  if (!res.ok) await throwApiError(res, "Failed to export skill")
  const payload = (await res.json()) as {
    skill?: {
      name?: string
      path?: string
      enabled?: boolean
      checksum?: string
      signature_verified?: boolean
      workflow_text?: string
    }
  }
  const skill = payload.skill ?? {}
  return {
    name: String(skill.name ?? name),
    path: String(skill.path ?? ""),
    enabled: Boolean(skill.enabled),
    checksum: String(skill.checksum ?? ""),
    signature_verified: Boolean(skill.signature_verified),
    workflow_text: String(skill.workflow_text ?? ""),
  }
}

export async function importSkillBundle(
  bundle: unknown,
  overwrite = false
): Promise<{ imported: boolean; name: string; path: string; enabled: boolean; overwrote: boolean }> {
  const body =
    bundle && typeof bundle === "object"
      ? ({ ...(bundle as Record<string, unknown>), overwrite } as Record<string, unknown>)
      : { workflow_text: "", overwrite }
  const res = await fetch(`${baseUrl()}/v1/skills/import`, {
    method: "POST",
    headers: headers(),
    body: JSON.stringify(body),
  })
  if (!res.ok) await throwApiError(res, "Failed to import skill bundle")
  const payload = (await res.json()) as {
    imported?: boolean
    name?: string
    path?: string
    enabled?: boolean
    overwrote?: boolean
  }
  return {
    imported: Boolean(payload.imported),
    name: String(payload.name ?? ""),
    path: String(payload.path ?? ""),
    enabled: Boolean(payload.enabled),
    overwrote: Boolean(payload.overwrote),
  }
}

export interface SkillGovernanceCandidate {
  skill_a: string
  skill_b: string
  score: number
  capability_overlap: number
  io_overlap: number
  dependency_overlap: number
  rationale: string
  recommendation: string
}

export interface SkillGovernanceReport {
  generated_at: string
  summary: {
    skills_analyzed: number
    merge_candidates: number
    crossover_candidates: number
  }
  artifacts: {
    out_dir: string
    merge_candidates_path: string
    crossover_candidates_path: string
    skills_bloat_report_path: string
    deprecation_plan_path: string
  }
  merge_candidates: SkillGovernanceCandidate[]
  crossover_candidates: SkillGovernanceCandidate[]
}

export async function runSkillGovernanceAnalysis(params?: {
  includeDisabled?: boolean
  limit?: number
  outDir?: string
}): Promise<SkillGovernanceReport> {
  const res = await fetch(`${baseUrl()}/v1/skills/governance/analyze`, {
    method: "POST",
    headers: headers(),
    body: JSON.stringify({
      include_disabled: Boolean(params?.includeDisabled),
      limit: Number(params?.limit ?? 20),
      out_dir: params?.outDir || undefined,
    }),
  })
  if (!res.ok) await throwApiError(res, "Failed to run skill governance analysis")
  const payload = (await res.json()) as Partial<SkillGovernanceReport>
  return {
    generated_at: String(payload.generated_at ?? ""),
    summary: {
      skills_analyzed: Number(payload.summary?.skills_analyzed ?? 0),
      merge_candidates: Number(payload.summary?.merge_candidates ?? 0),
      crossover_candidates: Number(payload.summary?.crossover_candidates ?? 0),
    },
    artifacts: {
      out_dir: String(payload.artifacts?.out_dir ?? ""),
      merge_candidates_path: String(payload.artifacts?.merge_candidates_path ?? ""),
      crossover_candidates_path: String(payload.artifacts?.crossover_candidates_path ?? ""),
      skills_bloat_report_path: String(payload.artifacts?.skills_bloat_report_path ?? ""),
      deprecation_plan_path: String(payload.artifacts?.deprecation_plan_path ?? ""),
    },
    merge_candidates: Array.isArray(payload.merge_candidates)
      ? payload.merge_candidates.map((row) => ({
          skill_a: String((row as SkillGovernanceCandidate).skill_a ?? ""),
          skill_b: String((row as SkillGovernanceCandidate).skill_b ?? ""),
          score: Number((row as SkillGovernanceCandidate).score ?? 0),
          capability_overlap: Number((row as SkillGovernanceCandidate).capability_overlap ?? 0),
          io_overlap: Number((row as SkillGovernanceCandidate).io_overlap ?? 0),
          dependency_overlap: Number((row as SkillGovernanceCandidate).dependency_overlap ?? 0),
          rationale: String((row as SkillGovernanceCandidate).rationale ?? ""),
          recommendation: String((row as SkillGovernanceCandidate).recommendation ?? ""),
        }))
      : [],
    crossover_candidates: Array.isArray(payload.crossover_candidates)
      ? payload.crossover_candidates.map((row) => ({
          skill_a: String((row as SkillGovernanceCandidate).skill_a ?? ""),
          skill_b: String((row as SkillGovernanceCandidate).skill_b ?? ""),
          score: Number((row as SkillGovernanceCandidate).score ?? 0),
          capability_overlap: Number((row as SkillGovernanceCandidate).capability_overlap ?? 0),
          io_overlap: Number((row as SkillGovernanceCandidate).io_overlap ?? 0),
          dependency_overlap: Number((row as SkillGovernanceCandidate).dependency_overlap ?? 0),
          rationale: String((row as SkillGovernanceCandidate).rationale ?? ""),
          recommendation: String((row as SkillGovernanceCandidate).recommendation ?? ""),
        }))
      : [],
  }
}

export async function approveToolApproval(id: string): Promise<void> {
  const res = await fetch(`${baseUrl()}/v1/tool-approvals/${encodeURIComponent(id)}/approve`, {
    method: "POST",
    headers: headers(),
  })
  if (!res.ok) throw new Error("Failed to approve tool call")
}

export async function denyToolApproval(id: string): Promise<void> {
  const res = await fetch(`${baseUrl()}/v1/tool-approvals/${encodeURIComponent(id)}/deny`, {
    method: "POST",
    headers: headers(),
  })
  if (!res.ok) throw new Error("Failed to deny tool call")
}

export async function fetchToolApprovals(status: "pending" | "approved" | "denied" | "consumed" | "" = "pending"): Promise<ToolCall[]> {
  const query = status ? `?status=${encodeURIComponent(status)}` : ""
  const res = await fetch(`${baseUrl()}/v1/tool-approvals${query}`, { headers: headers() })
  if (!res.ok) throw new Error("Failed to fetch tool approvals")
  const payload = (await res.json()) as {
    approvals?: Array<{
      approval_id?: string
      tool_name?: string
      arguments?: Record<string, unknown>
      risk_level?: "low" | "medium" | "high"
      reason?: string
      status?: "pending" | "approved" | "denied" | "consumed"
    }>
  }
  return (payload.approvals ?? []).map((item) => ({
    approval_id: item.approval_id,
    tool_name: item.tool_name ?? "tool",
    arguments: item.arguments ?? {},
    risk_level: item.risk_level ?? "high",
    reason: item.reason,
    status: item.status,
  }))
}

export async function fetchProviderCatalog(): Promise<Record<string, string[]>> {
  const res = await fetch(`${baseUrl()}/v1/providers/catalog`, { headers: headers() })
  if (!res.ok) await throwApiError(res, "Failed to fetch provider catalog")
  const payload = (await res.json()) as { catalog?: Record<string, string[]> }
  return payload.catalog ?? {}
}

export interface ProviderModelsResponse {
  provider: string
  models: string[]
  configured_model: string
  fast_model: string
  source: string
  warnings: string[]
}

export async function fetchProviderModels(provider: string): Promise<ProviderModelsResponse> {
  const res = await fetch(`${baseUrl()}/v1/providers/${encodeURIComponent(provider)}/models`, { headers: headers() })
  if (!res.ok) await throwApiError(res, "Failed to fetch provider models")
  const payload = (await res.json()) as Partial<ProviderModelsResponse>
  return {
    provider: String(payload.provider ?? provider),
    models: Array.isArray(payload.models) ? payload.models.map((m) => String(m)) : [],
    configured_model: String(payload.configured_model ?? ""),
    fast_model: String(payload.fast_model ?? ""),
    source: String(payload.source ?? "catalog"),
    warnings: Array.isArray(payload.warnings) ? payload.warnings.map((w) => String(w)) : [],
  }
}

export async function fetchProviderConfig(): Promise<ProviderConfigEntry[]> {
  const res = await fetch(`${baseUrl()}/v1/providers`, { headers: headers() })
  if (!res.ok) await throwApiError(res, "Failed to fetch provider config")
  const payload = (await res.json()) as {
    providers?: Array<{ name?: string; model?: string; enabled?: boolean }>
  }
  return (payload.providers ?? []).map((item) => ({
    name: item.name ?? "",
    model: item.model ?? "",
    enabled: Boolean(item.enabled),
  }))
}

export async function applyProviderConfig(providers: ProviderConfigEntry[]): Promise<void> {
  const res = await fetch(`${baseUrl()}/v1/providers`, {
    method: "PUT",
    headers: headers(),
    body: JSON.stringify({ providers }),
  })
  if (!res.ok) await throwApiError(res, "Failed to apply provider config")
}

export async function fetchRoleRoutingConfig(): Promise<RoleRoutingConfig> {
  const res = await fetch(`${baseUrl()}/v1/routing/roles`, { headers: headers() })
  if (!res.ok) await throwApiError(res, "Failed to fetch role routing config")
  const payload = (await res.json()) as { routing?: RoleRoutingConfig }
  if (!payload.routing) throw new Error("Invalid role routing response")
  return payload.routing
}

export async function applyRoleRoutingConfig(routing: RoleRoutingConfig): Promise<RoleRoutingConfig> {
  const res = await fetch(`${baseUrl()}/v1/routing/roles`, {
    method: "PUT",
    headers: headers(),
    body: JSON.stringify({ routing }),
  })
  if (!res.ok) await throwApiError(res, "Failed to apply role routing config")
  const payload = (await res.json()) as { routing?: RoleRoutingConfig }
  if (!payload.routing) throw new Error("Invalid role routing apply response")
  return payload.routing
}

export interface ProviderKeyStatusEntry {
  name: string
  api_key_env: string
  key_set: boolean
  source: "env" | "keyring" | "none"
}

export async function fetchProviderKeyStatus(): Promise<ProviderKeyStatusEntry[]> {
  const res = await fetch(`${baseUrl()}/v1/providers/keys/status`, { headers: headers() })
  if (!res.ok) await throwApiError(res, "Failed to fetch provider key status")
  const payload = (await res.json()) as {
    providers?: Array<{
      name?: string
      api_key_env?: string
      key_set?: boolean
      source?: "env" | "keyring" | "none"
    }>
  }
  return (payload.providers ?? []).map((item) => ({
    name: item.name ?? "",
    api_key_env: item.api_key_env ?? "",
    key_set: Boolean(item.key_set),
    source: item.source ?? "none",
  }))
}

export async function setProviderApiKey(provider: string, apiKey: string): Promise<void> {
  const res = await fetch(`${baseUrl()}/v1/providers/keys`, {
    method: "POST",
    headers: headers(),
    body: JSON.stringify({ provider, api_key: apiKey }),
  })
  if (!res.ok) await throwApiError(res, "Failed to set provider API key")
}

export async function clearProviderApiKey(provider: string): Promise<void> {
  const res = await fetch(`${baseUrl()}/v1/providers/keys/${encodeURIComponent(provider)}`, {
    method: "DELETE",
    headers: headers(),
  })
  if (!res.ok) await throwApiError(res, "Failed to clear provider API key")
}

export async function testProviderConnection(provider: string, model?: string): Promise<{ latency_ms: number }> {
  const res = await fetch(`${baseUrl()}/v1/providers/${encodeURIComponent(provider)}/test`, {
    method: "POST",
    headers: headers(),
    body: JSON.stringify(model ? { model } : {}),
  })
  if (!res.ok) await throwApiError(res, "Failed to test provider connection")
  const payload = (await res.json()) as { latency_ms?: number }
  return { latency_ms: payload.latency_ms ?? 0 }
}

export async function rotateServerToken(): Promise<{ token: string; token_file?: string }> {
  const res = await fetch(`${baseUrl()}/v1/server/token/rotate`, {
    method: "POST",
    headers: headers(),
  })
  if (!res.ok) await throwApiError(res, "Failed to rotate server token")
  const payload = (await res.json()) as { token?: string; token_file?: string }
  if (!payload.token) throw new Error("Invalid rotate token response")
  return { token: payload.token, token_file: payload.token_file }
}

export async function getAdminPasswordStatus(): Promise<{ configured: boolean; updated_at?: string }> {
  const res = await fetch(`${baseUrl()}/v1/server/admin-password/status`, {
    method: "GET",
    headers: headers(),
  })
  if (!res.ok) await throwApiError(res, "Failed to fetch admin password status")
  const payload = (await res.json()) as { configured?: boolean; updated_at?: string }
  return { configured: Boolean(payload.configured), updated_at: payload.updated_at }
}

export async function setAdminPassword(password: string): Promise<void> {
  const res = await fetch(`${baseUrl()}/v1/server/admin-password/set`, {
    method: "POST",
    headers: headers(),
    body: JSON.stringify({ password }),
  })
  if (!res.ok) await throwApiError(res, "Failed to set admin password")
}

export async function verifyAdminPassword(password: string): Promise<void> {
  const res = await fetch(`${baseUrl()}/v1/server/admin-password/verify`, {
    method: "POST",
    headers: headers(),
    body: JSON.stringify({ password }),
  })
  if (!res.ok) await throwApiError(res, "Failed to verify admin password")
}

export async function logServerAuditEvent(
  eventType: string,
  payload: Record<string, unknown>
): Promise<void> {
  const res = await fetch(`${baseUrl()}/v1/server/audit/event`, {
    method: "POST",
    headers: headers(),
    body: JSON.stringify({ event_type: eventType, payload }),
  })
  if (!res.ok) await throwApiError(res, "Failed to write audit event")
}

export interface SecurityAuditEvent {
  timestamp: string
  event_type: string
  payload: Record<string, unknown>
}

export async function fetchSecurityAuditEvents(
  adminPassword?: string,
  limit = 50
): Promise<SecurityAuditEvent[]> {
  const res = await fetch(`${baseUrl()}/v1/server/audit/security-events`, {
    method: "POST",
    headers: headers(),
    body: JSON.stringify({
      admin_password: adminPassword ?? "",
      limit,
    }),
  })
  if (!res.ok) await throwApiError(res, "Failed to fetch security audit events")
  const payload = (await res.json()) as {
    events?: Array<{
      timestamp?: string
      event_type?: string
      payload?: Record<string, unknown>
    }>
  }
  return (payload.events ?? []).map((row) => ({
    timestamp: row.timestamp ?? "",
    event_type: row.event_type ?? "",
    payload: row.payload ?? {},
  }))
}

export async function recoverServerToken(adminPassword: string): Promise<{ token: string; token_file?: string }> {
  const res = await fetch(`${baseUrl()}/v1/server/token/recover`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ admin_password: adminPassword }),
  })
  if (!res.ok) await throwApiError(res, "Failed to recover server token")
  const payload = (await res.json()) as { token?: string; token_file?: string }
  if (!payload.token) throw new Error("Invalid recover token response")
  return { token: payload.token, token_file: payload.token_file }
}

export async function fetchUiSettingsProfile(): Promise<Record<string, unknown>> {
  const res = await fetch(`${baseUrl()}/v1/server/ui-settings`, {
    method: "GET",
    headers: headers(),
  })
  if (!res.ok) await throwApiError(res, "Failed to fetch UI settings profile")
  const payload = (await res.json()) as { settings?: Record<string, unknown> }
  return payload.settings ?? {}
}

export interface ServerSetupStatus {
  ready: boolean
  checks: {
    token_configured: boolean
    enabled_provider_present: boolean
    enabled_provider_has_key: boolean
    role_routing_valid: boolean
    delegation_reachable: boolean
  }
  details: {
    enabled_providers: string[]
    enabled_providers_with_keys: string[]
    invalid_routes: string[]
    delegation_status: string
    delegation_detail: string
  }
}

export interface RemoteAccessStatus {
  enabled: boolean
  admin_password_configured: boolean
  mode_label: string
  public_url: string
  launch_command: string
  rollback_command: string
  summary: string
  warnings: string[]
  steps: string[]
  profile: {
    enabled: boolean
    mode: "lan" | "tailscale" | "cloudflare" | "manual_proxy"
    bind_host: string
    bind_port: number
    public_base_url: string
    notes: string
    updated_at: string
  }
}

export interface RemoteAccessHealthReport {
  generated_at: string
  enabled: boolean
  summary: {
    passed: number
    failed: number
    skipped: number
    total: number
  }
  checks: Array<{
    name: string
    url: string
    reachable: boolean
    status: "PASS" | "FAIL" | "SKIP"
    detail: string
    remediation: string
    latency_ms: number
  }>
}

export async function fetchServerSetupStatus(): Promise<ServerSetupStatus> {
  const res = await fetch(`${baseUrl()}/v1/server/setup-status`, {
    method: "GET",
    headers: headers(),
  })
  if (!res.ok) await throwApiError(res, "Failed to fetch setup status")
  const payload = (await res.json()) as Partial<ServerSetupStatus>
  return {
    ready: Boolean(payload.ready),
    checks: {
      token_configured: Boolean(payload.checks?.token_configured),
      enabled_provider_present: Boolean(payload.checks?.enabled_provider_present),
      enabled_provider_has_key: Boolean(payload.checks?.enabled_provider_has_key),
      role_routing_valid: Boolean(payload.checks?.role_routing_valid),
      delegation_reachable: Boolean(payload.checks?.delegation_reachable),
    },
    details: {
      enabled_providers: Array.isArray(payload.details?.enabled_providers) ? payload.details!.enabled_providers : [],
      enabled_providers_with_keys: Array.isArray(payload.details?.enabled_providers_with_keys)
        ? payload.details!.enabled_providers_with_keys
        : [],
      invalid_routes: Array.isArray(payload.details?.invalid_routes) ? payload.details!.invalid_routes : [],
      delegation_status: String(payload.details?.delegation_status ?? ""),
      delegation_detail: String(payload.details?.delegation_detail ?? ""),
    },
  }
}

export async function fetchRemoteAccessStatus(): Promise<RemoteAccessStatus> {
  const res = await fetch(`${baseUrl()}/v1/server/remote-access/status`, {
    method: "GET",
    headers: headers(),
  })
  if (!res.ok) await throwApiError(res, "Failed to fetch remote access status")
  return (await res.json()) as RemoteAccessStatus
}

export async function configureRemoteAccess(payload: {
  admin_password?: string
  enabled?: boolean
  mode: "lan" | "tailscale" | "cloudflare" | "manual_proxy"
  bind_host: string
  bind_port: number
  public_base_url?: string
  notes?: string
}): Promise<RemoteAccessStatus> {
  const res = await fetch(`${baseUrl()}/v1/server/remote-access/configure`, {
    method: "POST",
    headers: headers(),
    body: JSON.stringify(payload),
  })
  if (!res.ok) await throwApiError(res, "Failed to configure remote access")
  return (await res.json()) as RemoteAccessStatus
}

export async function revokeRemoteAccess(payload: { admin_password?: string } = {}): Promise<RemoteAccessStatus> {
  const res = await fetch(`${baseUrl()}/v1/server/remote-access/revoke`, {
    method: "POST",
    headers: headers(),
    body: JSON.stringify(payload),
  })
  if (!res.ok) await throwApiError(res, "Failed to revoke remote access")
  return (await res.json()) as RemoteAccessStatus
}

export async function runRemoteAccessHealth(): Promise<RemoteAccessHealthReport> {
  const res = await fetch(`${baseUrl()}/v1/server/remote-access/health`, {
    method: "POST",
    headers: headers(),
  })
  if (!res.ok) await throwApiError(res, "Failed to run remote access health probe")
  return (await res.json()) as RemoteAccessHealthReport
}

export async function saveUiSettingsProfile(settings: Record<string, unknown>): Promise<Record<string, unknown>> {
  const res = await fetch(`${baseUrl()}/v1/server/ui-settings`, {
    method: "PUT",
    headers: headers(),
    body: JSON.stringify({ settings }),
  })
  if (!res.ok) await throwApiError(res, "Failed to save UI settings profile")
  const payload = (await res.json()) as { settings?: Record<string, unknown> }
  return payload.settings ?? {}
}

export interface McpServerConfig {
  name: string
  transport: "stdio" | "http" | "sse" | "ws"
  enabled: boolean
  command: string
  args: string[]
  url: string
  headers: Record<string, string>
  header_env_refs: Record<string, string>
  declared_tools: string[]
}

export interface McpHealthCheck {
  name: string
  transport: string
  enabled: boolean
  status: "PASS" | "FAIL" | "SKIP"
  reachable: boolean
  latency_ms: number
  detail: string
  error_code: string
  tools: string[]
  remediation: string
}

export interface McpHealthReport {
  generated_at: string
  summary: {
    passed: number
    failed: number
    skipped: number
    total: number
  }
  checks: McpHealthCheck[]
}

export async function fetchMcpServers(): Promise<McpServerConfig[]> {
  const res = await fetch(`${baseUrl()}/v1/server/mcp/servers`, {
    method: "GET",
    headers: headers(),
  })
  if (!res.ok) await throwApiError(res, "Failed to fetch MCP servers")
  const payload = (await res.json()) as { servers?: Partial<McpServerConfig>[] }
  return Array.isArray(payload.servers)
    ? payload.servers.map((row) => ({
        name: String(row.name ?? ""),
        transport: (["stdio", "http", "sse", "ws"].includes(String(row.transport)) ? String(row.transport) : "stdio") as
          | "stdio"
          | "http"
          | "sse"
          | "ws",
        enabled: Boolean(row.enabled),
        command: String(row.command ?? ""),
        args: Array.isArray(row.args) ? row.args.map((x) => String(x)) : [],
        url: String(row.url ?? ""),
        headers:
          row.headers && typeof row.headers === "object"
            ? Object.fromEntries(
                Object.entries(row.headers as Record<string, unknown>).map(([k, v]) => [String(k), String(v ?? "")])
              )
            : {},
        header_env_refs:
          row.header_env_refs && typeof row.header_env_refs === "object"
            ? Object.fromEntries(
                Object.entries(row.header_env_refs as Record<string, unknown>).map(([k, v]) => [String(k), String(v ?? "")])
              )
            : {},
        declared_tools: Array.isArray(row.declared_tools) ? row.declared_tools.map((x) => String(x)) : [],
      }))
    : []
}

export async function saveMcpServers(servers: McpServerConfig[]): Promise<McpServerConfig[]> {
  const res = await fetch(`${baseUrl()}/v1/server/mcp/servers`, {
    method: "PUT",
    headers: headers(),
    body: JSON.stringify({ servers }),
  })
  if (!res.ok) await throwApiError(res, "Failed to save MCP servers")
  const payload = (await res.json()) as { servers?: Partial<McpServerConfig>[] }
  return Array.isArray(payload.servers)
    ? payload.servers.map((row) => ({
        name: String(row.name ?? ""),
        transport: (["stdio", "http", "sse", "ws"].includes(String(row.transport)) ? String(row.transport) : "stdio") as
          | "stdio"
          | "http"
          | "sse"
          | "ws",
        enabled: Boolean(row.enabled),
        command: String(row.command ?? ""),
        args: Array.isArray(row.args) ? row.args.map((x) => String(x)) : [],
        url: String(row.url ?? ""),
        headers:
          row.headers && typeof row.headers === "object"
            ? Object.fromEntries(
                Object.entries(row.headers as Record<string, unknown>).map(([k, v]) => [String(k), String(v ?? "")])
              )
            : {},
        header_env_refs:
          row.header_env_refs && typeof row.header_env_refs === "object"
            ? Object.fromEntries(
                Object.entries(row.header_env_refs as Record<string, unknown>).map(([k, v]) => [String(k), String(v ?? "")])
              )
            : {},
        declared_tools: Array.isArray(row.declared_tools) ? row.declared_tools.map((x) => String(x)) : [],
      }))
    : []
}

export async function runMcpHealth(
  includeDisabled = false,
  probeTools = true,
  serverNames: string[] = []
): Promise<McpHealthReport> {
  const res = await fetch(`${baseUrl()}/v1/server/mcp/health`, {
    method: "POST",
    headers: headers(),
    body: JSON.stringify({
      include_disabled: includeDisabled,
      probe_tools: probeTools,
      server_names: serverNames,
    }),
  })
  if (!res.ok) await throwApiError(res, "Failed to run MCP health checks")
  const payload = (await res.json()) as Partial<McpHealthReport>
  return {
    generated_at: String(payload.generated_at ?? ""),
    summary: {
      passed: Number(payload.summary?.passed ?? 0),
      failed: Number(payload.summary?.failed ?? 0),
      skipped: Number(payload.summary?.skipped ?? 0),
      total: Number(payload.summary?.total ?? 0),
    },
    checks: Array.isArray(payload.checks)
      ? payload.checks.map((row) => ({
          name: String((row as McpHealthCheck).name ?? ""),
          transport: String((row as McpHealthCheck).transport ?? ""),
          enabled: Boolean((row as McpHealthCheck).enabled),
          status: (String((row as McpHealthCheck).status ?? "FAIL").toUpperCase() as "PASS" | "FAIL" | "SKIP"),
          reachable: Boolean((row as McpHealthCheck).reachable),
          latency_ms: Number((row as McpHealthCheck).latency_ms ?? 0),
          detail: String((row as McpHealthCheck).detail ?? ""),
          error_code: String((row as McpHealthCheck).error_code ?? "none"),
          tools: Array.isArray((row as McpHealthCheck).tools) ? ((row as McpHealthCheck).tools as string[]) : [],
          remediation: String((row as McpHealthCheck).remediation ?? ""),
        }))
      : [],
  }
}

export interface ServerDoctorCheck {
  name: string
  status: "PASS" | "FAIL" | "SKIP"
  detail: string
  latency_ms: number
}

export interface ServerDoctorReport {
  generated_at: string
  summary: {
    passed: number
    failed: number
    skipped: number
    total: number
  }
  checks: ServerDoctorCheck[]
}

export async function runServerDoctor(smokeProviders = true, governance = false): Promise<ServerDoctorReport> {
  const res = await fetch(`${baseUrl()}/v1/server/doctor`, {
    method: "POST",
    headers: headers(),
    body: JSON.stringify({ smoke_providers: smokeProviders, governance }),
  })
  if (!res.ok) await throwApiError(res, "Failed to run doctor")
  const payload = (await res.json()) as Partial<ServerDoctorReport>
  return {
    generated_at: String(payload.generated_at ?? ""),
    summary: {
      passed: Number(payload.summary?.passed ?? 0),
      failed: Number(payload.summary?.failed ?? 0),
      skipped: Number(payload.summary?.skipped ?? 0),
      total: Number(payload.summary?.total ?? 0),
    },
    checks: Array.isArray(payload.checks)
      ? payload.checks.map((row) => ({
          name: String((row as ServerDoctorCheck).name ?? ""),
          status: (String((row as ServerDoctorCheck).status ?? "FAIL").toUpperCase() as "PASS" | "FAIL" | "SKIP"),
          detail: String((row as ServerDoctorCheck).detail ?? ""),
          latency_ms: Number((row as ServerDoctorCheck).latency_ms ?? 0),
        }))
      : [],
  }
}
