import { useState, useEffect } from "react"
import { PLUGIN_REGISTRY, PLATFORM_APPS, SECTIONS } from "./apps"
import { hasRole } from "../shared/auth"
import { authFetch } from "../shared/api"
import { API_URL, subdomainUrl } from "../shared/config"

function AppCard({ label, icon, href }) {
  return (
    <a
      href={href}
      className="flex flex-col items-center gap-2 p-6 bg-white rounded-xl shadow-sm hover:shadow-md hover:-translate-y-0.5 transition-all no-underline"
    >
      <span className="text-4xl">{icon}</span>
      <span className="text-sm font-medium text-gray-700">{label}</span>
    </a>
  )
}

function Portal({ user, onLogout }) {
  const [installedPlugins, setInstalledPlugins] = useState([])

  useEffect(() => {
    authFetch(`${API_URL}/system/plugins`)
      .then(r => r.json())
      .then(data => setInstalledPlugins(data.map(p => p.name)))
      .catch(() => setInstalledPlugins([]))
  }, [])

  const pluginApps = installedPlugins
    .filter(name => PLUGIN_REGISTRY[name])
    .map(name => ({ id: name, ...PLUGIN_REGISTRY[name] }))

  const platformApps = PLATFORM_APPS.filter(a => !a.minRole || hasRole(user.role, a.minRole))

  const allApps = [...pluginApps, ...platformApps]

  return (
    <div className="min-h-screen bg-gray-50 flex flex-col">
      <div className="bg-white border-b px-6 py-3 flex items-center justify-between">
        <span className="text-lg font-bold text-gray-800">Atlas</span>
        <div className="flex items-center gap-4">
          <span className="text-sm text-gray-500">{user.username}</span>
          <button onClick={onLogout} className="text-sm text-gray-400 hover:text-red-500">
            Logout
          </button>
        </div>
      </div>

      <div className="flex-1 p-8 flex flex-col gap-8 max-w-2xl mx-auto w-full">
        {SECTIONS.map(section => {
          const apps = allApps.filter(a => a.section === section.id)
          if (apps.length === 0) return null
          return (
            <div key={section.id} className="flex flex-col gap-3">
              <p className="text-xs text-gray-400 uppercase tracking-widest">{section.label}</p>
              <div className="grid grid-cols-3 gap-4 sm:grid-cols-4">
                {apps.map(app => (
                  <AppCard
                    key={app.id}
                    label={app.label}
                    icon={app.icon}
                    href={subdomainUrl(app.id)}
                  />
                ))}
              </div>
            </div>
          )
        })}
        {allApps.length === 0 && (
          <p className="text-sm text-gray-400 text-center py-12">No apps installed yet.</p>
        )}
      </div>
    </div>
  )
}

export default Portal
