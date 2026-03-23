"use client"

// Settings are stored in localStorage per the spec (API token + base URL)
const SETTINGS_KEY = "mmy-settings"

export interface ProviderConfigEntry {
  name: string
  model: string
  enabled: boolean
}

export interface McpServerEntry {
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

export interface RoleRoutingConfig {
  critique: {
    drafter_provider: string
    critic_provider: string
    refiner_provider: string
  }
  debate: {
    debater_a_provider: string
    debater_b_provider: string
    judge_provider: string
    synthesizer_provider: string
  }
  consensus: {
    adjudicator_provider: string
  }
  council: {
    specialist_roles: {
      coding: string
      security: string
      writing: string
      factual: string
    }
    synthesizer_provider: string
  }
}

export interface AppSettings {
  apiBaseUrl: string
  bearerToken: string
  activeProjectId: string
  assistantName: string
  assistantInstructions: string
  assistantStrictProfile: boolean
  debugRetrievalWarnings: boolean
  webMaxSources: number
  webAssistMode: "off" | "auto" | "confirm"
  retrievalAnswerStyle: "concise_ranked" | "full_details" | "source_first"
  theme: "cyberpunk" | "stealth" | "light"
  sessionPanelSide: "left" | "right"
  sessionSortOrder: "newest" | "oldest"
  chatMode: "single" | "critique" | "debate" | "consensus" | "council" | "retrieval"
  chatToolsEnabled: boolean
  chatFactCheckEnabled: boolean
  chatAutoMemoryEnabled: boolean
  chatAutoMemoryBySession: Record<string, boolean>
  providers: ProviderConfigEntry[]
  providerMonthlyBudgets: Record<string, number>
  mcpServers: McpServerEntry[]
  roleRouting: RoleRoutingConfig
  strictReliabilityLastReport?: {
    provider?: string
    model?: string
    runs: number
    passed: number
    failed: number
    checked_at: string
  }
}

export type SyncableAppSettings = Omit<AppSettings, "bearerToken">

export const DEFAULT_SETTINGS: AppSettings = {
  apiBaseUrl: "http://localhost:8100",
  bearerToken: "",
  activeProjectId: "default",
  assistantName: "CerbiBot",
  assistantInstructions: "",
  assistantStrictProfile: false,
  debugRetrievalWarnings: false,
  webMaxSources: 3,
  webAssistMode: "off",
  retrievalAnswerStyle: "concise_ranked",
  theme: "cyberpunk",
  sessionPanelSide: "left",
  sessionSortOrder: "newest",
  chatMode: "single",
  chatToolsEnabled: false,
  chatFactCheckEnabled: false,
  chatAutoMemoryEnabled: false,
  chatAutoMemoryBySession: {},
  providers: [
    { name: "openai", model: "gpt-4.1", enabled: false },
    { name: "anthropic", model: "claude-3-7-sonnet-latest", enabled: false },
    { name: "google", model: "gemini-2.5-flash", enabled: false },
    { name: "xai", model: "grok-3", enabled: false },
    { name: "groq", model: "llama-3.3-70b", enabled: false },
    { name: "local", model: "qwen3-vl:4b", enabled: true },
  ],
  providerMonthlyBudgets: {},
  mcpServers: [],
  roleRouting: {
    critique: {
      drafter_provider: "local",
      critic_provider: "local",
      refiner_provider: "local",
    },
    debate: {
      debater_a_provider: "local",
      debater_b_provider: "local",
      judge_provider: "local",
      synthesizer_provider: "local",
    },
    consensus: {
      adjudicator_provider: "local",
    },
    council: {
      specialist_roles: {
        coding: "local",
        security: "local",
        writing: "local",
        factual: "local",
      },
      synthesizer_provider: "local",
    },
  },
}

export function getSettings(): AppSettings {
  if (typeof window === "undefined") return DEFAULT_SETTINGS
  try {
    const raw = localStorage.getItem(SETTINGS_KEY)
    if (!raw) return DEFAULT_SETTINGS
    return { ...DEFAULT_SETTINGS, ...JSON.parse(raw) }
  } catch {
    return DEFAULT_SETTINGS
  }
}

export function saveSettings(settings: Partial<AppSettings>) {
  if (typeof window === "undefined") return
  const current = getSettings()
  const next = { ...current, ...settings }
  localStorage.setItem(SETTINGS_KEY, JSON.stringify(next))
  window.dispatchEvent(new Event("mmy-settings-changed"))
}

export function mergeSettings(current: AppSettings, patch: Partial<AppSettings>): AppSettings {
  return { ...current, ...patch }
}

export function pickSyncableSettings(settings: AppSettings): SyncableAppSettings {
  const { bearerToken: _ignored, ...syncable } = settings
  return syncable
}
