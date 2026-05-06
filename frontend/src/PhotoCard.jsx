import { useEffect, useState } from "react"
import { authFetch } from "./api"
import { API_URL } from "./config"

function PhotoCard({ photo, onClick, onDelete }) {
    const [thumbSrc, setThumbSrc] = useState(null)

    useEffect(() => {
        let objectUrl = null
        authFetch(`${API_URL}/objects/${photo.id}/thumbnail`)
            .then(res => res.blob())
            .then(blob => {
                objectUrl = URL.createObjectURL(blob)
                setThumbSrc(objectUrl)
            })
            .catch(() => {})
        return () => { if (objectUrl) URL.revokeObjectURL(objectUrl) }
    }, [photo.id])

    return (
        <div className="bg-white rounded shadow overflow-hidden group relative">
            <div
                className="w-full h-40 bg-gray-100 flex items-center justify-center cursor-pointer"
                onClick={onClick}
            >
                {thumbSrc
                    ? <img src={thumbSrc} className="w-full h-full object-cover" alt={photo.name} />
                    : <span className="text-gray-300 text-3xl">🖼</span>
                }
            </div>
            <div className="flex justify-between items-center px-2 py-1">
                <p className="text-xs truncate text-gray-600 flex-1">{photo.name}</p>
                <button
                    className="text-xs text-red-400 hover:text-red-600 ml-2 shrink-0"
                    onClick={e => { e.stopPropagation(); onDelete() }}
                >
                    Delete
                </button>
            </div>
        </div>
    )
}

export default PhotoCard
