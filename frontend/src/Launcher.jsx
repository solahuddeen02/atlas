import { useNavigate } from "react-router-dom"
import { APPS, SECTIONS } from "./apps"
import { hasRole } from "./auth"

function AppCard({ app, onClick }) {
  return (
    <button
      onClick={onClick}
      className="flex flex-col items-center gap-2 p-6 bg-white rounded-xl shadow-sm hover:shadow-md hover:-translate-y-0.5 transition-all"
    >
      <span className="text-4xl">{app.icon}</span>
      <span className="text-sm font-medium text-gray-700">{app.label}</span>
    </button>
  )
}

function Launcher({ user, onLogout }) {
  const navigate = useNavigate()
  const visibleApps = APPS.filter(a => !a.minRole || hasRole(user?.role, a.minRole))

  return (
    <div className="min-h-screen bg-gray-50 flex flex-col">
      {/* Top bar */}
      <div className="bg-white border-b px-6 py-3 flex items-center justify-between">
        <span className="text-lg font-bold text-gray-800">Atlas</span>
        <div className="flex items-center gap-4">
          <span className="text-sm text-gray-500">{user?.username}</span>
          <button
            onClick={onLogout}
            className="text-sm text-gray-400 hover:text-red-500"
          >
            Logout
          </button>
        </div>
      </div>

      {/* App grid */}
      <div className="flex-1 p-8 flex flex-col gap-8 max-w-2xl mx-auto w-full">
        {SECTIONS.map(section => {
          const apps = visibleApps.filter(a => a.section === section.id)
          if (apps.length === 0) return null
          return (
            <div key={section.id} className="flex flex-col gap-3">
              <p className="text-xs text-gray-400 uppercase tracking-widest">{section.label}</p>
              <div className="grid grid-cols-3 gap-4 sm:grid-cols-4">
                {apps.map(app => (
                  <AppCard key={app.id} app={app} onClick={() => navigate(app.path)} />
                ))}
              </div>
            </div>
          )
        })}
      </div>
    </div>
  )
}

export default Launcher
