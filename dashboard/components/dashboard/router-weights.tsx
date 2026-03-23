"use client"

import type { CostData } from "@/lib/types"

interface RouterWeightsProps {
  cost: CostData | null
}

const palette = ["#00f0ff", "#ff00aa", "#00ff88", "#ffaa00", "#aa66ff", "#ff6677", "#66ccff"]

export function RouterWeights({ cost }: RouterWeightsProps) {
  const snapshot = cost?.router_weights ?? {}
  const domainMap = new Map<string, Array<{ provider: string; score: number }>>()
  for (const [provider, perDomain] of Object.entries(snapshot)) {
    for (const [domain, stats] of Object.entries(perDomain ?? {})) {
      const list = domainMap.get(domain) ?? []
      list.push({ provider, score: Number(stats?.score ?? 0) })
      domainMap.set(domain, list)
    }
  }
  const domains =
    domainMap.size > 0
      ? Array.from(domainMap.entries()).map(([name, rows]) => {
          const total = rows.reduce((sum, r) => sum + Math.max(r.score, 0), 0)
          const normalized =
            total > 0
              ? rows.map((r, i) => ({
                  provider: r.provider,
                  weight: Math.max(r.score, 0) / total,
                  color: palette[i % palette.length],
                }))
              : rows.map((r, i) => ({
                  provider: r.provider,
                  weight: 1 / Math.max(rows.length, 1),
                  color: palette[i % palette.length],
                }))
          return { name, weights: normalized.sort((a, b) => b.weight - a.weight) }
        })
      : []

  return (
    <div>
      <h3 className="mb-3 font-mono text-xs tracking-wider text-muted-foreground">
        ROUTER WEIGHTS
      </h3>
      <div className="glass-card space-y-4 rounded-lg p-4">
        {domains.length === 0 && (
          <div className="font-mono text-xs text-muted-foreground">No router weight data yet.</div>
        )}
        {domains.map((domain) => (
          <div key={domain.name}>
            <div className="mb-1 flex items-center justify-between">
              <span className="font-mono text-xs text-foreground">
                {domain.name}
              </span>
            </div>
            <div className="flex h-4 overflow-hidden rounded-full">
              {domain.weights.map((w) => (
                <div
                  key={w.provider}
                  className="relative transition-all duration-500 hover:brightness-125"
                  style={{
                    width: `${w.weight * 100}%`,
                    backgroundColor: w.color,
                    opacity: 0.7,
                  }}
                  title={`${w.provider}: ${(w.weight * 100).toFixed(0)}%`}
                />
              ))}
            </div>
            <div className="mt-1 flex flex-wrap gap-2">
              {domain.weights.map((w) => (
                <span
                  key={w.provider}
                  className="flex items-center gap-1 font-mono text-[10px] text-muted-foreground"
                >
                  <span
                    className="inline-block h-1.5 w-1.5 rounded-full"
                    style={{ backgroundColor: w.color }}
                  />
                  {w.provider} {(w.weight * 100).toFixed(0)}%
                </span>
              ))}
            </div>
          </div>
        ))}
      </div>
    </div>
  )
}
