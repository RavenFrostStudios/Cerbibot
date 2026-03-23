"use client"

import React from "react"

import { NavSidebar } from "./nav-sidebar"
import { useEffect, useState } from "react"
import { RefreshCw } from "lucide-react"
import { getSettings, mergeSettings, saveSettings, type AppSettings } from "@/lib/settings"
import { fetchProjects, fetchUiSettingsProfile } from "@/lib/api"

export function AppShell({ children }: { children: React.ReactNode }) {
  const [theme, setTheme] = useState<"cyberpunk" | "stealth" | "light">("cyberpunk")
  const [projectId, setProjectId] = useState<string>("default")
  const [projectOptions, setProjectOptions] = useState<string[]>(["default"])

  const loadProjects = async (currentProjectId?: string) => {
    const current = (currentProjectId ?? getSettings().activeProjectId ?? "default").trim() || "default"
    try {
      const ids = await fetchProjects()
      const merged = Array.from(new Set([...ids, current]))
      setProjectOptions(merged.sort((a, b) => a.localeCompare(b)))
    } catch {
      setProjectOptions((prev) => {
        const merged = Array.from(new Set([...prev, current, "default"]))
        return merged.sort((a, b) => a.localeCompare(b))
      })
    }
  }

  useEffect(() => {
    const update = () => {
      const settings = getSettings()
      setTheme(settings.theme)
      setProjectId((settings.activeProjectId || "default").trim() || "default")
    }

    update()
    void (async () => {
      const local = getSettings()
      try {
        const profile = await fetchUiSettingsProfile()
        const merged = mergeSettings(local, profile as Partial<AppSettings>)
        merged.bearerToken = local.bearerToken
        saveSettings(merged)
      } catch {
        // Keep local settings when daemon profile cannot be loaded.
      }
    })()
    void loadProjects()
    window.addEventListener("mmy-settings-changed", update)
    return () => window.removeEventListener("mmy-settings-changed", update)
  }, [])

  const handleProjectChange = (nextProjectId: string) => {
    const normalized = (nextProjectId || "default").trim() || "default"
    if (normalized === projectId) return
    setProjectId(normalized)
    saveSettings({ activeProjectId: normalized })
  }

  return (
    <div
      className={`flex h-screen overflow-hidden ${
        theme === "cyberpunk" ? "theme-cyberpunk cyber-grid" : theme === "stealth" ? "theme-stealth" : "theme-light"
      } bg-background text-foreground`}
    >
      <NavSidebar />
      <main
        className={`relative flex-1 overflow-auto ${
          theme === "cyberpunk" ? "bg-background cyber-grid" : "bg-background"
        }`}
      >
        <div className="sticky top-0 z-20 flex items-center justify-end gap-2 border-b border-border bg-background/85 px-4 py-2 backdrop-blur-sm">
          <span className="font-mono text-[10px] text-muted-foreground">PROJECT</span>
          <select
            value={projectId}
            onChange={(e) => handleProjectChange(e.target.value)}
            className="rounded border border-border bg-background/60 px-2 py-1 font-mono text-[10px] text-foreground focus:outline-none"
            aria-label="Global project selector"
            title="Global project selector"
          >
            {projectOptions.map((id) => (
              <option key={id} value={id}>
                {id}
              </option>
            ))}
          </select>
          <button
            type="button"
            onClick={() => {
              void loadProjects(projectId)
            }}
            className="rounded border border-border px-1.5 py-1 text-muted-foreground hover:text-foreground"
            aria-label="Refresh project list"
            title="Refresh project list"
          >
            <RefreshCw className="h-3 w-3" />
          </button>
        </div>
        {children}
      </main>
    </div>
  )
}
