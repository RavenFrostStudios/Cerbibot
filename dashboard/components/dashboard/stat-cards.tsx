"use client"

import type { CostData } from "@/lib/types"
import { DollarSign, TrendingUp, Zap } from "lucide-react"
import { formatUsdAdaptive } from "@/lib/utils"

interface StatCardsProps {
  cost: CostData | null
}

const SPARK_HEIGHTS = [18, 14, 16, 9, 20, 17, 12, 19, 15, 13, 21, 11] as const

export function StatCards({ cost }: StatCardsProps) {
  const budgetCap = cost?.monthly_budget_total ?? (cost ? cost.month + cost.budget_remaining : 0)
  const budgetPct = cost
    ? Math.min(
        budgetCap > 0 ? ((cost.month / budgetCap) * 100) : 0,
        100
      )
    : 0

  return (
    <div className="grid gap-4 sm:grid-cols-3">
      {/* Today's cost */}
      <div className="glass-card rounded-lg p-4">
        <div className="mb-3 flex items-center justify-between">
          <span className="font-mono text-[10px] tracking-wider text-muted-foreground">
            TODAY&apos;S COST
          </span>
          <DollarSign className="h-4 w-4 text-neon-cyan" />
        </div>
        <p className="font-mono text-2xl font-bold text-foreground">
          ${formatUsdAdaptive(cost?.today, { minDecimals: 0, maxDecimals: 5, fallback: "-.--" })}
        </p>
        <div className="mt-2 flex items-center gap-1">
          <div className="flex gap-0.5">
            {SPARK_HEIGHTS.map((height, i) => (
              <div
                key={`spark-${i}`}
                className="w-1 rounded-full bg-neon-cyan/40"
                style={{ height: `${height}px` }}
              />
            ))}
          </div>
        </div>
      </div>

      {/* Monthly cost + budget gauge */}
      <div className="glass-card rounded-lg p-4">
        <div className="mb-3 flex items-center justify-between">
          <span className="font-mono text-[10px] tracking-wider text-muted-foreground">
            MONTHLY COST
          </span>
          <TrendingUp className="h-4 w-4 text-neon-magenta" />
        </div>
        <p className="font-mono text-2xl font-bold text-foreground">
          ${formatUsdAdaptive(cost?.month, { minDecimals: 0, maxDecimals: 5, fallback: "-.--" })}
        </p>
        <div className="mt-2">
          <div className="h-2 w-full overflow-hidden rounded-full bg-secondary">
            <div
              className="h-full rounded-full transition-all duration-700"
              style={{
                width: `${budgetPct}%`,
                background: `linear-gradient(90deg, #00f0ff, ${budgetPct > 80 ? "#ff00aa" : "#00f0ff"})`,
                boxShadow:
                  budgetPct > 80
                    ? "0 0 8px rgba(255,0,170,0.5)"
                    : "0 0 8px rgba(0,240,255,0.3)",
              }}
            />
          </div>
          <p className="mt-1 font-mono text-[10px] text-muted-foreground">
            ${formatUsdAdaptive(cost?.budget_remaining, { minDecimals: 0, maxDecimals: 5, fallback: "--" })} remaining
            {cost?.monthly_budget_total != null ? ` of $${formatUsdAdaptive(cost.monthly_budget_total, { minDecimals: 0, maxDecimals: 5, fallback: "--" })} allocated` : ""}
          </p>
        </div>
      </div>

      {/* Requests today */}
      <div className="glass-card rounded-lg p-4">
        <div className="mb-3 flex items-center justify-between">
          <span className="font-mono text-[10px] tracking-wider text-muted-foreground">
            REQUESTS TODAY
          </span>
          <Zap className="h-4 w-4 text-neon-green" />
        </div>
        <p className="font-mono text-2xl font-bold text-foreground">
          {cost ? cost.requests_today ?? "--" : "--"}
        </p>
        <div className="mt-2 flex flex-wrap gap-1">
          {cost &&
            Object.entries(cost.per_provider).map(([provider, amount]) => (
              <span
                key={provider}
                className="rounded-full border border-border px-2 py-0.5 font-mono text-[10px] text-muted-foreground"
              >
                {provider}: {amount as number} req
              </span>
            ))}
        </div>
      </div>
    </div>
  )
}
