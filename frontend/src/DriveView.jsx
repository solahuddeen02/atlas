import { useState, useEffect } from "react"
import { authFetch } from "./api"
import { API_URL } from "./config"
import UploadButton from "./UploadButton"

function DriveView() {
  const LIMIT = 20
  const [items, setItems] = useState([])
  const [offset, setOffset] = useState(0)
  const [hasMore, setHasMore] = useState(false)
  const [breadcrumb, setBreadcrumb] = useState([{ id: null, name: "Home" }])
  const [q, setQ] = useState("")
  const [newFolderName, setNewFolderName] = useState("")
  const [showNewFolder, setShowNewFolder] = useState(false)
  const [renamingId, setRenamingId] = useState(null)
  const [renameValue, setRenameValue] = useState("")

  const currentFolderId = breadcrumb[breadcrumb.length - 1].id

  function fetchItems(off = 0, append = false) {
    const base = currentFolderId === null
      ? `${API_URL}/drive/root`
      : `${API_URL}/folders/${currentFolderId}`
    authFetch(`${base}?limit=${LIMIT}&offset=${off}`)
      .then(res => res.json())
      .then(data => {
        setItems(prev => append ? [...prev, ...data] : data)
        setHasMore(data.length === LIMIT)
        setOffset(off + data.length)
      })
  }

  useEffect(() => { setItems([]); setOffset(0); fetchItems(0, false) }, [currentFolderId])

  function loadMore() { fetchItems(offset, true) }

  function formatSize(bytes) {
    if (!bytes) return "-"
    if (bytes < 1024) return bytes + " B"
    if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(2) + " KB"
    return (bytes / (1024 * 1024)).toFixed(2) + " MB"
  }

  function openFolder(id, name) {
    setBreadcrumb(prev => [...prev, { id, name }])
    setQ("")
  }

  function navigateTo(index) {
    setBreadcrumb(prev => prev.slice(0, index + 1))
    setQ("")
  }

  function trashItem(id) {
    authFetch(`${API_URL}/objects/${id}`, { method: "DELETE" })
      .then(() => fetchItems(0, false))
  }

  function downloadFile(id, name) {
    authFetch(`${API_URL}/objects/${id}/download`)
      .then(res => res.blob())
      .then(blob => {
        const url = URL.createObjectURL(blob)
        const a = document.createElement("a")
        a.href = url
        a.download = name
        a.click()
        URL.revokeObjectURL(url)
      })
  }

  function createFolder() {
    if (!newFolderName.trim()) return
    authFetch(`${API_URL}/folders`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: newFolderName.trim(), parent_id: currentFolderId }),
    })
      .then(res => res.json())
      .then(() => { fetchItems(0, false); setNewFolderName(""); setShowNewFolder(false) })
  }

  function startRename(item) {
    setRenamingId(item.id)
    setRenameValue(item.name)
  }

  function submitRename(id) {
    if (!renameValue.trim()) { setRenamingId(null); return }
    authFetch(`${API_URL}/objects/${id}/rename`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: renameValue.trim() }),
    })
      .then(() => { fetchItems(0, false); setRenamingId(null) })
  }

  const filtered = q
    ? items.filter(f => f.name.toLowerCase().includes(q.toLowerCase()))
    : items

  return (
    <div className="flex flex-col gap-3">

      {/* Breadcrumb */}
      <div className="flex items-center gap-1 text-sm text-gray-500">
        {breadcrumb.map((crumb, i) => (
          <span key={i} className="flex items-center gap-1">
            {i > 0 && <span>/</span>}
            <button
              className={i === breadcrumb.length - 1 ? "font-medium text-gray-800" : "hover:underline"}
              onClick={() => navigateTo(i)}
              disabled={i === breadcrumb.length - 1}
            >
              {crumb.name}
            </button>
          </span>
        ))}
      </div>

      {/* Toolbar */}
      <div className="flex gap-2 items-center">
        <UploadButton onUpload={fetchItems} parentId={currentFolderId} />
        <input
          className="border rounded px-3 py-2 text-sm flex-1"
          placeholder="Search files..."
          value={q}
          onChange={e => setQ(e.target.value)}
        />
        <button
          className="text-sm border rounded px-3 py-2 hover:bg-gray-100 whitespace-nowrap"
          onClick={() => setShowNewFolder(v => !v)}
        >
          + New folder
        </button>
      </div>

      {/* New folder input */}
      {showNewFolder && (
        <div className="flex gap-2">
          <input
            className="border rounded px-3 py-2 text-sm flex-1"
            placeholder="Folder name"
            value={newFolderName}
            onChange={e => setNewFolderName(e.target.value)}
            onKeyDown={e => e.key === "Enter" && createFolder()}
            autoFocus
          />
          <button className="text-sm bg-slate-900 text-white px-3 py-2 rounded" onClick={createFolder}>
            Create
          </button>
        </div>
      )}

      {/* Item list */}
      {filtered.length === 0
        ? <p className="text-gray-500">No files found.</p>
        : (
          <div className="grid grid-cols-1 gap-2">
            {filtered.map(item => (
              <div key={item.id} className="bg-white rounded shadow px-4 py-3 flex justify-between items-center">
                <div className="flex items-center gap-2 flex-1 min-w-0">
                  <span>{item.type === "folder" ? "📁" : "📄"}</span>
                  {renamingId === item.id
                    ? (
                      <input
                        className="border rounded px-2 py-1 text-sm flex-1"
                        value={renameValue}
                        onChange={e => setRenameValue(e.target.value)}
                        onKeyDown={e => { if (e.key === "Enter") submitRename(item.id); if (e.key === "Escape") setRenamingId(null) }}
                        onBlur={() => submitRename(item.id)}
                        autoFocus
                      />
                    )
                    : item.type === "folder"
                      ? <button className="font-medium hover:underline text-left truncate" onClick={() => openFolder(item.id, item.name)}>{item.name}</button>
                      : <span className="font-medium truncate">{item.name}</span>
                  }
                </div>
                <div className="flex items-center gap-4 ml-4 shrink-0">
                  {item.type !== "folder" && (
                    <span className="text-sm text-gray-400">{formatSize(item.size)}</span>
                  )}
                  <div className="flex gap-3">
                    <button className="text-sm text-gray-400 hover:text-gray-700" onClick={() => startRename(item)}>
                      Rename
                    </button>
                    {item.type !== "folder" && (
                      <button className="text-sm text-blue-500 hover:underline" onClick={() => downloadFile(item.id, item.name)}>
                        Download
                      </button>
                    )}
                    <button className="text-sm text-red-400 hover:text-red-600" onClick={() => trashItem(item.id)}>
                      Delete
                    </button>
                  </div>
                </div>
              </div>
            ))}
          </div>
        )
      }
      {hasMore && !q && (
        <button
          className="text-sm text-gray-500 hover:underline text-center py-2"
          onClick={loadMore}
        >
          Load more
        </button>
      )}
    </div>
  )
}

export default DriveView
