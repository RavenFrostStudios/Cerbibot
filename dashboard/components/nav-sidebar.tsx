"use client"

import Link from "next/link"
import { usePathname } from "next/navigation"
import {
  MessageSquare,
  LayoutDashboard,
  Brain,
  History,
  Activity,
  Puzzle,
  Settings,
  Menu,
  X,
} from "lucide-react"
import { useEffect, useState } from "react"
import { cn } from "@/lib/utils"
import { fetchDelegationHealth } from "@/lib/api"
import { getSettings } from "@/lib/settings"

const navItems = [
  { href: "/", label: "Chat", icon: MessageSquare },
  { href: "/dashboard", label: "Dashboard", icon: LayoutDashboard },
  { href: "/memory", label: "Memory", icon: Brain },
  { href: "/artifacts", label: "Artifacts", icon: History },
  { href: "/runs", label: "Runs", icon: Activity },
  { href: "/skills", label: "Skills", icon: Puzzle },
  { href: "/settings", label: "Settings", icon: Settings },
]

export function NavSidebar() {
  const pathname = usePathname()
  const [open, setOpen] = useState(false)
  const [projectId, setProjectId] = useState<string>(() => getSettings().activeProjectId || "default")
  const [delegationHealth, setDelegationHealth] = useState<{
    state: "checking" | "online" | "offline"
    message: string
  }>({
    state: "checking",
    message: "Delegation checking...",
  })

  useEffect(() => {
    let cancelled = false
    let timer: number | null = null
    const poll = async () => {
      try {
        const status = await fetchDelegationHealth()
        if (cancelled) return
        if (status.reachable) {
          setDelegationHealth({ state: "online", message: "Delegation online" })
        } else {
          setDelegationHealth({ state: "offline", message: `Delegation ${status.status}` })
        }
      } catch {
        if (cancelled) return
        setDelegationHealth({ state: "offline", message: "Delegation offline" })
      }
    }
    void poll()
    timer = window.setInterval(() => {
      if (document.hidden) return
      void poll()
    }, 60000)
    return () => {
      cancelled = true
      if (timer !== null) window.clearInterval(timer)
    }
  }, [])

  useEffect(() => {
    const update = () => {
      const next = (getSettings().activeProjectId || "default").trim() || "default"
      setProjectId(next)
    }
    update()
    window.addEventListener("mmy-settings-changed", update)
    return () => window.removeEventListener("mmy-settings-changed", update)
  }, [])

  return (
    <>
      {/* Mobile hamburger */}
      <button
        type="button"
        onClick={() => setOpen(true)}
        className="fixed top-4 left-4 z-50 flex items-center justify-center rounded-lg border border-border bg-card p-2 text-foreground lg:hidden"
        aria-label="Open navigation"
      >
        <Menu className="h-5 w-5" />
      </button>

      {/* Backdrop */}
      {open && (
        <div
          className="fixed inset-0 z-40 bg-background/80 backdrop-blur-sm lg:hidden"
          onClick={() => setOpen(false)}
          onKeyDown={() => {}}
          role="presentation"
        />
      )}

      {/* Sidebar */}
      <aside
        className={cn(
          "fixed top-0 left-0 z-50 flex h-full w-56 flex-col border-r border-border bg-sidebar transition-transform duration-300 lg:relative lg:translate-x-0",
          open ? "translate-x-0" : "-translate-x-full"
        )}
      >
        {/* Logo */}
        <div className="flex items-center justify-between border-b border-border px-4 py-4">
          <div className="flex items-center gap-2">
            <div className="flex h-8 w-8 items-center justify-center rounded-md neon-border glow-cyan">
              <span className="font-mono text-sm font-bold text-neon-cyan">
                M
              </span>
            </div>
            <div>
              <h1 className="font-mono text-sm font-bold tracking-wider text-foreground">
                CerbiBot
              </h1>
              <p className="text-[10px] tracking-widest text-muted-foreground">
                POWERED BY MMY ORCHESTRATOR
              </p>
            </div>
          </div>
          <button
            type="button"
            onClick={() => setOpen(false)}
            className="text-muted-foreground lg:hidden"
            aria-label="Close navigation"
          >
            <X className="h-4 w-4" />
          </button>
        </div>

        {/* Nav items */}
        <nav className="flex flex-1 flex-col gap-1 p-3">
          {navItems.map((item) => {
            const isActive =
              item.href === "/"
                ? pathname === "/"
                : pathname.startsWith(item.href)
            return (
              <Link
                key={item.href}
                href={item.href}
                onClick={() => setOpen(false)}
                className={cn(
                  "flex items-center gap-3 rounded-md px-3 py-2.5 text-sm font-medium transition-all",
                  isActive
                    ? "neon-border glow-cyan bg-secondary text-neon-cyan"
                    : "text-muted-foreground hover:bg-secondary hover:text-foreground"
                )}
              >
                <item.icon className="h-4 w-4 shrink-0" />
                {item.label}
              </Link>
            )
          })}
        </nav>

        {/* Bottom status */}
        <div className="border-t border-border p-3">
          <div className="mb-2 flex items-center justify-between rounded border border-border/60 bg-background/30 px-2 py-1">
            <span className="font-mono text-[10px] tracking-wider text-muted-foreground">PROJECT</span>
            <span
              className="max-w-[120px] truncate rounded border border-neon-cyan/35 bg-neon-cyan/10 px-1.5 py-0.5 font-mono text-[10px] text-neon-cyan"
              title={projectId}
            >
              {projectId}
            </span>
          </div>
          <div className="flex items-center gap-2 text-xs text-muted-foreground">
            <div className="h-2 w-2 rounded-full bg-neon-green shadow-[0_0_6px_rgba(0,255,136,0.5)]" />
            <span className="font-mono">SYSTEM ONLINE</span>
          </div>
          <div className="mt-2 flex items-center gap-2 text-xs text-muted-foreground">
            <div
              className={cn(
                "h-2 w-2 rounded-full",
                delegationHealth.state === "online"
                  ? "bg-neon-green shadow-[0_0_6px_rgba(0,255,136,0.5)]"
                  : delegationHealth.state === "checking"
                    ? "bg-neon-yellow shadow-[0_0_6px_rgba(255,170,0,0.45)]"
                    : "bg-neon-red shadow-[0_0_6px_rgba(255,51,51,0.45)]"
              )}
            />
            <span className="font-mono">
              {delegationHealth.state === "online"
                ? "DELEGATION ONLINE"
                : delegationHealth.state === "checking"
                  ? "DELEGATION CHECKING"
                  : "DELEGATION OFFLINE"}
            </span>
          </div>
        </div>
      </aside>
    </>
  )
}
