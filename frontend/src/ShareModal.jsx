import { useState, useEffect } from "react"
import { authFetch } from "./api"
import { API_URL } from "./config"
import { useToast } from "./Toast"

const EXPIRY_OPTIONS = [
  { label: "No expiry", value: null },
  { label: "1 day", value: 1 },
  { label: "7 days", value: 7 },
  { label: "30 days", value: 30 },
]

function ShareModal({ file, onClose }) {
  const [links, setLinks] = useState([])
  const [expiryDays, setExpiryDays] = useState(null)
  const [creating, setCreating] = useState(false)
  const showToast = useToast()

  useEffect(() => {
    authFetch(`${API_URL}/objects/${file.id}/shares`)
      .then(res => res.json())
      .then(setLinks)
      .catch(err => showToast(err.message))
  }, [file.id])

  function createLink() {
    setCreating(true)
    authFetch(`${API_URL}/objects/${file.id}/share`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ expires_in_days: expiryDays }),
    })
      .then(res => res.json())
      .then(link => {
        setLinks(prev => [link, ...prev])
        copyUrl(link.url)
      })
      .catch(err => showToast(err.message))
      .finally(() => setCreating(false))
  }

  function copyUrl(url) {
    const full = `${API_URL}${url}`
    navigator.clipboard.writeText(full)
      .then(() => showToast("Link copied to clipboard"))
      .catch(() => showToast(`Link: ${full}`))
  }

  function revokeLink(token) {
    authFetch(`${API_URL}/share/${token}`, { method: "DELETE" })
      .then(() => setLinks(prev => prev.filter(l => l.token !== token)))
      .catch(err => showToast(err.message))
  }

  function formatExpiry(expires_at) {
    if (!expires_at) return "No expiry"
    const d = new Date(expires_at)
    return d < new Date() ? "Expired" : d.toLocaleDateString()
  }

  return (
    <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50" onClick={onClose}>
      <div className="bg-white rounded-lg shadow-xl p-6 w-full max-w-md flex flex-col gap-4" onClick={e => e.stopPropagation()}>
        <div className="flex justify-between items-center">
          <h2 className="font-semibold text-gray-800">Share — {file.name}</h2>
          <button className="text-gray-400 hover:text-gray-600 text-lg" onClick={onClose}>✕</button>
        </div>

        {/* Create new link */}
        <div className="flex gap-2">
          <select
            className="border rounded px-2 py-1.5 text-sm flex-1"
            value={expiryDays ?? ""}
            onChange={e => setExpiryDays(e.target.value ? Number(e.target.value) : null)}
          >
            {EXPIRY_OPTIONS.map(o => (
              <option key={o.label} value={o.value ?? ""}>{o.label}</option>
            ))}
          </select>
          <button
            onClick={createLink}
            disabled={creating}
            className="bg-blue-500 text-white text-sm px-3 py-1.5 rounded hover:bg-blue-600 disabled:opacity-50 whitespace-nowrap"
          >
            {creating ? "Creating..." : "+ New link"}
          </button>
        </div>

        {/* Existing links */}
        {links.length === 0
          ? <p className="text-sm text-gray-400 text-center py-2">No share links yet.</p>
          : (
            <div className="flex flex-col gap-2 max-h-64 overflow-y-auto">
              {links.map(link => (
                <div key={link.token} className="flex items-center justify-between border rounded px-3 py-2 text-sm">
                  <div className="flex flex-col min-w-0">
                    <span className="text-xs text-gray-400 truncate">{link.token.slice(0, 16)}…</span>
                    <span className="text-xs text-gray-400">{formatExpiry(link.expires_at)}</span>
                  </div>
                  <div className="flex gap-2 ml-2 shrink-0">
                    <button className="text-blue-500 hover:underline text-xs" onClick={() => copyUrl(link.url)}>Copy</button>
                    <button className="text-red-400 hover:text-red-600 text-xs" onClick={() => revokeLink(link.token)}>Revoke</button>
                  </div>
                </div>
              ))}
            </div>
          )
        }
      </div>
    </div>
  )
}

export default ShareModal
