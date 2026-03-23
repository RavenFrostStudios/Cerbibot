"use client"

let routeActiveSessionId: string | null = null
let routeSidebarOpen = true

export function getRouteActiveSessionId(): string | null {
  return routeActiveSessionId
}

export function setRouteActiveSessionId(sessionId: string | null) {
  routeActiveSessionId = sessionId
}

export function getRouteSidebarOpen(): boolean {
  return routeSidebarOpen
}

export function setRouteSidebarOpen(sidebarOpen: boolean) {
  routeSidebarOpen = sidebarOpen
}
