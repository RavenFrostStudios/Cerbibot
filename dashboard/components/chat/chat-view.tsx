"use client"

import { useState, useRef, useEffect, useCallback, useMemo } from "react"
import { SessionList } from "./session-list"
import { MessageBubble } from "./message-bubble"
import { ChatInput } from "./chat-input"
import { ToolApprovalModal } from "./tool-approval-modal"
import {
  addMemory,
  fetchSessions,
  fetchSession,
  fetchToolApprovals,
  fetchRun,
  fetchRuns,
  fetchRunsDag,
  streamRunEvents,
  deleteRun,
  clearRuns,
  deleteSession,
  streamChat,
  updateRunDependencies,
  approveToolApproval,
  denyToolApproval,
  getAdminPasswordStatus,
  logServerAuditEvent,
  resumeRun,
  verifyAdminPassword,
  suggestMemoryFromSession,
} from "@/lib/api"
import { getSettings, saveSettings } from "@/lib/settings"
import {
  getRouteActiveSessionId,
  getRouteSidebarOpen,
  setRouteActiveSessionId,
  setRouteSidebarOpen,
} from "@/lib/chat-state"
import type { ChatMessage, Session, Mode, ToolCall, RunInfo, RunBlocker, RunDag } from "@/lib/types"
import { MessageSquare } from "lucide-react"

interface PendingApprovalContext {
  sessionId: string
  text: string
  mode: Mode
  tools: boolean
  factCheck: boolean
}

interface MemorySuggestionCandidate {
  statement: string
  source_type: string
  confidence: number
  reason: string
  session_id: string
  project_id?: string
}

function shouldAutoPromptMemory(text: string): boolean {
  const normalized = text.trim().toLowerCase()
  return /\b(remember|memorize|store this|save this|note this)\b/.test(normalized)
}

function extractRememberPhrase(text: string): string | null {
  const raw = text.trim()
  if (!raw) return null
  const exact = raw.match(/remember\s+this\s+exact\s+phrase\s*:\s*(.+)$/i)
  if (exact?.[1]) return exact[1].trim()
  const generic = raw.match(/remember\s*:\s*(.+)$/i)
  if (generic?.[1]) return generic[1].trim()
  return null
}

function isYes(text: string): boolean {
  const normalized = text.trim().toLowerCase()
  return normalized === "yes" || normalized === "y"
}

function isNo(text: string): boolean {
  const normalized = text.trim().toLowerCase()
  return normalized === "no" || normalized === "n"
}

function isRunBlocked(row: RunInfo | null | undefined): boolean {
  if (!row || !Array.isArray(row.blockers)) return false
  return row.blockers.some((item) => String(item?.status ?? "open").toLowerCase() === "open")
}

function parseIsoTime(value?: string | null): number | null {
  if (!value) return null
  const ts = Date.parse(value)
  return Number.isFinite(ts) ? ts : null
}

function runElapsedMs(row: RunInfo | null | undefined, nowMs: number): number | null {
  if (!row) return null
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

function isRunStalled(row: RunInfo | null | undefined, nowMs: number): boolean {
  if (!row) return false
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

function isTerminalRunStatus(status?: string | null): boolean {
  const normalized = String(status ?? "").toLowerCase()
  return ["completed", "failed", "cancelled", "blocked"].includes(normalized)
}

function trimUiText(value: string, max = 48): string {
  const normalized = value.replace(/\s+/g, " ").trim()
  if (normalized.length <= max) return normalized
  return `${normalized.slice(0, Math.max(1, max - 1)).trimEnd()}...`
}

function humanizeUiError(raw: string): string {
  const msg = String(raw || "").trim()
  const lower = msg.toLowerCase()
  if (!msg) return "Something went wrong."
  if (lower.includes("timed out") || lower.includes("timeout")) {
    return "Request timed out. Please retry, or use a smaller/faster model."
  }
  if (lower.includes("missing bearer token") || lower.includes("401")) {
    return "Authentication is missing or expired. Reconnect in Settings and try again."
  }
  if (lower.includes("network") || lower.includes("connection")) {
    return "Network connection failed. Please check connectivity and retry."
  }
  if (lower.startsWith("failed to ")) return msg.replace(/^failed to\s+/i, "Could not ")
  return msg
}

function humanizeChatStatus(raw: unknown): string | undefined {
  const normalized = String(raw ?? "").trim().toLowerCase()
  if (!normalized) return undefined
  if (normalized === "ok") return "Response complete."
  if (normalized === "failed") return "Request failed."
  if (normalized === "pending") return "Tool approval required before execution."
  if (normalized === "running") return "Running tool step..."
  if (normalized === "queued") return "Queued."
  if (normalized === "waiting") return "Waiting for tool output..."
  return String(raw)
}

function requestFailureText(detail: string): { status: string; message: string } {
  const normalized = humanizeUiError(detail || "Unknown connection error")
  const timeoutHint =
    normalized.toLowerCase().includes("timeout") || normalized.toLowerCase().includes("timed out")
      ? " The selected provider/model timed out. For local Ollama, use a smaller model, reduce complexity, or increase local timeout."
      : ""
  const message = `Request failed. ${normalized}${timeoutHint}`.trim()
  return { status: message, message }
}

export function ChatView() {
  const [sessions, setSessions] = useState<Session[]>([])
  const [activeSession, setActiveSession] = useState<string | null>(null)
  const [messages, setMessages] = useState<ChatMessage[]>([])
  const [isStreaming, setIsStreaming] = useState(false)
  const [statusText, setStatusText] = useState<string | undefined>()
  const [pendingTool, setPendingTool] = useState<ToolCall | null>(null)
  const [pendingContext, setPendingContext] = useState<PendingApprovalContext | null>(null)
  const [pendingApprovals, setPendingApprovals] = useState<ToolCall[]>([])
  const [activeRunId, setActiveRunId] = useState<string | null>(null)
  const [activeRun, setActiveRun] = useState<RunInfo | null>(null)
  const [recentRuns, setRecentRuns] = useState<RunInfo[]>([])
  const [runFilter, setRunFilter] = useState<"all" | "running" | "blocked" | "failed" | "completed">("all")
  const [resumeBusy, setResumeBusy] = useState(false)
  const [dagEditOpen, setDagEditOpen] = useState(false)
  const [dagDepsText, setDagDepsText] = useState("")
  const [dagBlockers, setDagBlockers] = useState<RunBlocker[]>([])
  const [dagSaveBusy, setDagSaveBusy] = useState(false)
  const [runDagOpen, setRunDagOpen] = useState(false)
  const [runDagLoading, setRunDagLoading] = useState(false)
  const [runDagError, setRunDagError] = useState<string | null>(null)
  const [runDag, setRunDag] = useState<RunDag | null>(null)
  const [runDagFocusOnly, setRunDagFocusOnly] = useState(false)
  const [runDagFocusId, setRunDagFocusId] = useState("")
  const [runDagFocusDepth, setRunDagFocusDepth] = useState<1 | 2>(1)
  const [sessionPanelSide, setSessionPanelSide] = useState<"left" | "right">("left")
  const [sessionSortOrder, setSessionSortOrder] = useState<"newest" | "oldest">("newest")
  const [sessionSearchQuery, setSessionSearchQuery] = useState("")
  const [sidebarOpen, setSidebarOpen] = useState(true)
  const [uiStateHydrated, setUiStateHydrated] = useState(false)
  const [exportStatus, setExportStatus] = useState<string | null>(null)
  const [exportBusy, setExportBusy] = useState(false)
  const [exportAuthOpen, setExportAuthOpen] = useState(false)
  const [exportAuthPassword, setExportAuthPassword] = useState("")
  const [exportAuthError, setExportAuthError] = useState<string | null>(null)
  const [runNowMs, setRunNowMs] = useState<number>(Date.now())
  const [memorySuggestion, setMemorySuggestion] = useState<MemorySuggestionCandidate | null>(null)
  const [memorySuggestBusy, setMemorySuggestBusy] = useState(false)
  const [memorySaveBusy, setMemorySaveBusy] = useState(false)
  const [memoryDuplicateNotice, setMemoryDuplicateNotice] = useState(false)
  const [runsStreamConnected, setRunsStreamConnected] = useState(false)
  const [projectId, setProjectId] = useState<string>("default")
  const [chatAutoMemoryDefault, setChatAutoMemoryDefault] = useState<boolean>(false)
  const [chatAutoMemoryBySession, setChatAutoMemoryBySession] = useState<Record<string, boolean>>({})
  const exportAuthResolverRef = useRef<((value: string | null) => void) | null>(null)
  const messagesEndRef = useRef<HTMLDivElement>(null)
  const abortRef = useRef<AbortController | null>(null)
  const pendingApprovalsInFlightRef = useRef(false)
  const runsInFlightRef = useRef(false)
  const activeRunInFlightRef = useRef(false)

  const loadSessions = useCallback(async () => {
    try {
      const data = await fetchSessions(projectId)
      setSessions(data)
      setActiveSession((current) => {
        if (!current) return current
        return data.some((session) => session.id === current) ? current : null
      })
    } catch {
      // API not available, use empty
    }
  }, [projectId])

  const loadPendingApprovals = useCallback(async () => {
    if (pendingApprovalsInFlightRef.current) return
    pendingApprovalsInFlightRef.current = true
    try {
      const rows = await fetchToolApprovals("pending")
      setPendingApprovals(rows)
    } catch {
      setPendingApprovals([])
    } finally {
      pendingApprovalsInFlightRef.current = false
    }
  }, [])

  const loadRuns = useCallback(async () => {
    if (runsInFlightRef.current) return
    runsInFlightRef.current = true
    try {
      const rows = await fetchRuns({ limit: 20 })
      setRecentRuns(rows)
    } catch {
      setRecentRuns([])
    } finally {
      runsInFlightRef.current = false
    }
  }, [])

  const upsertRecentRun = useCallback((row: RunInfo) => {
    setRecentRuns((prev) => {
      const idx = prev.findIndex((item) => item.run_id === row.run_id)
      if (idx >= 0) {
        const next = [...prev]
        next[idx] = row
        return next
      }
      return [row, ...prev].slice(0, 50)
    })
  }, [])

  useEffect(() => {
    loadSessions()
    loadPendingApprovals()
    loadRuns()
  }, [loadSessions, loadPendingApprovals, loadRuns])

  useEffect(() => {
    const stop = streamRunEvents(
      (event) => {
        if (event.type === "ready" || event.type === "heartbeat") {
          setRunsStreamConnected(true)
          return
        }
        if (event.type === "upsert" && event.run) {
          setRunsStreamConnected(true)
          upsertRecentRun(event.run)
          if (activeRunId && event.run.run_id === activeRunId) {
            setActiveRun(event.run)
          }
          return
        }
        if (event.type === "delete" && event.run_id) {
          setRunsStreamConnected(true)
          setRecentRuns((prev) => prev.filter((row) => row.run_id !== event.run_id))
          if (activeRunId === event.run_id) setActiveRun(null)
        }
      },
      () => {
        setRunsStreamConnected(false)
      }
    )
    return () => stop()
  }, [activeRunId, upsertRecentRun])

  useEffect(() => {
    const update = () => {
      const cfg = getSettings()
      setSessionPanelSide(cfg.sessionPanelSide)
      setSessionSortOrder(cfg.sessionSortOrder)
      setProjectId((cfg.activeProjectId || "default").trim() || "default")
      setChatAutoMemoryDefault(Boolean(cfg.chatAutoMemoryEnabled))
      setChatAutoMemoryBySession({ ...(cfg.chatAutoMemoryBySession ?? {}) })
    }
    update()
    window.addEventListener("mmy-settings-changed", update)
    return () => window.removeEventListener("mmy-settings-changed", update)
  }, [])

  useEffect(() => {
    const id = window.setInterval(() => {
      if (document.hidden) return
      loadPendingApprovals()
      if (!runsStreamConnected) loadRuns()
    }, isStreaming ? 8000 : 30000)
    return () => window.clearInterval(id)
  }, [loadPendingApprovals, loadRuns, isStreaming, runsStreamConnected])

  useEffect(() => {
    const id = window.setInterval(() => setRunNowMs(Date.now()), 1000)
    return () => window.clearInterval(id)
  }, [])

  useEffect(() => {
    if (!activeRunId) return
    if (!isStreaming && isTerminalRunStatus(activeRun?.status)) return
    if (runsStreamConnected) return
    const tick = async () => {
      if (activeRunInFlightRef.current) return
      activeRunInFlightRef.current = true
      try {
        const row = await fetchRun(activeRunId)
        setActiveRun(row)
      } catch {
        // ignore transient API failures
      } finally {
        activeRunInFlightRef.current = false
      }
    }
    void tick()
    const id = window.setInterval(() => {
      if (document.hidden) return
      void tick()
    }, isStreaming ? 2500 : 10000)
    return () => window.clearInterval(id)
  }, [activeRunId, isStreaming, activeRun?.status, runsStreamConnected])

  useEffect(() => {
    setActiveSession(getRouteActiveSessionId())
    setSidebarOpen(getRouteSidebarOpen())
    setUiStateHydrated(true)
  }, [])

  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: "smooth" })
  }, [messages])

  useEffect(() => {
    if (!uiStateHydrated) return
    setRouteActiveSessionId(activeSession)
  }, [activeSession, uiStateHydrated])

  useEffect(() => {
    if (!uiStateHydrated) return
    setRouteSidebarOpen(sidebarOpen)
  }, [sidebarOpen, uiStateHydrated])

  const handleNewSession = () => {
    const id = `session-${Date.now()}`
    const newSession: Session = { id, title: "New Chat" }
    setSessions((prev) => [newSession, ...prev])
    setActiveSession(id)
    setMessages([])
    if ("__draft__" in chatAutoMemoryBySession) {
      const next = { ...chatAutoMemoryBySession }
      delete next.__draft__
      setChatAutoMemoryBySession(next)
      saveSettings({ chatAutoMemoryBySession: next })
    }
  }

  const handleDeleteSession = async (id: string) => {
    try {
      await deleteSession(id, projectId)
    } catch {
      // swallow
    }
    setSessions((prev) => prev.filter((s) => s.id !== id))
    if (id in chatAutoMemoryBySession) {
      const next = { ...chatAutoMemoryBySession }
      delete next[id]
      setChatAutoMemoryBySession(next)
      saveSettings({ chatAutoMemoryBySession: next })
    }
    if (activeSession === id) {
      setActiveSession(null)
      setMessages([])
    }
  }

  const sendMessage = (
    text: string,
    mode: Mode,
    tools: boolean,
    factCheck: boolean,
    options?: {
      appendUser?: boolean
      toolApprovalId?: string
      forcedSessionId?: string
      autoMemoryPrompt?: boolean
      autoMemorySave?: boolean
    }
  ) => {
    const appendUser = options?.appendUser ?? true
    const sessionId = options?.forcedSessionId ?? activeSession ?? `session-${Date.now()}`
    const runId =
      (typeof crypto !== "undefined" && "randomUUID" in crypto
        ? `run-${crypto.randomUUID()}`
        : `run-${Date.now()}`) || `run-${Date.now()}`
    const creatingSession = !activeSession && !options?.forcedSessionId
    const autoMemoryPrompt = options?.autoMemoryPrompt ?? false
    const autoMemorySave = options?.autoMemorySave ?? false
    const requestStartedAt = Date.now()

    if (creatingSession) {
      if ("__draft__" in chatAutoMemoryBySession) {
        const next = { ...chatAutoMemoryBySession, [sessionId]: chatAutoMemoryBySession.__draft__ }
        delete next.__draft__
        setChatAutoMemoryBySession(next)
        saveSettings({ chatAutoMemoryBySession: next })
      }
    }

    if (appendUser) {
      const userMsg: ChatMessage = { role: "user", content: text }
      setMessages((prev) => [...prev, userMsg])
    }
    setIsStreaming(true)
    setStatusText(mode === "critique" ? "Drafting..." : "Generating...")
    setActiveRunId(runId)
    setActiveRun({
      run_id: runId,
      endpoint: "chat",
      status: "running",
      session_id: sessionId,
      checkpoint: { stage: "queued" },
    })

    let assistantContent = ""
    let metadata: Record<string, unknown> = {}

    const controller = streamChat(
      {
        session_id: sessionId,
        project_id: projectId,
        run_id: runId,
        message: text,
        mode,
        tools: tools || undefined,
        fact_check: factCheck || undefined,
        tool_approval_id: options?.toolApprovalId,
        assistant_name: getSettings().assistantName || undefined,
        assistant_instructions: getSettings().assistantInstructions || undefined,
        strict_profile: getSettings().assistantStrictProfile || undefined,
        web_assist_mode: getSettings().webAssistMode || undefined,
      },
      (chunk) => {
        assistantContent += chunk
        setMessages((prev) => {
          const updated = [...prev]
          const lastIdx = updated.length - 1
          if (
            lastIdx >= 0 &&
            updated[lastIdx].role === "assistant" &&
            !updated[lastIdx].metadata?.mode
          ) {
            updated[lastIdx] = {
              ...updated[lastIdx],
              content: assistantContent,
            }
          } else {
            updated.push({
              role: "assistant",
              content: assistantContent,
            })
          }
          return updated
        })
      },
      (meta) => {
        if (meta.status) {
          setStatusText(humanizeChatStatus(meta.status))
        }
        if (meta.tool_call) {
          setPendingTool(meta.tool_call as ToolCall)
        }
        if (meta.pending_tool) {
          setPendingTool(meta.pending_tool as ToolCall)
          setPendingContext({ sessionId, text, mode, tools, factCheck })
          loadPendingApprovals()
        }
        metadata = { ...metadata, ...meta }
        if (typeof meta.run_id === "string" && meta.run_id.trim()) {
          setActiveRunId(meta.run_id)
        }
      },
      () => {
        setIsStreaming(false)
        setStatusText(undefined)
        setMessages((prev) => {
          const updated = [...prev]
          const lastIdx = updated.length - 1
          if (lastIdx >= 0 && updated[lastIdx].role === "assistant") {
            updated[lastIdx] = {
              ...updated[lastIdx],
              content:
                updated[lastIdx].content ||
                (metadata.pending_tool ? "Tool approval required before execution." : updated[lastIdx].content),
              metadata: {
                mode: metadata.mode as string,
                provider: metadata.provider as string,
                tokens: metadata.tokens as number,
                cost: metadata.cost as number,
                duration_ms: Math.max(0, Date.now() - requestStartedAt),
                warnings: Array.isArray(metadata.warnings) ? (metadata.warnings as string[]) : [],
                tool_outputs: Array.isArray(metadata.tool_outputs)
                  ? (metadata.tool_outputs as Array<Record<string, unknown>>)
                  : [],
              },
            }
          }
          return updated
        })
        if (creatingSession) {
          const title = text.slice(0, 40) + (text.length > 40 ? "..." : "")
          setSessions((prev) => [{ id: sessionId, title }, ...prev])
          setActiveSession(sessionId)
        }
        loadSessions()
        loadPendingApprovals()
        loadRuns()
        void fetchRun(runId)
          .then((row) => setActiveRun(row))
          .catch(() => {
            // swallow
          })
        if (autoMemoryPrompt && !metadata.pending_tool) {
          void (async () => {
            try {
              const result = await suggestMemoryFromSession(sessionId, undefined, projectId)
              if (!result.suggested || !result.candidate) {
                if (autoMemorySave) {
                  const phrase = extractRememberPhrase(text)
                  if (phrase) {
                    await addMemory(phrase, "chat_user_stated", `chat.session:${sessionId}`, projectId)
                    setMemorySuggestion(null)
                    setMemoryDuplicateNotice(false)
                    setMessages((prev) => [
                      ...prev,
                      { role: "assistant", content: "Saved to memory automatically." },
                    ])
                    setStatusText("Saved memory automatically.")
                    return
                  }
                }
                if (result.reason === "already_stored") {
                  setMemorySuggestion(null)
                  setMemoryDuplicateNotice(true)
                  setStatusText("Memory already stored.")
                  setMessages((prev) => [
                    ...prev,
                    { role: "assistant", content: "That memory is already stored." },
                  ])
                  return
                }
                setMemorySuggestion(null)
                setMemoryDuplicateNotice(false)
                setStatusText(result.reason ? `No memory suggestion (${result.reason}).` : "No memory suggestion.")
                return
              }
              if (autoMemorySave) {
                await addMemory(
                  result.candidate.statement,
                  result.candidate.source_type,
                  `chat.session:${result.candidate.session_id}`,
                  result.candidate.project_id || projectId
                )
                setMemorySuggestion(null)
                setMemoryDuplicateNotice(false)
                setMessages((prev) => [
                  ...prev,
                  {
                    role: "assistant",
                    content: "Saved to memory automatically.",
                  },
                ])
                setStatusText("Saved memory automatically.")
                return
              }
              setMemoryDuplicateNotice(false)
              setMemorySuggestion(result.candidate)
              setMessages((prev) => [
                ...prev,
                {
                  role: "assistant",
                  content: "I can save that to memory. Reply YES to save or NO to skip.",
                },
              ])
              setStatusText("Memory suggestion ready. Reply YES to save or NO to skip.")
            } catch (err) {
              const msg = humanizeUiError(err instanceof Error ? err.message : "failed")
              setStatusText(`Memory suggestion failed (${msg}).`)
              setMessages((prev) => [
                ...prev,
                { role: "assistant", content: `I could not save memory automatically (${msg}).` },
              ])
            }
          })()
        }
      },
      (err) => {
        setIsStreaming(false)
        setActiveRun((prev) =>
          prev
            ? { ...prev, status: "failed", error_detail: err.message, checkpoint: { stage: "client_error" } }
            : prev
        )
        const failure = requestFailureText(err.message || "Unknown connection error")
        setStatusText(failure.status)
        setMessages((prev) => [
          ...prev,
          {
            role: "assistant",
            content: failure.message,
          },
        ])
      }
    )

    abortRef.current = controller
  }

  const handleSend = (text: string, mode: Mode, tools: boolean, factCheck: boolean, autoMemory: boolean) => {
    if (memorySuggestion && !isStreaming) {
      if (isYes(text)) {
        const candidate = memorySuggestion
        setMessages((prev) => [...prev, { role: "user", content: text }])
        setMemorySaveBusy(true)
        setMemorySuggestion(null)
        setMemoryDuplicateNotice(false)
        void addMemory(
          candidate.statement,
          candidate.source_type,
          `chat.session:${candidate.session_id}`,
          candidate.project_id || projectId
        )
          .then(() => {
            setMessages((prev) => [
              ...prev,
              { role: "assistant", content: "Saved. I will remember that." },
            ])
            setStatusText("Saved suggested memory.")
          })
          .catch((err) => {
            const msg = err instanceof Error ? err.message : "failed"
            setMessages((prev) => [
              ...prev,
              { role: "assistant", content: `I could not save that memory (${msg}).` },
            ])
            setStatusText(`Failed to save suggested memory (${msg}).`)
          })
          .finally(() => {
            setMemorySaveBusy(false)
            window.setTimeout(() => setStatusText(undefined), 2500)
          })
        return
      }
      if (isNo(text)) {
        setMessages((prev) => [
          ...prev,
          { role: "user", content: text },
          { role: "assistant", content: "Okay, I will not store that memory." },
        ])
        setMemorySuggestion(null)
        setMemoryDuplicateNotice(false)
        setStatusText("Skipped memory suggestion.")
        window.setTimeout(() => setStatusText(undefined), 2000)
        return
      }
    }
    setMemorySuggestion(null)
    setMemoryDuplicateNotice(false)
    sendMessage(text, mode, tools, factCheck, {
      autoMemoryPrompt: shouldAutoPromptMemory(text),
      autoMemorySave: autoMemory && shouldAutoPromptMemory(text),
    })
  }

  const autoMemorySessionKey = activeSession ?? "__draft__"
  const autoMemoryEnabled =
    chatAutoMemoryBySession[autoMemorySessionKey] ?? chatAutoMemoryDefault

  const handleAutoMemoryChange = (next: boolean) => {
    const current = chatAutoMemoryBySession[autoMemorySessionKey]
    if (current === next) return
    const updated = { ...chatAutoMemoryBySession, [autoMemorySessionKey]: next }
    setChatAutoMemoryBySession(updated)
    saveSettings({ chatAutoMemoryBySession: updated })
  }

  const handleSuggestMemory = async () => {
    if (!activeSession || memorySuggestBusy || isStreaming) return
    setMemorySuggestBusy(true)
    setStatusText("Generating memory suggestion...")
    try {
      const result = await suggestMemoryFromSession(activeSession, undefined, projectId)
      if (!result.suggested || !result.candidate) {
        setMemorySuggestion(null)
        if (result.reason === "already_stored") {
          setMemoryDuplicateNotice(true)
          setStatusText("Memory already stored.")
        } else {
          setMemoryDuplicateNotice(false)
          setStatusText(result.reason ? `No memory suggestion (${result.reason}).` : "No memory suggestion.")
        }
        return
      }
      setMemoryDuplicateNotice(false)
      setMemorySuggestion(result.candidate)
      setStatusText("Memory suggestion ready. Review and save if useful.")
    } catch (err) {
      const msg = humanizeUiError(err instanceof Error ? err.message : "failed")
      setStatusText(`Memory suggestion failed (${msg}).`)
    } finally {
      setMemorySuggestBusy(false)
      window.setTimeout(() => setStatusText(undefined), 3000)
    }
  }

  const handleSaveSuggestedMemory = async () => {
    if (!memorySuggestion || memorySaveBusy) return
    setMemorySaveBusy(true)
    try {
      await addMemory(
        memorySuggestion.statement,
        memorySuggestion.source_type,
        `chat.session:${memorySuggestion.session_id}`,
        memorySuggestion.project_id || projectId
      )
      setStatusText("Saved suggested memory.")
      setMemorySuggestion(null)
      setMemoryDuplicateNotice(false)
    } catch (err) {
      const msg = humanizeUiError(err instanceof Error ? err.message : "failed")
      setStatusText(`Could not save suggested memory (${msg}).`)
    } finally {
      setMemorySaveBusy(false)
      window.setTimeout(() => setStatusText(undefined), 2500)
    }
  }

  const handleResumeRun = async (runId: string) => {
    if (resumeBusy) return
    setResumeBusy(true)
    try {
      setStatusText(`Resuming ${runId.slice(0, 12)}...`)
      const resumed = await resumeRun(runId)
      const resumedRun = resumed.run
      if (resumedRun?.run_id) {
        setActiveRunId(resumedRun.run_id)
        setActiveRun(resumedRun)
      }
      const resumePayload = resumed.resume
      const resumeSessionId = resumePayload?.session_id
      if (resumeSessionId) {
        setActiveSession(resumeSessionId)
        try {
          const history = await fetchSession(resumeSessionId, projectId)
          setMessages(history)
        } catch {
          // ignore session reload failure
        }
      } else if (resumePayload?.result?.answer || resumePayload?.answer) {
        const answer = resumePayload?.result?.answer ?? resumePayload?.answer ?? ""
        if (answer) {
          setMessages((prev) => [...prev, { role: "assistant", content: answer }])
        }
      }
      setStatusText("Run resumed.")
      await Promise.all([loadRuns(), loadSessions()])
    } catch (err) {
      const msg = humanizeUiError(err instanceof Error ? err.message : "Failed to resume run")
      setStatusText(`Could not resume run (${msg}).`)
    } finally {
      setResumeBusy(false)
      window.setTimeout(() => setStatusText(undefined), 3000)
    }
  }

  const handleResumeLatestFailedRun = async () => {
    const failed = (await fetchRuns({ status: "failed", limit: 1 }))[0]
    if (!failed?.run_id) {
      setStatusText("No failed runs to resume.")
      window.setTimeout(() => setStatusText(undefined), 3000)
      return
    }
    await handleResumeRun(failed.run_id)
  }

  const handleDeleteRun = async (runId: string) => {
    try {
      await deleteRun(runId)
      if (activeRunId === runId) {
        setActiveRunId(null)
        setActiveRun(null)
      }
      await loadRuns()
      setStatusText("Run deleted.")
    } catch (err) {
      const msg = humanizeUiError(err instanceof Error ? err.message : "Failed to delete run")
      setStatusText(`Could not delete run (${msg}).`)
    } finally {
      window.setTimeout(() => setStatusText(undefined), 2000)
    }
  }

  const copyRunId = async (runId: string) => {
    try {
      await navigator.clipboard.writeText(runId)
      setStatusText(`Copied run ID: ${runId.slice(0, 18)}`)
    } catch {
      setStatusText("Could not copy run ID.")
    } finally {
      window.setTimeout(() => setStatusText(undefined), 2000)
    }
  }

  const copyArtifactHint = async (runId: string) => {
    const hint = `Run ID: ${runId}\nArtifacts page: /artifacts\nLocal artifact default path: ~/.mmo/artifacts`
    try {
      await navigator.clipboard.writeText(hint)
      setStatusText("Copied artifact folder hint.")
    } catch {
      setStatusText("Could not copy artifact folder hint.")
    } finally {
      window.setTimeout(() => setStatusText(undefined), 2500)
    }
  }

  const openArtifacts = async (runId: string) => {
    await copyRunId(runId)
    window.location.href = "/artifacts"
  }

  const handleClearCompletedRuns = async () => {
    try {
      const result = await clearRuns("completed")
      await loadRuns()
      if (activeRun?.status === "completed") {
        setActiveRunId(null)
        setActiveRun(null)
      }
      setStatusText(`Cleared ${result.deleted} completed run(s).`)
    } catch (err) {
      const msg = humanizeUiError(err instanceof Error ? err.message : "Failed to clear completed runs")
      setStatusText(`Could not clear completed runs (${msg}).`)
    } finally {
      window.setTimeout(() => setStatusText(undefined), 2500)
    }
  }

  const parseDepsText = (text: string): string[] =>
    Array.from(
      new Set(
        text
          .split(/[\n,]+/)
          .map((item) => item.trim())
          .filter(Boolean)
      )
    )

  const openDagEditor = () => {
    if (!activeRun?.run_id) return
    setDagEditOpen(true)
    setDagDepsText(Array.isArray(activeRun.dependencies) ? activeRun.dependencies.join(", ") : "")
    setDagBlockers(
      Array.isArray(activeRun.blockers)
        ? activeRun.blockers.map((item, idx) => ({
            blocker_id: item.blocker_id || `blocker-${idx + 1}`,
            code: item.code || "",
            message: item.message || "",
            severity: item.severity || "medium",
            status: item.status || "open",
          }))
        : []
    )
  }

  const closeDagEditor = () => {
    setDagEditOpen(false)
    setDagDepsText("")
    setDagBlockers([])
  }

  const addDagBlocker = () => {
    setDagBlockers((prev) => [
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

  const updateDagBlocker = (index: number, field: keyof RunBlocker, value: string) => {
    setDagBlockers((prev) =>
      prev.map((item, idx) => (idx === index ? { ...item, [field]: value } : item))
    )
  }

  const removeDagBlocker = (index: number) => {
    setDagBlockers((prev) => prev.filter((_, idx) => idx !== index))
  }

  const saveDagEditor = async () => {
    if (!activeRun?.run_id || dagSaveBusy) return
    setDagSaveBusy(true)
    setStatusText(`Saving DAG metadata for ${activeRun.run_id.slice(0, 12)}...`)
    try {
      const dependencies = parseDepsText(dagDepsText)
      const blockers = dagBlockers
        .map((item, idx) => ({
          blocker_id: item.blocker_id || `blocker-${idx + 1}`,
          code: String(item.code || "").trim(),
          message: String(item.message || "").trim(),
          severity: String(item.severity || "medium"),
          status: String(item.status || "open"),
        }))
        .filter((item) => item.code || item.message)
      const updated = await updateRunDependencies(activeRun.run_id, { dependencies, blockers })
      setActiveRun(updated)
      await loadRuns()
      setStatusText("Run DAG metadata saved.")
      closeDagEditor()
    } catch (err) {
      const msg = humanizeUiError(err instanceof Error ? err.message : "Failed to save run DAG metadata")
      setStatusText(`Could not save run dependencies (${msg}).`)
    } finally {
      setDagSaveBusy(false)
      window.setTimeout(() => setStatusText(undefined), 2500)
    }
  }

  const openRunDagModal = async () => {
    setRunDagOpen(true)
    setRunDagLoading(true)
    setRunDagError(null)
    setRunDagFocusOnly(false)
    setRunDagFocusId(activeRun?.run_id ?? "")
    setRunDagFocusDepth(1)
    try {
      const dag = await fetchRunsDag(150)
      setRunDag(dag)
    } catch (err) {
      const msg = humanizeUiError(err instanceof Error ? err.message : "Failed to load run DAG")
      setRunDagError(`Could not load run graph (${msg}).`)
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

  const approveById = async (approvalId: string, context?: PendingApprovalContext | null) => {
    try {
      await approveToolApproval(approvalId)
      setPendingTool(null)
      loadPendingApprovals()
      if (!context) return
      setStatusText("Approval granted. Re-running with tool...")
      sendMessage(
        context.text,
        context.mode,
        context.tools,
        context.factCheck,
        {
          appendUser: false,
          toolApprovalId: approvalId,
          forcedSessionId: context.sessionId,
        }
      )
      setPendingContext(null)
    } catch (err) {
      const msg = humanizeUiError(err instanceof Error ? err.message : "Approval failed")
      setStatusText(`Tool approval failed. ${msg}`)
      setMessages((prev) => [...prev, { role: "assistant", content: `Tool approval failed. ${msg}` }])
    }
  }

  const denyById = async (approvalId: string) => {
    try {
      await denyToolApproval(approvalId)
    } catch {
      // swallow backend deny errors for UX continuity
    }
    setPendingTool(null)
    setPendingContext(null)
    loadPendingApprovals()
    setMessages((prev) => [
      ...prev,
      { role: "assistant", content: "Tool execution was denied. Continuing without tool output." },
    ])
    setStatusText("Tool execution was denied.")
  }

  const handleApproveTool = async () => {
    if (!pendingTool?.approval_id || !pendingContext) {
      setPendingTool(null)
      return
    }
    await approveById(pendingTool.approval_id, pendingContext)
  }

  const handleDenyTool = async () => {
    if (pendingTool?.approval_id) await denyById(pendingTool.approval_id)
    else setPendingTool(null)
  }

  useEffect(() => {
    if (!activeSession) return
    let cancelled = false
    fetchSession(activeSession, projectId)
      .then((history) => {
        if (!cancelled) setMessages(history)
      })
      .catch(() => {
        if (!cancelled) setMessages([])
      })
    return () => {
      cancelled = true
    }
  }, [activeSession, projectId])

  const handlePanelSideChange = (side: "left" | "right") => {
    setSessionPanelSide(side)
    saveSettings({ sessionPanelSide: side })
  }

  const handleSortOrderChange = (order: "newest" | "oldest") => {
    setSessionSortOrder(order)
    saveSettings({ sessionSortOrder: order })
  }

  const downloadJson = (filename: string, payload: unknown) => {
    const blob = new Blob([JSON.stringify(payload, null, 2)], { type: "application/json" })
    const url = URL.createObjectURL(blob)
    const anchor = document.createElement("a")
    anchor.href = url
    anchor.download = filename
    anchor.click()
    URL.revokeObjectURL(url)
  }

  const safeLogAudit = async (eventType: string, payload: Record<string, unknown>) => {
    try {
      await logServerAuditEvent(eventType, payload)
    } catch {
      // non-blocking: export action should still complete even if audit logging is unavailable
    }
  }

  const promptExportAdminPassword = () =>
    new Promise<string | null>((resolve) => {
      exportAuthResolverRef.current = resolve
      setExportAuthPassword("")
      setExportAuthError(null)
      setExportAuthOpen(true)
    })

  const closeExportAuthPrompt = (value: string | null) => {
    setExportAuthOpen(false)
    const resolve = exportAuthResolverRef.current
    exportAuthResolverRef.current = null
    if (resolve) resolve(value)
  }

  const ensureExportAuthorized = async (scope: "active" | "all"): Promise<boolean> => {
    const status = await getAdminPasswordStatus()
    await safeLogAudit("ui.export_attempt", { scope, auth_required: status.configured })
    if (!status.configured) return true
    const password = await promptExportAdminPassword()
    if (!password) {
      await safeLogAudit("ui.export_cancelled", { scope, reason: "no_password" })
      return false
    }
    try {
      await verifyAdminPassword(password)
      await safeLogAudit("ui.export_auth_ok", { scope })
      return true
    } catch (err) {
      const msg = err instanceof Error ? err.message : "failed"
      await safeLogAudit("ui.export_auth_failed", { scope, message: msg.slice(0, 120) })
      throw err
    }
  }

  const handleExportActive = async () => {
    if (!activeSession || exportBusy) return
    setExportBusy(true)
    setExportStatus(null)
    try {
      const allowed = await ensureExportAuthorized("active")
      if (!allowed) {
        setExportStatus("Export cancelled.")
        return
      }
      const history = await fetchSession(activeSession, projectId)
      const info = sessions.find((row) => row.id === activeSession)
      const payload = {
        exported_at: new Date().toISOString(),
        sessions: [
          {
            session_id: activeSession,
            project_id: projectId,
            title: info?.title ?? activeSession,
            messages: history,
          },
        ],
      }
      downloadJson(`mmy-session-${activeSession}.json`, payload)
      await safeLogAudit("ui.export_success", { scope: "active", count: 1 })
      setExportStatus("Exported active session.")
    } catch (err) {
      const msg = humanizeUiError(err instanceof Error ? err.message : "failed")
      await safeLogAudit("ui.export_failed", { scope: "active", message: msg.slice(0, 120) })
      setExportStatus(`Could not export session (${msg}).`)
    } finally {
      setExportBusy(false)
    }
  }

  const handleExportAll = async () => {
    if (exportBusy) return
    setExportBusy(true)
    setExportStatus(null)
    try {
      const allowed = await ensureExportAuthorized("all")
      if (!allowed) {
        setExportStatus("Export cancelled.")
        return
      }
      const rows = await Promise.all(
        sessions.map(async (session) => ({
          session_id: session.id,
          project_id: projectId,
          title: session.title ?? session.id,
          messages: await fetchSession(session.id, projectId),
        }))
      )
      downloadJson("mmy-sessions-all.json", {
        exported_at: new Date().toISOString(),
        sessions: rows,
      })
      await safeLogAudit("ui.export_success", { scope: "all", count: rows.length })
      setExportStatus(`Exported ${rows.length} session(s).`)
    } catch (err) {
      const msg = humanizeUiError(err instanceof Error ? err.message : "failed")
      await safeLogAudit("ui.export_failed", { scope: "all", message: msg.slice(0, 120) })
      setExportStatus(`Could not export sessions (${msg}).`)
    } finally {
      setExportBusy(false)
    }
  }

  const displayedSessions = useMemo(() => {
    const query = sessionSearchQuery.trim().toLowerCase()
    const filtered = query
      ? sessions.filter((session) =>
          `${session.title ?? ""} ${session.id}`.toLowerCase().includes(query)
        )
      : sessions
    return sessionSortOrder === "newest" ? filtered : [...filtered].reverse()
  }, [sessionSearchQuery, sessionSortOrder, sessions])

  const latestFailedRun = useMemo(
    () => recentRuns.find((row) => String(row.status) === "failed") ?? null,
    [recentRuns]
  )
  const displayedRuns = useMemo(() => {
    if (runFilter === "all") return recentRuns
    if (runFilter === "blocked") return recentRuns.filter((row) => isRunBlocked(row))
    return recentRuns.filter((row) => String(row.status) === runFilter)
  }, [recentRuns, runFilter])
  const activeElapsed = useMemo(() => formatElapsed(runElapsedMs(activeRun, runNowMs)), [activeRun, runNowMs])
  const activeStalled = useMemo(() => isRunStalled(activeRun, runNowMs), [activeRun, runNowMs])

  const sidebar = (
    <div
      className={`${sidebarOpen ? "w-56 min-w-[14rem]" : "w-0 min-w-0 overflow-hidden"} transition-all duration-200`}
    >
      <SessionList
        sessions={displayedSessions}
        searchQuery={sessionSearchQuery}
        sortOrder={sessionSortOrder}
        exportStatus={exportStatus}
        exportBusy={exportBusy}
        pendingApprovals={pendingApprovals}
        side={sessionPanelSide}
        activeId={activeSession}
        onSearchQueryChange={setSessionSearchQuery}
        onSortOrderChange={handleSortOrderChange}
        onSelect={(id) => {
          setActiveSession(id)
        }}
        onNew={handleNewSession}
        onDelete={handleDeleteSession}
        onExportActive={() => {
          void handleExportActive()
        }}
        onExportAll={() => {
          void handleExportAll()
        }}
        onApproveTool={(approvalId) => {
          const context =
            pendingTool?.approval_id === approvalId
              ? pendingContext
              : null
          void approveById(approvalId, context)
        }}
        onDenyTool={(approvalId) => {
          void denyById(approvalId)
        }}
        onRefreshApprovals={() => {
          void loadPendingApprovals()
        }}
      />
    </div>
  )

  return (
    <div className="flex h-full">
      {/* Session sidebar */}
      {sessionPanelSide === "left" && sidebar}

      {/* Main chat area */}
      <div className="flex flex-1 flex-col">
        {/* Header */}
        <div className="flex items-center gap-3 border-b border-border px-4 py-3">
          <button
            type="button"
            onClick={() => {
              const next = !sidebarOpen
              setSidebarOpen(next)
              setRouteSidebarOpen(next)
            }}
            className="font-mono text-xs text-muted-foreground hover:text-foreground"
            aria-label="Toggle session list"
          >
            {sidebarOpen ? "[<<]" : "[>>]"}
          </button>
          <span className="font-mono text-xs tracking-wider text-muted-foreground">
            {activeSession ? `SESSION: ${activeSession.slice(0, 16)}` : "New Query"}
          </span>
          <div className="ml-auto flex items-center gap-1">
            {(resumeBusy || latestFailedRun) && (
              <button
                type="button"
                onClick={() => {
                  void handleResumeLatestFailedRun()
                }}
                disabled={resumeBusy || !latestFailedRun}
                className="rounded border border-neon-yellow/40 bg-neon-yellow/10 px-1.5 py-0.5 font-mono text-[10px] text-neon-yellow disabled:opacity-50"
                aria-label="Resume latest failed run"
                title={latestFailedRun ? `Resume ${latestFailedRun.run_id}` : "No failed runs"}
              >
                {resumeBusy ? "Resuming..." : "Resume Failed"}
              </button>
            )}
            <button
              type="button"
              onClick={() => handlePanelSideChange("left")}
              className={`rounded border px-1.5 py-0.5 font-mono text-[10px] ${sessionPanelSide === "left" ? "border-neon-cyan/40 text-neon-cyan" : "border-border text-muted-foreground"}`}
              aria-label="Set session panel left"
            >
              LEFT
            </button>
            <button
              type="button"
              onClick={() => handlePanelSideChange("right")}
              className={`rounded border px-1.5 py-0.5 font-mono text-[10px] ${sessionPanelSide === "right" ? "border-neon-cyan/40 text-neon-cyan" : "border-border text-muted-foreground"}`}
              aria-label="Set session panel right"
            >
              RIGHT
            </button>
          </div>
        </div>

        <div className="border-b border-border px-4 py-2">
          <div className="flex flex-wrap items-center gap-3 font-mono text-[10px] text-muted-foreground">
            <span>
              RUN:{" "}
              <span className="text-foreground">
                {activeRun?.run_id ? activeRun.run_id.slice(0, 16) : "none"}
              </span>
            </span>
            <span>
              STATUS:{" "}
              <span
                className={
                  isRunBlocked(activeRun)
                    ? "text-neon-yellow"
                    : activeRun?.status === "failed"
                    ? "text-neon-red"
                    : activeRun?.status === "completed"
                      ? "text-neon-green"
                      : "text-neon-cyan"
                }
              >
                {isRunBlocked(activeRun) ? "blocked" : activeRun?.status ?? "idle"}
              </span>
            </span>
            <span>HEARTBEATS: {activeRun?.heartbeat_count ?? 0}</span>
            <span>ELAPSED: {activeElapsed}</span>
            {activeStalled && <span className="text-neon-yellow">Stale heartbeat</span>}
            <span>STAGE: {activeRun?.checkpoint?.stage ?? "n/a"}</span>
            <span>
              Blockers:{" "}
              {isRunBlocked(activeRun)
                ? (activeRun?.blockers ?? []).filter((item) => String(item?.status ?? "open").toLowerCase() === "open").length
                : 0}
            </span>
            {activeRun?.error_detail && (
              <span className="max-w-[40ch] truncate text-neon-red" title={activeRun.error_detail}>
                ERROR: {activeRun.error_detail}
              </span>
            )}
          </div>
          <div className="mt-2 rounded border border-border bg-background/20 p-2">
            <div className="flex flex-wrap items-center gap-2">
              <span className="font-mono text-[10px] tracking-wider text-muted-foreground">
                RUNS
              </span>
              <button
                type="button"
                onClick={() => setRunFilter("all")}
                className={`rounded border px-1.5 py-0.5 font-mono text-[10px] ${runFilter === "all" ? "border-neon-cyan/40 text-neon-cyan" : "border-border text-muted-foreground"}`}
              >
                ALL
              </button>
              <button
                type="button"
                onClick={() => setRunFilter("running")}
                className={`rounded border px-1.5 py-0.5 font-mono text-[10px] ${runFilter === "running" ? "border-neon-cyan/40 text-neon-cyan" : "border-border text-muted-foreground"}`}
              >
                RUNNING
              </button>
              <button
                type="button"
                onClick={() => setRunFilter("failed")}
                className={`rounded border px-1.5 py-0.5 font-mono text-[10px] ${runFilter === "failed" ? "border-neon-red/40 text-neon-red" : "border-border text-muted-foreground"}`}
              >
                FAILED
              </button>
              <button
                type="button"
                onClick={() => setRunFilter("blocked")}
                className={`rounded border px-1.5 py-0.5 font-mono text-[10px] ${runFilter === "blocked" ? "border-neon-yellow/40 text-neon-yellow" : "border-border text-muted-foreground"}`}
              >
                BLOCKED
              </button>
              <button
                type="button"
                onClick={() => setRunFilter("completed")}
                className={`rounded border px-1.5 py-0.5 font-mono text-[10px] ${runFilter === "completed" ? "border-neon-green/40 text-neon-green" : "border-border text-muted-foreground"}`}
              >
                COMPLETED
              </button>
              <button
                type="button"
                onClick={() => {
                  void loadRuns()
                }}
                className="ml-auto rounded border border-border px-1.5 py-0.5 font-mono text-[10px] text-muted-foreground hover:text-foreground"
              >
                REFRESH
              </button>
              <button
                type="button"
                onClick={() => {
                  void openRunDagModal()
                }}
                className="rounded border border-border px-1.5 py-0.5 font-mono text-[10px] text-muted-foreground hover:text-neon-cyan"
                title="View run dependency graph"
              >
                VIEW DAG
              </button>
              <button
                type="button"
                onClick={() => {
                  if (dagEditOpen) closeDagEditor()
                  else openDagEditor()
                }}
                disabled={!activeRun?.run_id}
                className="rounded border border-border px-1.5 py-0.5 font-mono text-[10px] text-muted-foreground hover:text-neon-cyan disabled:opacity-50"
                title={activeRun?.run_id ? "Edit dependencies/blockers for active run" : "No active run selected"}
              >
                {dagEditOpen ? "Close DAG" : "Edit DAG"}
              </button>
              <button
                type="button"
                onClick={() => {
                  void handleClearCompletedRuns()
                }}
                className="rounded border border-border px-1.5 py-0.5 font-mono text-[10px] text-muted-foreground hover:text-foreground"
                title="Clear completed runs"
              >
                CLEAR COMPLETED
              </button>
            </div>
            {dagEditOpen && (
              <div className="mt-2 rounded border border-border/70 bg-background/40 p-2">
                <p className="mb-1 font-mono text-[10px] text-muted-foreground">DEPENDENCIES (comma/newline separated run IDs)</p>
                <textarea
                  value={dagDepsText}
                  onChange={(e) => setDagDepsText(e.target.value)}
                  rows={2}
                  className="w-full rounded border border-border bg-background/60 px-2 py-1 font-mono text-[10px] text-foreground focus:outline-none"
                  placeholder="run-a, run-b"
                />
                <div className="mb-1 mt-2 flex items-center gap-2">
                  <p className="font-mono text-[10px] text-muted-foreground">Blockers</p>
                  <button
                    type="button"
                    onClick={addDagBlocker}
                    className="rounded border border-border px-1.5 py-0.5 font-mono text-[10px] text-muted-foreground hover:text-foreground"
                  >
                    ADD
                  </button>
                </div>
                <div className="grid gap-2">
                  {dagBlockers.map((item, idx) => (
                    <div key={`${item.blocker_id}-${idx}`} className="grid grid-cols-1 gap-2 rounded border border-border/70 p-2 md:grid-cols-[1fr_2fr_auto_auto_auto]">
                      <input
                        type="text"
                        value={item.code ?? ""}
                        onChange={(e) => updateDagBlocker(idx, "code", e.target.value)}
                        placeholder="code"
                        className="rounded border border-border bg-background/60 px-2 py-1 font-mono text-[10px] text-foreground focus:outline-none"
                      />
                      <input
                        type="text"
                        value={item.message ?? ""}
                        onChange={(e) => updateDagBlocker(idx, "message", e.target.value)}
                        placeholder="message"
                        className="rounded border border-border bg-background/60 px-2 py-1 font-mono text-[10px] text-foreground focus:outline-none"
                      />
                      <select
                        value={String(item.severity ?? "medium")}
                        onChange={(e) => updateDagBlocker(idx, "severity", e.target.value)}
                        className="rounded border border-border bg-background/60 px-2 py-1 font-mono text-[10px] text-foreground focus:outline-none"
                      >
                        <option value="low">low</option>
                        <option value="medium">medium</option>
                        <option value="high">high</option>
                      </select>
                      <select
                        value={String(item.status ?? "open")}
                        onChange={(e) => updateDagBlocker(idx, "status", e.target.value)}
                        className="rounded border border-border bg-background/60 px-2 py-1 font-mono text-[10px] text-foreground focus:outline-none"
                      >
                        <option value="open">open</option>
                        <option value="resolved">resolved</option>
                      </select>
                      <button
                        type="button"
                        onClick={() => removeDagBlocker(idx)}
                        className="rounded border border-border px-1.5 py-1 font-mono text-[10px] text-muted-foreground hover:text-neon-red"
                      >
                        REMOVE
                      </button>
                    </div>
                  ))}
                  {dagBlockers.length === 0 && (
                    <p className="font-mono text-[10px] text-muted-foreground">No blockers configured.</p>
                  )}
                </div>
                <div className="mt-2 flex items-center gap-2">
                  <button
                    type="button"
                    onClick={() => {
                      void saveDagEditor()
                    }}
                    disabled={dagSaveBusy}
                    className="rounded border border-neon-cyan/40 bg-neon-cyan/10 px-2 py-1 font-mono text-[10px] text-neon-cyan disabled:opacity-50"
                  >
                    {dagSaveBusy ? "Saving..." : "Save DAG"}
                  </button>
                  <button
                    type="button"
                    onClick={closeDagEditor}
                    className="rounded border border-border px-2 py-1 font-mono text-[10px] text-muted-foreground hover:text-foreground"
                  >
                    CANCEL
                  </button>
                </div>
              </div>
            )}
            <div className="mt-2 grid max-h-36 gap-1 overflow-auto pr-1">
              {displayedRuns.slice(0, 8).map((row) => (
                <div
                  key={row.run_id}
                  className="flex items-center gap-2 rounded border border-border/70 bg-background/30 px-2 py-1"
                >
                  <button
                    type="button"
                    onClick={() => {
                      setActiveRunId(row.run_id)
                      setActiveRun(row)
                    }}
                    className="truncate font-mono text-[10px] text-foreground hover:text-neon-cyan"
                    title={row.run_id}
                  >
                    {row.run_id.slice(0, 18)}
                  </button>
                  <span
                    className={`font-mono text-[10px] ${
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
                  </span>
                  <span className="truncate font-mono text-[10px] text-muted-foreground">
                    {row.checkpoint?.stage ?? "n/a"}
                  </span>
                  <span className="font-mono text-[10px] text-muted-foreground">
                    {formatElapsed(runElapsedMs(row, runNowMs))}
                  </span>
                  {isRunStalled(row, runNowMs) && (
                    <span className="font-mono text-[10px] text-neon-yellow">stalled</span>
                  )}
                  {canRetryRun(row) && (
                    <button
                      type="button"
                      onClick={() => {
                        void handleResumeRun(row.run_id)
                      }}
                      disabled={resumeBusy}
                      className="ml-auto rounded border border-neon-yellow/40 bg-neon-yellow/10 px-1.5 py-0.5 font-mono text-[10px] text-neon-yellow disabled:opacity-50"
                    >
                      RETRY
                    </button>
                  )}
                  <button
                    type="button"
                    onClick={() => {
                      void copyRunId(row.run_id)
                    }}
                    className="rounded border border-border px-1.5 py-0.5 font-mono text-[10px] text-muted-foreground hover:text-foreground"
                    title="Copy run ID"
                  >
                    COPY ID
                  </button>
                  <button
                    type="button"
                    onClick={() => {
                      void openArtifacts(row.run_id)
                    }}
                    className="rounded border border-border px-1.5 py-0.5 font-mono text-[10px] text-muted-foreground hover:text-neon-cyan"
                    title="Open Artifacts page"
                  >
                    ARTIFACTS
                  </button>
                  <button
                    type="button"
                    onClick={() => {
                      void copyArtifactHint(row.run_id)
                    }}
                    className="rounded border border-border px-1.5 py-0.5 font-mono text-[10px] text-muted-foreground hover:text-foreground"
                    title="Copy artifact folder hint"
                  >
                    FOLDER HINT
                  </button>
                  <button
                    type="button"
                    onClick={() => {
                      void handleDeleteRun(row.run_id)
                    }}
                    className="rounded border border-border px-1.5 py-0.5 font-mono text-[10px] text-muted-foreground hover:text-neon-red"
                    title="Delete run"
                  >
                    DELETE
                  </button>
                </div>
              ))}
              {displayedRuns.length === 0 && (
                <p className="font-mono text-[10px] text-muted-foreground">No runs in this filter.</p>
              )}
            </div>
          </div>
        </div>

        {memorySuggestion && (
          <div className="border-b border-border px-4 py-2">
            <div className="rounded border border-neon-green/30 bg-neon-green/5 p-2">
              <p className="font-mono text-[10px] text-neon-green">Memory suggestion</p>
              <p className="mt-1 font-mono text-xs text-foreground">{memorySuggestion.statement}</p>
              <p className="mt-1 font-mono text-[10px] text-muted-foreground">
                source: {memorySuggestion.source_type === "retrieval" ? "web" : memorySuggestion.source_type} • confidence: {memorySuggestion.confidence.toFixed(2)} • reason:{" "}
                {memorySuggestion.reason || "durable"}
              </p>
              <div className="mt-2 flex gap-2">
                <button
                  type="button"
                  onClick={() => {
                    void handleSaveSuggestedMemory()
                  }}
                  disabled={memorySaveBusy}
                  className="rounded border border-neon-green/40 bg-neon-green/10 px-2 py-1 font-mono text-[10px] text-neon-green disabled:opacity-50"
                >
                  {memorySaveBusy ? "Saving..." : "Save memory"}
                </button>
                <button
                  type="button"
                  onClick={() => setMemorySuggestion(null)}
                  className="rounded border border-border px-2 py-1 font-mono text-[10px] text-muted-foreground hover:text-foreground"
                >
                  DISMISS
                </button>
              </div>
            </div>
          </div>
        )}

        {/* Messages */}
        <div className="flex-1 overflow-auto p-4">
          {messages.length === 0 && (
            <div className="flex h-full flex-col items-center justify-center gap-4 text-center">
              <div className="flex h-16 w-16 items-center justify-center rounded-xl neon-border glow-cyan">
                <MessageSquare className="h-8 w-8 text-neon-cyan" />
              </div>
              <div>
                <h2 className="font-mono text-lg font-bold text-foreground">
                  CerbiBot
                </h2>
                <p className="mt-1 text-sm text-muted-foreground">
                  Powered by MMY Orchestrator
                </p>
              </div>
            </div>
          )}
          <div className="mx-auto flex max-w-3xl flex-col gap-4">
            {messages.map((msg, i) => (
              <MessageBubble
                key={`msg-${i}-${msg.role}`}
                message={msg}
                isStreaming={
                  isStreaming &&
                  i === messages.length - 1 &&
                  msg.role === "assistant"
                }
              />
            ))}
            <div ref={messagesEndRef} />
          </div>
        </div>

        {/* Input */}
        <div className="border-t border-border px-3 py-1.5">
          <div className="flex items-center gap-2">
            <button
              type="button"
              onClick={() => {
                void handleSuggestMemory()
              }}
              disabled={!activeSession || isStreaming || memorySuggestBusy}
              className="rounded border border-neon-green/30 bg-neon-green/5 px-2 py-1 font-mono text-[10px] text-neon-green disabled:opacity-50"
            >
              {memorySuggestBusy ? "Suggesting memory..." : "Suggest memory"}
            </button>
            {memoryDuplicateNotice && (
              <span className="rounded border border-neon-cyan/30 bg-neon-cyan/10 px-2 py-1 font-mono text-[10px] text-neon-cyan">
                ALREADY IN MEMORY
              </span>
            )}
          </div>
        </div>
        <ChatInput
          onSend={handleSend}
          autoMemory={autoMemoryEnabled}
          onAutoMemoryChange={handleAutoMemoryChange}
          disabled={isStreaming}
          statusText={statusText}
        />
      </div>

      {sessionPanelSide === "right" && sidebar}

      {/* Tool approval overlay */}
      {pendingTool && (
        <ToolApprovalModal
          toolCall={pendingTool}
          onApprove={handleApproveTool}
          onDeny={handleDenyTool}
        />
      )}

      {exportAuthOpen && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4">
          <div className="w-full max-w-md rounded-lg border border-neon-yellow/40 bg-card p-4 shadow-xl">
            <h3 className="font-mono text-sm font-bold tracking-wider text-foreground">
              ADMIN PASSWORD REQUIRED
            </h3>
            <p className="mt-1 font-mono text-xs text-muted-foreground">
              Enter admin password to authorize export.
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
            {exportAuthError && (
              <p className="mt-2 font-mono text-[10px] text-neon-red">{exportAuthError}</p>
            )}
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
      {runDagOpen && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4">
          <div className="w-full max-w-3xl rounded-lg border border-neon-cyan/30 bg-card p-4 shadow-xl">
            <div className="mb-2 flex items-center justify-between">
              <h3 className="font-mono text-sm font-bold tracking-wider text-foreground">Run DAG</h3>
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
                  <label className="font-mono text-[10px] text-muted-foreground">Focus run</label>
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
                  <span>Nodes: {Array.isArray(focusedRunDag?.nodes) ? focusedRunDag.nodes.length : 0}</span>
                  <span>Edges: {Array.isArray(focusedRunDag?.edges) ? focusedRunDag.edges.length : 0}</span>
                </div>
                <div className="grid max-h-[60vh] grid-cols-1 gap-3 overflow-auto md:grid-cols-2">
                  <div className="rounded border border-border/70 p-2">
                    <p className="mb-1 font-mono text-[10px] text-muted-foreground">Nodes</p>
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
                    <p className="mb-1 font-mono text-[10px] text-muted-foreground">Edges</p>
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
