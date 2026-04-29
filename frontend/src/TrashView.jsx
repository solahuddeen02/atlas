import { useState, useEffect } from "react"
import { authFetch } from "./api"

function TrashView() {
  const [items, setItems] = useState([])

  function loadTrash() {
    authFetch("http://127.0.0.1:8000/objects/trash")
      .then(res => res.json())
      .then(data => setItems(data))
  }

  useEffect(() => {
    loadTrash()
  }, [])

  function restore(id) {
    authFetch(`http://127.0.0.1:8000/objects/${id}/restore`, { method: "POST" })
      .then(() => loadTrash())
  }

  if (items.length === 0) return <p className="text-gray-500">Trash is empty.</p>

  return (
    <div className="grid grid-cols-1 gap-2">
      {items.map(item => (
        <div key={item.id} className="bg-white rounded shadow px-4 py-3 flex justify-between items-center">
          <span className="font-medium">{item.name}</span>
          <span className="text-sm text-gray-400">{item.deleted_at?.slice(0, 10) ?? "-"}</span>
          <button
            className="text-sm text-blue-500 hover:underline"
            onClick={() => restore(item.id)}
          >
            Restore
          </button>
        </div>
      ))}
    </div>
  )
}

export default TrashView
