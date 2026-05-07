import { useState, useEffect } from "react"
import { authFetch } from "./api"
import { API_URL } from "./config"
import { useToast } from "./Toast"

function StatCard({ label, value }) {
  return (
    <div className="bg-white rounded shadow px-6 py-5 flex flex-col gap-1">
      <span className="text-sm text-gray-400">{label}</span>
      <span className="text-2xl font-semibold text-gray-800">{value}</span>
    </div>
  )
}

function formatBytes(bytes) {
  if (!bytes) return "0 B"
  if (bytes < 1024) return bytes + " B"
  if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + " KB"
  if (bytes < 1024 * 1024 * 1024) return (bytes / (1024 * 1024)).toFixed(1) + " MB"
  return (bytes / (1024 * 1024 * 1024)).toFixed(2) + " GB"
}

function PluginsSection() {
  const [plugins, setPlugins] = useState([])
  const [installName, setInstallName] = useState("")
  const [installing, setInstalling] = useState(false)
  const showToast = useToast()

  useEffect(() => {
    authFetch(`${API_URL}/admin/plugins`)
      .then(res => res.json())
      .then(setPlugins)
      .catch(err => showToast(err.message))
  }, [])

  function installPlugin() {
    if (!installName.trim()) return
    setInstalling(true)
    authFetch(`${API_URL}/admin/plugins/install`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: installName.trim() }),
    })
      .then(res => res.json())
      .then(data => {
        showToast(`${data.name} ${data.status}`)
        if (data.status === "installed") {
          setPlugins(prev => [...prev, { name: data.name, version: data.version, installed_at: new Date().toISOString() }])
        }
        setInstallName("")
      })
      .catch(err => showToast(err.message))
      .finally(() => setInstalling(false))
  }

  function uninstallPlugin(name) {
    authFetch(`${API_URL}/admin/plugins/${name}`, { method: "DELETE" })
      .then(res => res.json())
      .then(data => {
        showToast(`${data.name} uninstalled`)
        setPlugins(prev => prev.filter(p => p.name !== name))
      })
      .catch(err => showToast(err.message))
  }

  return (
    <div className="flex flex-col gap-3">
      <h2 className="text-lg font-semibold text-gray-700">Plugins</h2>

      {/* Install */}
      <div className="flex gap-2">
        <input
          className="border rounded px-3 py-2 text-sm flex-1"
          placeholder="Plugin name (e.g. pos, inventory)"
          value={installName}
          onChange={e => setInstallName(e.target.value)}
          onKeyDown={e => e.key === "Enter" && installPlugin()}
        />
        <button
          onClick={installPlugin}
          disabled={installing || !installName.trim()}
          className="text-sm bg-slate-900 text-white px-4 py-2 rounded hover:bg-slate-700 disabled:opacity-50 whitespace-nowrap"
        >
          {installing ? "Installing..." : "Install"}
        </button>
      </div>

      {/* Installed list */}
      {plugins.length === 0
        ? <p className="text-sm text-gray-400">No plugins installed.</p>
        : (
          <div className="flex flex-col gap-2">
            {plugins.map(p => (
              <div key={p.name} className="bg-white rounded shadow px-4 py-3 flex items-center justify-between">
                <div className="flex flex-col">
                  <span className="font-medium text-gray-800">{p.name}</span>
                  <span className="text-xs text-gray-400">v{p.version} · installed {new Date(p.installed_at).toLocaleDateString()}</span>
                </div>
                <button
                  className="text-xs text-red-400 hover:text-red-600"
                  onClick={() => uninstallPlugin(p.name)}
                >
                  Uninstall
                </button>
              </div>
            ))}
          </div>
        )
      }
    </div>
  )
}

function ManagementView() {
  const [stats, setStats] = useState(null)
  const showToast = useToast()

  useEffect(() => {
    authFetch(`${API_URL}/admin/stats`)
      .then(res => res.json())
      .then(setStats)
      .catch(err => showToast(err.message))
  }, [])

  function inviteMember() {
    authFetch(`${API_URL}/tenant/invite`, { method: "POST" })
      .then(res => res.json())
      .then(data => {
        const url = `${window.location.origin}${data.url}`
        navigator.clipboard.writeText(url)
          .then(() => showToast("Invite link copied to clipboard"))
          .catch(() => showToast(`Invite link: ${url}`))
      })
      .catch(err => showToast(err.message))
  }

  return (
    <div className="flex flex-col gap-8">

      {/* Overview */}
      <div className="flex flex-col gap-4">
        <div className="flex items-center justify-between">
          <h2 className="text-lg font-semibold text-gray-700">Overview</h2>
          <button
            onClick={inviteMember}
            className="text-sm bg-blue-500 text-white px-3 py-1.5 rounded hover:bg-blue-600"
          >
            Invite member
          </button>
        </div>
        {stats
          ? (
            <div className="grid grid-cols-2 gap-4 sm:grid-cols-3">
              <StatCard label="Members" value={stats.members} />
              <StatCard label="Storage used" value={formatBytes(stats.storage_used)} />
              <StatCard label="Files" value={stats.files} />
              <StatCard label="Folders" value={stats.folders} />
              <StatCard label="Photos" value={stats.photos} />
              <StatCard label="Trashed" value={stats.trashed} />
            </div>
          )
          : <p className="text-gray-400 text-sm">Loading...</p>
        }
      </div>

      {/* Plugins */}
      <PluginsSection />

    </div>
  )
}

export default ManagementView
