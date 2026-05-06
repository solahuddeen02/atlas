import { useState, useEffect } from "react"
import { authFetch } from "./api"
import { API_URL } from "./config"
import { useToast } from "./Toast"

function StatCard({ label, value, sub }) {
  return (
    <div className="bg-white rounded shadow px-6 py-5 flex flex-col gap-1">
      <span className="text-sm text-gray-400">{label}</span>
      <span className="text-2xl font-semibold text-gray-800">{value}</span>
      {sub && <span className="text-xs text-gray-400">{sub}</span>}
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

  if (!stats) return <p className="text-gray-400 text-sm">Loading...</p>

  return (
    <div className="flex flex-col gap-6">
      <div className="flex items-center justify-between">
        <h2 className="text-lg font-semibold text-gray-700">Overview</h2>
        <button
          onClick={inviteMember}
          className="text-sm bg-blue-500 text-white px-3 py-1.5 rounded hover:bg-blue-600"
        >
          Invite member
        </button>
      </div>
      <div className="grid grid-cols-2 gap-4 sm:grid-cols-3">
        <StatCard label="Members" value={stats.members} />
        <StatCard label="Storage used" value={formatBytes(stats.storage_used)} />
        <StatCard label="Files" value={stats.files} />
        <StatCard label="Folders" value={stats.folders} />
        <StatCard label="Photos" value={stats.photos} />
        <StatCard label="Trashed" value={stats.trashed} />
      </div>
    </div>
  )
}

export default ManagementView
