import { clsx, type ClassValue } from 'clsx'
import { twMerge } from 'tailwind-merge'

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs))
}

export type StatusTone = "success" | "error" | "progress" | "neutral"

export function classifyStatusTone(message: string | null | undefined): StatusTone {
  const value = String(message ?? "").trim().toLowerCase()
  if (!value) return "neutral"

  if (
    /(could not|failed|fail\b|error|invalid|unavailable|blocked|missing|denied|offline|timed out|timeout)/.test(value)
  ) {
    return "error"
  }
  if (/(running|loading|saving|checking|refreshing|deleting|resuming|generating|testing|copying)/.test(value)) {
    return "progress"
  }
  if (
    /(loaded|saved|complete\b|completed\b|ok\b|pass\b|copied|exported|online|generated|set\b|installed|imported|deleted|cleared|resumed)/.test(
      value
    )
  ) {
    return "success"
  }
  return "neutral"
}

export function statusMessageClass(message: string | null | undefined, baseClassName = ""): string {
  const tone = classifyStatusTone(message)
  return cn(
    baseClassName,
    tone === "error" && "text-neon-red",
    tone === "progress" && "text-neon-yellow",
    tone === "success" && "text-neon-green",
    tone === "neutral" && "text-muted-foreground"
  )
}

export function formatUsdAdaptive(
  value: number | null | undefined,
  options?: { minDecimals?: number; maxDecimals?: number; fallback?: string }
): string {
  const minDecimals = Math.max(0, Math.min(5, options?.minDecimals ?? 0))
  const maxDecimals = Math.max(minDecimals, Math.min(5, options?.maxDecimals ?? 5))
  const fallback = options?.fallback ?? "--"
  if (value == null || !Number.isFinite(value)) return fallback

  let text = Number(value).toFixed(maxDecimals).replace(/\.?0+$/, "")
  if (text === "-0") text = "0"

  if (minDecimals > 0) {
    const parts = text.split(".")
    const whole = parts[0]
    const frac = parts[1] ?? ""
    if (frac.length < minDecimals) {
      text = `${whole}.${frac.padEnd(minDecimals, "0")}`
    }
  }
  return text
}
