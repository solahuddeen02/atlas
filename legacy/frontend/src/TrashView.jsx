import { useState, useEffect } from "react"
import { authFetch } from "./api"
import { API_URL } from "./config"
import { useToast } from "./Toast"

function TrashView() {
  const LIMIT = 20
  const showToast = useToast()
  const [items, setItems] = useState([])
  const [offset, setOffset] = useState(0)
  const [hasMore, setHasMore] = useState(false)

  function loadTrash(off = 0, append = false) {
    authFetch(`${API_URL}/objects/trash?limit=${LIMIT}&offset=${off}`)
      .then(res => res.json())
      .then(data => {
        setItems(prev => append ? [...prev, ...data] : data)
        setHasMore(data.length === LIMIT)
        setOffset(off + data.length)
      })
      .catch(err => showToast(err.message))
  }

  useEffect(() => { loadTrash(0, false) }, [])

  function restore(id) {
    authFetch(`${API_URL}/objects/${id}/restore`, { method: "POST" })
      .then(() => loadTrash(0, false))
      .catch(err => showToast(err.message))
  }

  function permanentDelete(id, name) {
    if (!confirm(`ลบ "${name}" ถาวร? ไม่สามารถ restore ได้อีก`)) return
    authFetch(`${API_URL}/objects/${id}/permanent`, { method: "DELETE" })
      .then(() => loadTrash(0, false))
      .catch(err => showToast(err.message))
  }

  function emptyTrash() {
    if (!confirm(`ลบทั้งหมด ${items.length} รายการ? ไม่สามารถ restore ได้อีก`)) return
    authFetch(`${API_URL}/objects/trash/empty`, { method: "DELETE" })
      .then(() => loadTrash(0, false))
      .catch(err => showToast(err.message))
  }

  if (items.length === 0) return <p className="text-gray-500">Trash is empty.</p>

  return (
    <div className="flex flex-col gap-3">
      <div className="flex justify-end">
        <button
          className="text-sm text-red-500 border border-red-300 rounded px-3 py-1 hover:bg-red-50"
          onClick={emptyTrash}
        >
          Empty trash ({items.length})
        </button>
      </div>
      <div className="grid grid-cols-1 gap-2">
      {items.map(item => (
        <div key={item.id} className="bg-white rounded shadow px-4 py-3 flex justify-between items-center">
          <span className="font-medium">{item.name}</span>
          <span className="text-sm text-gray-400">{item.deleted_at?.slice(0, 10) ?? "-"}</span>
          <div className="flex gap-3">
            <button className="text-sm text-blue-500 hover:underline" onClick={() => restore(item.id)}>
              Restore
            </button>
            <button className="text-sm text-red-500 hover:underline" onClick={() => permanentDelete(item.id, item.name)}>
              Delete permanently
            </button>
          </div>
        </div>
      ))}
      </div>
      {hasMore && (
        <button
          className="text-sm text-gray-500 hover:underline text-center py-2"
          onClick={() => loadTrash(offset, true)}
        >
          Load more
        </button>
      )}
    </div>
  )
}

export default TrashView
