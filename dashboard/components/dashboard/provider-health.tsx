"use client"

import { useEffect, useState } from "react"
import type { CostData, HealthData, ProviderHealth } from "@/lib/types"
import { cn } from "@/lib/utils"
import { getSettings } from "@/lib/settings"

interface ProviderHealthPanelProps {
  health: HealthData | null
  cost: CostData | null
}

const statusConfig = {
  healthy: {
    color: "bg-neon-green",
    glow: "shadow-[0_0_6px_rgba(0,255,136,0.5)]",
    label: "ONLINE",
    labelColor: "text-neon-green",
  },
  degraded: {
    color: "bg-neon-yellow",
    glow: "shadow-[0_0_6px_rgba(255,170,0,0.5)]",
    label: "DEGRADED",
    labelColor: "text-neon-yellow",
  },
  down: {
    color: "bg-neon-red",
    glow: "shadow-[0_0_6px_rgba(255,51,51,0.5)]",
    label: "OFFLINE",
    labelColor: "text-neon-red",
  },
}

function ProviderCard({ provider }: { provider: ProviderHealth }) {
  const config = statusConfig[provider.status]

  return (
    <div className="glass-card rounded-lg p-4">
      <div className="mb-2 flex items-center justify-between">
        <span className="font-mono text-sm font-medium text-foreground">
          {provider.name}
        </span>
        <div className="flex items-center gap-2">
          <div
            className={cn("h-2 w-2 rounded-full", config.color, config.glow)}
          />
          <span className={cn("font-mono text-[10px]", config.labelColor)}>
            {config.label}
          </span>
        </div>
      </div>
      <div className="flex items-center gap-4">
        <div>
          <span className="font-mono text-[10px] text-muted-foreground">
            LATENCY
          </span>
          <p className="font-mono text-sm text-foreground">
            {provider.latency_ms}ms
          </p>
        </div>
        {provider.error_rate != null && (
          <div>
            <span className="font-mono text-[10px] text-muted-foreground">
              ERR RATE
            </span>
            <p className="font-mono text-sm text-foreground">
              {(provider.error_rate * 100).toFixed(1)}%
            </p>
          </div>
        )}
      </div>
    </div>
  )
}

export function ProviderHealthPanel({ health, cost }: ProviderHealthPanelProps) {
  const [configuredProviderNames, setConfiguredProviderNames] = useState<string[]>([])

  useEffect(() => {
    const update = () => {
      const configured = getSettings().providers
        .filter((provider) => provider.enabled)
        .map((provider) => provider.name)
      setConfiguredProviderNames(configured)
    }
    update()
    window.addEventListener("mmy-settings-changed", update)
    return () => window.removeEventListener("mmy-settings-changed", update)
  }, [])

  const baseProviders = health?.providers ?? []
  const rates = cost?.rate_limits ?? {}
  const fallbackNames = configuredProviderNames.length > 0
    ? configuredProviderNames
    : ["openai", "anthropic", "google", "xai", "local"]
  const providers: ProviderHealth[] =
    baseProviders.length > 0
      ? baseProviders.map((p) => {
          const rate = rates[p.name]
          const degraded =
            !!rate && (rate.rpm_headroom < 0.2 || rate.tpm_headroom < 0.2)
          return {
            ...p,
            status: degraded ? "degraded" : p.status,
            error_rate: rate ? Math.max(0, 1 - Math.min(rate.rpm_headroom, rate.tpm_headroom)) : p.error_rate,
          }
        })
      : fallbackNames.map((name) => ({ name, status: "healthy", latency_ms: 0, error_rate: 0 }))

  return (
    <div>
      <h3 className="mb-3 font-mono text-xs tracking-wider text-muted-foreground">
        PROVIDER HEALTH
      </h3>
      <div className="grid gap-3 sm:grid-cols-2">
        {providers.map((p) => (
          <ProviderCard key={p.name} provider={p} />
        ))}
      </div>
    </div>
  )
}
