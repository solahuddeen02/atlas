import { useState, useEffect } from "react"
import { authFetch } from "./api"
import { API_URL } from "./config"
import { useToast } from "./Toast"

function SystemView() {
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
    <div className="flex flex-col gap-6">
      <h2 className="text-lg font-semibold text-gray-700">System</h2>

      {/* Plugins */}
      <div className="flex flex-col gap-3">
        <h3 className="text-sm font-medium text-gray-500 uppercase tracking-wide">Plugins</h3>

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

        {plugins.length === 0
          ? <p className="text-sm text-gray-400">No plugins installed.</p>
          : (
            <div className="flex flex-col gap-2">
              {plugins.map(p => (
                <div key={p.name} className="bg-white rounded shadow px-4 py-3 flex items-center justify-between">
                  <div className="flex flex-col">
                    <span className="font-medium text-gray-800">{p.name}</span>
                    <span className="text-xs text-gray-400">
                      v{p.version} · installed {new Date(p.installed_at).toLocaleDateString()}
                    </span>
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
    </div>
  )
}

export default SystemView
