import { useState, useEffect } from "react"
import { authFetch } from "./api"
import { API_URL } from "./config"
import { useToast } from "./Toast"

const RUNTIMES = [
  { value: "inprocess", label: "In-process", description: "Same process, no overhead" },
  { value: "subprocess", label: "Subprocess", description: "Separate process, crash-isolated (planned)" },
  { value: "container", label: "Container", description: "Docker isolated (planned)" },
]

const RUNTIME_BADGE = {
  inprocess:  "bg-green-100 text-green-700",
  subprocess: "bg-blue-100 text-blue-700",
  container:  "bg-purple-100 text-purple-700",
}

function RuntimeBadge({ runtime }) {
  return (
    <span className={`text-xs px-2 py-0.5 rounded-full font-medium ${RUNTIME_BADGE[runtime] ?? "bg-gray-100 text-gray-500"}`}>
      {runtime}
    </span>
  )
}

function PluginRow({ plugin, onUninstall }) {
  const [confirming, setConfirming] = useState(false)

  return (
    <div className="bg-white rounded-lg border px-4 py-3 flex items-center justify-between gap-4">
      <div className="flex flex-col gap-0.5 min-w-0">
        <div className="flex items-center gap-2">
          <span className="font-medium text-gray-800">{plugin.name}</span>
          <RuntimeBadge runtime={plugin.runtime ?? "inprocess"} />
          {plugin.package && (
            <span className="text-xs text-gray-400">pip</span>
          )}
        </div>
        <span className="text-xs text-gray-400">
          v{plugin.version} · installed {new Date(plugin.installed_at).toLocaleDateString()}
        </span>
      </div>

      <div className="flex items-center gap-2 shrink-0">
        {confirming ? (
          <>
            <span className="text-xs text-gray-500">Uninstall?</span>
            <button
              className="text-xs text-red-500 hover:text-red-700 font-medium"
              onClick={() => { onUninstall(plugin.name); setConfirming(false) }}
            >
              Yes
            </button>
            <button
              className="text-xs text-gray-400 hover:text-gray-600"
              onClick={() => setConfirming(false)}
            >
              No
            </button>
          </>
        ) : (
          <button
            className="text-xs text-gray-400 hover:text-red-500"
            onClick={() => setConfirming(true)}
          >
            Uninstall
          </button>
        )}
      </div>
    </div>
  )
}

function SystemView() {
  const [plugins, setPlugins] = useState([])
  const [installName, setInstallName] = useState("")
  const [runtime, setRuntime] = useState("inprocess")
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
      body: JSON.stringify({ name: installName.trim(), runtime }),
    })
      .then(res => res.json())
      .then(data => {
        if (data.status === "installed") {
          setPlugins(prev => [...prev, {
            name: data.name,
            version: data.version,
            runtime: data.runtime,
            installed_at: new Date().toISOString(),
          }])
          showToast(`${data.name} installed`)
        } else {
          showToast(`${data.name}: ${data.status}`)
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
        showToast(`${data.name} uninstalled (restart to fully remove routes)`)
        setPlugins(prev => prev.filter(p => p.name !== name))
      })
      .catch(err => showToast(err.message))
  }

  return (
    <div className="flex flex-col gap-6 max-w-2xl">
      <h2 className="text-lg font-semibold text-gray-700">System</h2>

      {/* Install */}
      <div className="flex flex-col gap-3">
        <h3 className="text-xs font-semibold text-gray-400 uppercase tracking-widest">Install plugin</h3>

        <div className="flex gap-2">
          <input
            className="border rounded-lg px-3 py-2 text-sm flex-1 focus:outline-none focus:ring-2 focus:ring-slate-300"
            placeholder="Plugin name (e.g. drive, pos, inventory)"
            value={installName}
            onChange={e => setInstallName(e.target.value)}
            onKeyDown={e => e.key === "Enter" && installPlugin()}
          />
          <select
            className="border rounded-lg px-3 py-2 text-sm bg-white focus:outline-none focus:ring-2 focus:ring-slate-300"
            value={runtime}
            onChange={e => setRuntime(e.target.value)}
            title={RUNTIMES.find(r => r.value === runtime)?.description}
          >
            {RUNTIMES.map(r => (
              <option key={r.value} value={r.value}>{r.label}</option>
            ))}
          </select>
          <button
            onClick={installPlugin}
            disabled={installing || !installName.trim()}
            className="text-sm bg-slate-900 text-white px-4 py-2 rounded-lg hover:bg-slate-700 disabled:opacity-40 whitespace-nowrap"
          >
            {installing ? "Installing…" : "Install"}
          </button>
        </div>

        <p className="text-xs text-gray-400">
          Local plugins load from <code className="bg-gray-100 px-1 rounded">plugins/</code>.
          External plugins are fetched from pip as <code className="bg-gray-100 px-1 rounded">atlas_plugin_&#123;name&#125;</code>.
        </p>
      </div>

      {/* Installed list */}
      <div className="flex flex-col gap-3">
        <h3 className="text-xs font-semibold text-gray-400 uppercase tracking-widest">
          Installed ({plugins.length})
        </h3>

        {plugins.length === 0
          ? <p className="text-sm text-gray-400">No plugins installed.</p>
          : (
            <div className="flex flex-col gap-2">
              {plugins.map(p => (
                <PluginRow key={p.name} plugin={p} onUninstall={uninstallPlugin} />
              ))}
            </div>
          )
        }
      </div>
    </div>
  )
}

export default SystemView
