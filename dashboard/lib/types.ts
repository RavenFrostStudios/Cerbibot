export type Mode =
  | "single"
  | "critique"
  | "debate"
  | "consensus"
  | "council"
  | "retrieval"

export interface ChatMessage {
  role: "user" | "assistant"
  content: string
  metadata?: {
    mode?: string
    provider?: string
    tokens?: number
    cost?: number
    duration_ms?: number
    status?: string
    warnings?: string[]
    tool_outputs?: Array<Record<string, unknown>>
  }
}

export interface Session {
  id: string
  project_id?: string
  title?: string
  created_at?: string
  messages?: ChatMessage[]
  message_count?: number
}

export interface CostData {
  today: number
  month: number
  budget_remaining: number
  monthly_budget_total?: number
  per_provider: Record<string, number>
  monthly_spend_per_provider?: Record<string, number>
  monthly_budget_per_provider?: Record<string, number>
  requests_today?: number
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

export interface ProviderHealth {
  name: string
  status: "healthy" | "degraded" | "down"
  latency_ms: number
  error_rate?: number
}

export interface HealthData {
  providers: ProviderHealth[]
  budget_ok: boolean
}

export interface MemoryEntry {
  id: string | number
  statement: string
  source_type?: string
  confidence?: number
  ttl?: number
  ttl_days?: number
  created_at?: string
}

export interface ArtifactRun {
  id: string
  date: string
  query: string
  mode: string
  cost: number
  guardian_flags?: string[]
  steps?: ArtifactStep[]
}

export interface ArtifactStep {
  name: string
  content: string
  metadata?: Record<string, unknown>
  cost?: number
  duration_ms?: number
  tool_calls?: ToolCall[]
}

export interface ToolCall {
  approval_id?: string
  tool_name: string
  arguments: Record<string, unknown>
  risk_level: "low" | "medium" | "high"
  reason?: string
  status?: "pending" | "approved" | "denied" | "consumed"
  result?: string
  approved?: boolean
}

export interface Skill {
  id: string
  name: string
  description: string
  risk_level: "low" | "medium" | "high"
  enabled: boolean
  manifest?: Record<string, unknown>
  capabilities?: string[]
  last_test?: string
  input_schema?: Record<string, unknown>
  checksum?: string
  signature_verified?: boolean
}

export interface SkillCatalogEntry {
  id: string
  title: string
  description: string
  trust: string
  official: boolean
  tested: "smoke" | "schema"
  risk_level: "low" | "medium" | "high"
  workflow_text: string
  installed: boolean
  enabled: boolean
  signature_verified: boolean
  checksum: string
}

export interface RunCheckpoint {
  stage?: string
  note?: string
  progress?: number
}

export interface RunBlocker {
  blocker_id: string
  code?: string
  message?: string
  severity?: "low" | "medium" | "high" | string
  status?: "open" | "resolved" | string
}

export interface RunInfo {
  run_id: string
  endpoint?: "ask" | "chat" | string
  status?: "running" | "completed" | "failed" | "paused" | "waiting" | "resuming" | "blocked" | string
  session_id?: string | null
  created_at?: string
  updated_at?: string
  last_heartbeat_at?: string
  heartbeat_count?: number
  resume_count?: number
  error_detail?: string
  checkpoint?: RunCheckpoint
  dependencies?: string[]
  blockers?: RunBlocker[]
}

export interface RunTrigger {
  trigger_id: string
  name: string
  enabled: boolean
  project_id?: string
  session_id?: string
  mode: Mode
  provider?: string
  message: string
  tools: boolean
  fact_check: boolean
  assistant_name?: string
  assistant_instructions?: string
  strict_profile: boolean
  web_assist_mode: "off" | "auto" | "confirm"
  interval_minutes: number
  next_run_at?: string
  secret: string
  webhook_path: string
  webhook_url: string
  last_triggered_at?: string
  last_run_id?: string
  updated_at?: string
}

export interface RunDagNode {
  id: string
  label?: string
  status?: string
  details?: Record<string, unknown>
}

export interface RunDagEdge {
  from: string
  to: string
  type?: "depends_on" | "blocked_by" | string
}

export interface RunDag {
  nodes: RunDagNode[]
  edges: RunDagEdge[]
  count?: number
}
