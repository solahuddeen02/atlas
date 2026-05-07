import { useState, useEffect } from "react"
import { PLUGIN_REGISTRY, PLATFORM_APPS } from "./apps"
import { hasRole } from "../shared/auth"
import { authFetch } from "../shared/api"
import { API_URL, getSubdomain, subdomainUrl } from "../shared/config"
import AppShell from "./AppShell"
import Portal from "./Portal"
import LoginPage from "../pages/LoginPage"
import JoinView from "../pages/JoinView"

function App() {
  const [user, setUser]       = useState(null)
  const [loading, setLoading] = useState(true)

  // Verify auth on mount by calling /auth/me (cookie sent automatically)
  useEffect(() => {
    authFetch(`${API_URL}/auth/me`)
      .then(r => r.json())
      .then(setUser)
      .catch(() => setUser(null))
      .finally(() => setLoading(false))
  }, [])

  function handleLogout() {
    authFetch(`${API_URL}/auth/logout`, { method: "POST" })
      .catch(() => {})
      .finally(() => setUser(null))
  }

  if (loading) {
    return (
      <div className="min-h-screen flex items-center justify-center bg-gray-50">
        <span className="text-gray-400 text-sm">Loading…</span>
      </div>
    )
  }

  // Join link — works from any subdomain
  const joinToken = new URLSearchParams(window.location.search).get("token")
  if (joinToken && !user) {
    return <JoinView token={joinToken} onJoin={u => { setUser(u); window.history.replaceState({}, "", "/") }} />
  }

  if (!user) {
    return <LoginPage onLogin={setUser} />
  }

  const subdomain = getSubdomain()

  // Root domain → portal showing all available apps
  if (!subdomain) {
    return <Portal user={user} onLogout={handleLogout} />
  }

  // Find the app for this subdomain
  const pluginDef  = PLUGIN_REGISTRY[subdomain]
  const platformDef = PLATFORM_APPS.find(a => a.id === subdomain)
  const def = pluginDef ? { id: subdomain, ...pluginDef } : platformDef

  if (!def) {
    return (
      <div className="min-h-screen flex items-center justify-center bg-gray-50">
        <p className="text-gray-500">App <strong>{subdomain}</strong> not found.</p>
      </div>
    )
  }

  if (def.minRole && !hasRole(user.role, def.minRole)) {
    return (
      <div className="min-h-screen flex items-center justify-center bg-gray-50">
        <p className="text-gray-500">Access denied — requires <strong>{def.minRole}</strong> role.</p>
      </div>
    )
  }

  return (
    <AppShell label={def.label} icon={def.icon} onLogout={handleLogout} fullHeight={def.fullHeight}
      portalHref={`${window.location.protocol}//${import.meta.env.VITE_BASE_DOMAIN ?? "atlas.local"}`}
    >
      <def.component />
    </AppShell>
  )
}

export default App
