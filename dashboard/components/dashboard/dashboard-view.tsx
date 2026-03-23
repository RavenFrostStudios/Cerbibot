"use client"

import { useEffect, useState } from "react"
import { fetchCost, fetchHealth, saveUiSettingsProfile } from "@/lib/api"
import type { CostData, HealthData } from "@/lib/types"
import { StatCards } from "./stat-cards"
import { ProviderHealthPanel } from "./provider-health"
import { RouterWeights } from "./router-weights"
import { getSettings, pickSyncableSettings, saveSettings } from "@/lib/settings"
import { formatUsdAdaptive } from "@/lib/utils"
import { RefreshCw, Wallet } from "lucide-react"

export function DashboardView() {
  const [cost, setCost] = useState<CostData | null>(null)
  const [health, setHealth] = useState<HealthData | null>(null)
  const [loading, setLoading] = useState(true)
  const [budgetModalOpen, setBudgetModalOpen] = useState(false)
  const [budgetStatus, setBudgetStatus] = useState<string | null>(null)
  const [budgetDraft, setBudgetDraft] = useState<Record<string, number>>({})

  const openBudgetModal = () => {
    setBudgetDraft({ ...(getSettings().providerMonthlyBudgets ?? {}) })
    setBudgetStatus(null)
    setBudgetModalOpen(true)
  }

  const updateBudgetDraft = (providerName: string, value: string) => {
    const key = providerName.trim()
    if (!key) return
    setBudgetDraft((prev) => {
      const next = { ...prev }
      const trimmed = value.trim()
      if (!trimmed) {
        delete next[key]
      } else {
        const parsed = Number(trimmed)
        if (Number.isFinite(parsed) && parsed >= 0) {
          next[key] = parsed
        }
      }
      return next
    })
  }

  const saveBudgetDraft = async () => {
    const current = getSettings()
    const next = { ...current, providerMonthlyBudgets: budgetDraft }
    saveSettings(next)
    try {
      await saveUiSettingsProfile(pickSyncableSettings(next) as Record<string, unknown>)
      setBudgetStatus("Saved provider monthly budgets.")
      setBudgetModalOpen(false)
      await loadData()
    } catch (err) {
      const msg = err instanceof Error ? err.message : "failed"
      setBudgetStatus(`Saved locally only (${msg}).`)
      setBudgetModalOpen(false)
      await loadData()
    }
  }

  const loadData = async () => {
    setLoading(true)
    try {
      const [c, h] = await Promise.allSettled([fetchCost(), fetchHealth()])
      if (c.status === "fulfilled") setCost(c.value)
      if (h.status === "fulfilled") setHealth(h.value)
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    loadData()
  }, [])

  return (
    <div className="p-6">
      <div className="mb-6 flex items-center justify-between">
        <div>
          <h1 className="font-mono text-lg font-bold tracking-wider text-foreground">
            DASHBOARD
          </h1>
          <p className="text-sm text-muted-foreground">
            System overview and metrics
          </p>
        </div>
        <div className="flex items-center gap-2">
          <button
            type="button"
            onClick={loadData}
            disabled={loading}
            className="flex items-center gap-2 rounded-md border border-border px-3 py-1.5 font-mono text-xs text-muted-foreground transition-colors hover:border-neon-cyan/30 hover:text-neon-cyan disabled:opacity-50"
          >
            <RefreshCw className={`h-3 w-3 ${loading ? "animate-spin" : ""}`} />
            REFRESH
          </button>
          <button
            type="button"
            onClick={openBudgetModal}
            className="flex items-center gap-2 rounded-md border border-neon-cyan/40 bg-neon-cyan/10 px-3 py-1.5 font-mono text-xs text-neon-cyan transition-colors hover:bg-neon-cyan/20"
          >
            <Wallet className="h-3 w-3" />
            BUDGETS
          </button>
        </div>
      </div>

      <StatCards cost={cost} />
      {budgetStatus && (
        <p className="mt-2 font-mono text-[10px] text-muted-foreground">{budgetStatus}</p>
      )}

      <div className="mt-6 grid gap-6 lg:grid-cols-2">
        <ProviderHealthPanel health={health} cost={cost} />
        <RouterWeights cost={cost} />
      </div>

      {budgetModalOpen && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-background/70 px-4 backdrop-blur-sm">
          <div className="w-full max-w-2xl rounded-lg border border-neon-cyan/40 bg-card p-4">
            <div className="mb-3 flex items-center justify-between">
              <h3 className="font-mono text-sm font-bold tracking-wider text-foreground">PROVIDER MONTHLY BUDGETS</h3>
              <button
                type="button"
                onClick={() => setBudgetModalOpen(false)}
                className="rounded border border-border px-2 py-1 font-mono text-[10px] text-muted-foreground hover:text-foreground"
              >
                CLOSE
              </button>
            </div>
            <div className="grid gap-2">
              {Array.from(
                new Set([
                  ...Object.keys(getSettings().providerMonthlyBudgets ?? {}),
                  ...Object.keys(cost?.monthly_spend_per_provider ?? {}),
                  ...((getSettings().providers ?? []).map((p) => p.name.trim()).filter(Boolean)),
                ])
              )
                .sort((a, b) => a.localeCompare(b))
                .map((name) => {
                  const spend = Number(cost?.monthly_spend_per_provider?.[name] ?? 0)
                  const budget = Number(budgetDraft[name] ?? 0)
                  const remaining = Math.max(0, budget - spend)
                  return (
                    <div key={`budget-row-${name}`} className="grid grid-cols-12 items-center gap-2 rounded border border-border bg-background/30 px-2 py-2">
                      <div className="col-span-3 font-mono text-xs text-foreground">{name}</div>
                      <div className="col-span-3 font-mono text-[10px] text-muted-foreground">
                        spent: ${formatUsdAdaptive(spend, { minDecimals: 0, maxDecimals: 5, fallback: "--" })}
                      </div>
                      <div className="col-span-3">
                        <input
                          type="number"
                          min="0"
                          step="0.01"
                          value={budgetDraft[name] ?? ""}
                          onChange={(e) => updateBudgetDraft(name, e.target.value)}
                          className="w-full rounded border border-border bg-background/60 px-2 py-1 font-mono text-xs text-foreground focus:outline-none"
                          placeholder="0.00"
                        />
                      </div>
                      <div className="col-span-3 font-mono text-[10px] text-muted-foreground">
                        left: ${formatUsdAdaptive(remaining, { minDecimals: 0, maxDecimals: 5, fallback: "--" })}
                      </div>
                    </div>
                  )
                })}
            </div>
            <div className="mt-3 flex items-center justify-between">
              <span className="font-mono text-[10px] text-muted-foreground">
                Total allocated: ${formatUsdAdaptive(
                  Object.values(budgetDraft).reduce((sum, value) => sum + Number(value || 0), 0),
                  { minDecimals: 0, maxDecimals: 5, fallback: "--" }
                )}
              </span>
              <button
                type="button"
                onClick={() => void saveBudgetDraft()}
                className="rounded border border-neon-cyan/40 bg-neon-cyan/10 px-3 py-1.5 font-mono text-[10px] text-neon-cyan hover:bg-neon-cyan/20"
              >
                SAVE BUDGETS
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
