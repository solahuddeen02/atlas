import { useState, useEffect } from "react"
import { authFetch } from "../shared/api"
import { API_URL } from "../shared/config"
import { useToast } from "../shared/Toast"

// ─── helpers ─────────────────────────────────────────────────────────────────

function decodeJwt(token) {
  try { return JSON.parse(atob(token.split(".")[1])) } catch { return null }
}

function formatBytes(bytes) {
  if (!bytes) return "0 B"
  if (bytes < 1024) return bytes + " B"
  if (bytes < 1024 ** 2) return (bytes / 1024).toFixed(1) + " KB"
  if (bytes < 1024 ** 3) return (bytes / 1024 ** 2).toFixed(1) + " MB"
  return (bytes / 1024 ** 3).toFixed(2) + " GB"
}

const ROLE_OPTIONS   = ["member", "admin", "owner"]
const EXPIRY_OPTIONS = [
  { label: "No expiry", value: null },
  { label: "1 day",     value: 1    },
  { label: "7 days",    value: 7    },
  { label: "30 days",   value: 30   },
]
const RUNTIMES = [
  { value: "inprocess",  label: "In-process",  desc: "Same process, no overhead"              },
  { value: "subprocess", label: "Subprocess",  desc: "Separate process, crash-isolated (planned)" },
  { value: "container",  label: "Container",   desc: "Docker isolated (planned)"              },
]
const RUNTIME_BADGE = {
  inprocess:  "bg-green-100 text-green-700",
  subprocess: "bg-blue-100 text-blue-700",
  container:  "bg-purple-100 text-purple-700",
}

// ─── Overview ────────────────────────────────────────────────────────────────

function StatCard({ label, value, accent }) {
  return (
    <div className={`bg-white rounded-xl border px-5 py-4 flex flex-col gap-1 ${accent ? "border-l-4 " + accent : "border-gray-100"}`}>
      <span className="text-xs text-gray-400 uppercase tracking-wide">{label}</span>
      <span className="text-2xl font-semibold text-gray-800">{value ?? "—"}</span>
    </div>
  )
}

function StorageBar({ used, limit }) {
  const pct = limit ? Math.min((used / limit) * 100, 100) : 0
  return (
    <div className="flex flex-col gap-1.5">
      <div className="flex justify-between text-xs text-gray-400">
        <span>{formatBytes(used)} used</span>
        {limit ? <span>of {formatBytes(limit)}</span> : <span>No limit</span>}
      </div>
      <div className="h-2 bg-gray-100 rounded-full overflow-hidden">
        <div className="h-full bg-blue-400 rounded-full transition-all" style={{ width: `${pct}%` }} />
      </div>
    </div>
  )
}

function OverviewSection({ stats, onGoTo }) {
  if (!stats) return <p className="text-sm text-gray-400">Loading...</p>
  return (
    <div className="flex flex-col gap-6">
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-3">
        <StatCard label="Members"       value={stats.members}                   accent="border-indigo-400" />
        <StatCard label="Files"         value={stats.files}                     accent="border-blue-400"   />
        <StatCard label="Photos"        value={stats.photos}                    accent="border-emerald-400"/>
        <StatCard label="Folders"       value={stats.folders}                                              />
        <StatCard label="Trashed"       value={stats.trashed}                                              />
        <StatCard label="Storage"       value={formatBytes(stats.storage_used)}                            />
      </div>
      <StorageBar used={stats.storage_used} limit={stats.storage_limit} />
      <div className="flex gap-3 flex-wrap">
        <button onClick={() => onGoTo("members")}
          className="text-sm bg-slate-900 text-white px-4 py-2 rounded-lg hover:bg-slate-700">
          Manage members
        </button>
        <button onClick={() => onGoTo("invites")}
          className="text-sm border border-slate-200 text-slate-700 px-4 py-2 rounded-lg hover:bg-slate-50">
          Invite member
        </button>
      </div>
    </div>
  )
}

// ─── Members ─────────────────────────────────────────────────────────────────

function MembersSection({ currentUser }) {
  const [members, setMembers]       = useState([])
  const [confirming, setConfirming] = useState(null)
  const showToast = useToast()

  useEffect(() => {
    authFetch(`${API_URL}/admin/members`)
      .then(r => r.json()).then(setMembers)
      .catch(err => showToast(err.message))
  }, [])

  function updateRole(id, role) {
    authFetch(`${API_URL}/admin/members/${id}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ role }),
    })
      .then(r => r.json())
      .then(u => setMembers(prev => prev.map(m => m.id === id ? { ...m, role: u.role } : m)))
      .catch(err => showToast(err.message))
  }

  function removeMember(id) {
    authFetch(`${API_URL}/admin/members/${id}`, { method: "DELETE" })
      .then(() => { setMembers(prev => prev.filter(m => m.id !== id)); setConfirming(null) })
      .catch(err => showToast(err.message))
  }

  return (
    <div className="flex flex-col gap-3">
      {members.map(m => {
        const isSelf = m.id === currentUser?.sub
        return (
          <div key={m.id} className="bg-white rounded-xl border border-gray-100 px-4 py-3 flex items-center gap-3">
            <div className="w-9 h-9 rounded-full bg-slate-100 flex items-center justify-center text-sm font-semibold text-slate-600 shrink-0">
              {m.username[0].toUpperCase()}
            </div>
            <div className="flex-1 min-w-0">
              <p className="text-sm font-medium text-gray-800 truncate">
                {m.username}
                {isSelf && <span className="ml-1.5 text-xs text-gray-400 font-normal">(you)</span>}
              </p>
              <p className="text-xs text-gray-400">{m.role}</p>
            </div>
            <select
              className="text-xs border rounded-lg px-2 py-1.5 bg-white disabled:opacity-40"
              value={m.role}
              disabled={isSelf}
              onChange={e => updateRole(m.id, e.target.value)}
            >
              {ROLE_OPTIONS.map(r => <option key={r} value={r}>{r}</option>)}
            </select>
            {!isSelf && (
              confirming === m.id ? (
                <div className="flex items-center gap-1.5 text-xs">
                  <span className="text-gray-400">Remove?</span>
                  <button className="text-red-500 font-medium hover:text-red-700" onClick={() => removeMember(m.id)}>Yes</button>
                  <button className="text-gray-400 hover:text-gray-600" onClick={() => setConfirming(null)}>No</button>
                </div>
              ) : (
                <button className="text-xs text-gray-300 hover:text-red-400" onClick={() => setConfirming(m.id)}>Remove</button>
              )
            )}
          </div>
        )
      })}
      {members.length === 0 && <p className="text-sm text-gray-400">No members yet.</p>}
    </div>
  )
}

// ─── Invites ─────────────────────────────────────────────────────────────────

function InvitesSection() {
  const [invites, setInvites]       = useState([])
  const [role, setRole]             = useState("member")
  const [expiryDays, setExpiryDays] = useState(7)
  const [creating, setCreating]     = useState(false)
  const [confirming, setConfirming] = useState(null)
  const showToast = useToast()

  useEffect(() => {
    authFetch(`${API_URL}/tenant/invites`)
      .then(r => r.json()).then(setInvites)
      .catch(err => showToast(err.message))
  }, [])

  function createInvite() {
    setCreating(true)
    authFetch(`${API_URL}/tenant/invite`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ role, expires_in_days: expiryDays }),
    })
      .then(r => r.json())
      .then(inv => {
        setInvites(prev => [inv, ...prev])
        const url = `${window.location.origin}/?token=${inv.token}`
        navigator.clipboard.writeText(url)
          .then(() => showToast("Invite link copied"))
          .catch(() => showToast(`Link: ${url}`))
      })
      .catch(err => showToast(err.message))
      .finally(() => setCreating(false))
  }

  function revokeInvite(token) {
    authFetch(`${API_URL}/tenant/invites/${token}`, { method: "DELETE" })
      .then(() => { setInvites(prev => prev.filter(i => i.token !== token)); setConfirming(null) })
      .catch(err => showToast(err.message))
  }

  function fmtExpiry(exp) {
    if (!exp) return "No expiry"
    const d = new Date(exp)
    return d < new Date() ? "Expired" : d.toLocaleDateString()
  }

  return (
    <div className="flex flex-col gap-4">
      {/* Create row */}
      <div className="bg-white rounded-xl border border-gray-100 p-4 flex flex-col gap-3">
        <p className="text-xs font-semibold text-gray-500 uppercase tracking-widest">New invite link</p>
        <div className="flex gap-2 flex-wrap">
          <select className="border rounded-lg px-3 py-2 text-sm bg-white" value={role} onChange={e => setRole(e.target.value)}>
            {ROLE_OPTIONS.map(r => <option key={r} value={r}>{r}</option>)}
          </select>
          <select
            className="border rounded-lg px-3 py-2 text-sm bg-white"
            value={expiryDays ?? ""}
            onChange={e => setExpiryDays(e.target.value ? Number(e.target.value) : null)}
          >
            {EXPIRY_OPTIONS.map(o => <option key={o.label} value={o.value ?? ""}>{o.label}</option>)}
          </select>
          <button
            onClick={createInvite} disabled={creating}
            className="text-sm bg-slate-900 text-white px-4 py-2 rounded-lg hover:bg-slate-700 disabled:opacity-40"
          >
            {creating ? "Creating…" : "Create & copy link"}
          </button>
        </div>
      </div>

      {/* List */}
      {invites.length === 0
        ? <p className="text-sm text-gray-400">No pending invites.</p>
        : invites.map(inv => (
          <div key={inv.token} className="bg-white rounded-xl border border-gray-100 px-4 py-3 flex items-center justify-between gap-3">
            <div className="flex flex-col min-w-0">
              <span className="text-xs font-mono text-gray-600 truncate">{inv.token.slice(0, 24)}…</span>
              <span className="text-xs text-gray-400">{inv.role} · {fmtExpiry(inv.expires_at)}</span>
            </div>
            {confirming === inv.token ? (
              <div className="flex items-center gap-1.5 text-xs">
                <span className="text-gray-400">Revoke?</span>
                <button className="text-red-500 font-medium" onClick={() => revokeInvite(inv.token)}>Yes</button>
                <button className="text-gray-400" onClick={() => setConfirming(null)}>No</button>
              </div>
            ) : (
              <button className="text-xs text-gray-300 hover:text-red-400 shrink-0" onClick={() => setConfirming(inv.token)}>
                Revoke
              </button>
            )}
          </div>
        ))
      }
    </div>
  )
}

// ─── Plugins (System) ────────────────────────────────────────────────────────

function DepTag({ name }) {
  return <span className="text-xs px-1.5 py-0.5 rounded bg-gray-100 text-gray-500 font-mono">{name}</span>
}

function PluginRow({ plugin, onUninstall }) {
  const [confirming, setConfirming] = useState(false)
  const isBlocked      = plugin.used_by?.length > 0
  const hasMissingDeps = plugin.missing_deps?.length > 0

  return (
    <div className={`bg-white rounded-xl border px-4 py-3 flex flex-col gap-2 ${hasMissingDeps ? "border-amber-200" : "border-gray-100"}`}>
      <div className="flex items-center justify-between gap-3">
        <div className="flex flex-col gap-1 min-w-0">
          <div className="flex items-center gap-2 flex-wrap">
            <span className="font-medium text-gray-800">{plugin.name}</span>
            <span className={`text-xs px-2 py-0.5 rounded-full font-medium ${RUNTIME_BADGE[plugin.runtime] ?? "bg-gray-100 text-gray-500"}`}>
              {plugin.runtime ?? "inprocess"}
            </span>
            {plugin.package && <span className="text-xs text-gray-400">pip</span>}
          </div>
          <span className="text-xs text-gray-400">
            v{plugin.version} · {new Date(plugin.installed_at).toLocaleDateString()}
          </span>
        </div>
        <div className="shrink-0">
          {isBlocked ? (
            <span className="text-xs text-gray-300" title={`Required by: ${plugin.used_by.join(", ")}`}>In use</span>
          ) : confirming ? (
            <div className="flex items-center gap-1.5 text-xs">
              <span className="text-gray-400">Uninstall?</span>
              <button className="text-red-500 font-medium" onClick={() => { onUninstall(plugin.name); setConfirming(false) }}>Yes</button>
              <button className="text-gray-400" onClick={() => setConfirming(false)}>No</button>
            </div>
          ) : (
            <button className="text-xs text-gray-300 hover:text-red-400" onClick={() => setConfirming(true)}>Uninstall</button>
          )}
        </div>
      </div>
      {(plugin.depends_on?.length > 0 || plugin.used_by?.length > 0) && (
        <div className="flex flex-wrap gap-3 text-xs text-gray-400 border-t pt-2">
          {plugin.depends_on?.length > 0 && (
            <div className="flex items-center gap-1.5"><span>needs</span>{plugin.depends_on.map(d => <DepTag key={d} name={d} />)}</div>
          )}
          {plugin.used_by?.length > 0 && (
            <div className="flex items-center gap-1.5"><span>used by</span>{plugin.used_by.map(d => <DepTag key={d} name={d} />)}</div>
          )}
        </div>
      )}
      {hasMissingDeps && (
        <p className="text-xs text-amber-600 bg-amber-50 rounded px-2 py-1">
          Missing: {plugin.missing_deps.join(", ")}
        </p>
      )}
    </div>
  )
}

function PluginsSection() {
  const [plugins, setPlugins]         = useState([])
  const [installName, setInstallName] = useState("")
  const [runtime, setRuntime]         = useState("inprocess")
  const [installing, setInstalling]   = useState(false)
  const showToast = useToast()

  function load() {
    authFetch(`${API_URL}/system/plugins`)
      .then(r => r.json()).then(setPlugins)
      .catch(err => showToast(err.message))
  }
  useEffect(() => { load() }, [])

  function install() {
    if (!installName.trim()) return
    setInstalling(true)
    authFetch(`${API_URL}/system/plugins/install`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: installName.trim(), runtime }),
    })
      .then(r => r.json())
      .then(data => {
        showToast(data.status === "installed" ? `${data.name} installed` : `${data.name}: ${data.status}`)
        setInstallName("")
        load()
      })
      .catch(err => showToast(err.message))
      .finally(() => setInstalling(false))
  }

  function uninstall(name) {
    authFetch(`${API_URL}/system/plugins/${name}`, { method: "DELETE" })
      .then(r => r.json())
      .then(data => {
        if (data.status === "blocked") showToast(`Cannot uninstall: required by ${data.dependents.join(", ")}`)
        else { showToast(`${data.name} uninstalled`); load() }
      })
      .catch(err => showToast(err.message))
  }

  return (
    <div className="flex flex-col gap-4">
      {/* Install form */}
      <div className="bg-white rounded-xl border border-gray-100 p-4 flex flex-col gap-3">
        <p className="text-xs font-semibold text-gray-500 uppercase tracking-widest">Install plugin</p>
        <div className="flex gap-2">
          <input
            className="border rounded-lg px-3 py-2 text-sm flex-1 focus:outline-none focus:ring-2 focus:ring-slate-300"
            placeholder="Plugin name (e.g. drive, pos)"
            value={installName}
            onChange={e => setInstallName(e.target.value)}
            onKeyDown={e => e.key === "Enter" && install()}
          />
          <select
            className="border rounded-lg px-3 py-2 text-sm bg-white"
            value={runtime}
            onChange={e => setRuntime(e.target.value)}
            title={RUNTIMES.find(r => r.value === runtime)?.desc}
          >
            {RUNTIMES.map(r => <option key={r.value} value={r.value}>{r.label}</option>)}
          </select>
          <button
            onClick={install} disabled={installing || !installName.trim()}
            className="text-sm bg-slate-900 text-white px-4 py-2 rounded-lg hover:bg-slate-700 disabled:opacity-40 whitespace-nowrap"
          >
            {installing ? "Installing…" : "Install"}
          </button>
        </div>
        <p className="text-xs text-gray-400">Dependencies declared in plugin manifests are installed automatically.</p>
      </div>

      {/* Installed */}
      <div className="flex flex-col gap-2">
        <p className="text-xs font-semibold text-gray-400 uppercase tracking-widest">Installed ({plugins.length})</p>
        {plugins.length === 0
          ? <p className="text-sm text-gray-400">No plugins installed.</p>
          : plugins.map(p => <PluginRow key={p.name} plugin={p} onUninstall={uninstall} />)
        }
      </div>
    </div>
  )
}

// ─── Sidebar nav ─────────────────────────────────────────────────────────────

const NAV = [
  {
    section: "Workspace",
    items: [
      { id: "overview", label: "Overview", icon: "▤" },
      { id: "members",  label: "Members",  icon: "◎" },
      { id: "invites",  label: "Invites",  icon: "⊞" },
    ],
  },
  {
    section: "System",
    ownerOnly: true,
    items: [
      { id: "plugins", label: "Plugins", icon: "⊕" },
    ],
  },
]

function NavItem({ item, active, onClick }) {
  return (
    <button
      onClick={onClick}
      className={`w-full flex items-center gap-2.5 px-3 py-2 rounded-lg text-sm text-left transition-colors ${
        active
          ? "bg-white text-gray-900 shadow-sm font-medium"
          : "text-gray-500 hover:bg-white/60 hover:text-gray-700"
      }`}
    >
      <span className="text-base leading-none">{item.icon}</span>
      {item.label}
    </button>
  )
}

// ─── AdminView ───────────────────────────────────────────────────────────────

function AdminView() {
  const [active, setActive] = useState("overview")
  const [stats, setStats]   = useState(null)
  const showToast = useToast()

  const currentUser = decodeJwt(localStorage.getItem("token"))
  const isOwner = currentUser?.role === "owner"

  useEffect(() => {
    authFetch(`${API_URL}/admin/stats`)
      .then(r => r.json()).then(setStats)
      .catch(err => showToast(err.message))
  }, [])

  const visibleNav = NAV.filter(g => !g.ownerOnly || isOwner)

  return (
    <div className="flex h-full gap-0">

      {/* Sidebar */}
      <aside className="w-48 shrink-0 bg-gray-50 border-r border-gray-100 p-3 flex flex-col gap-4">
        {visibleNav.map(group => (
          <div key={group.section} className="flex flex-col gap-0.5">
            <p className="px-3 mb-1 text-[10px] font-semibold text-gray-400 uppercase tracking-widest">
              {group.section}
            </p>
            {group.items.map(item => (
              <NavItem
                key={item.id}
                item={item}
                active={active === item.id}
                onClick={() => setActive(item.id)}
              />
            ))}
          </div>
        ))}
      </aside>

      {/* Content */}
      <main className="flex-1 overflow-auto p-6">
        {active === "overview" && <OverviewSection stats={stats} onGoTo={setActive} />}
        {active === "members"  && <MembersSection currentUser={currentUser} />}
        {active === "invites"  && <InvitesSection />}
        {active === "plugins"  && isOwner && <PluginsSection />}
      </main>

    </div>
  )
}

export default AdminView
