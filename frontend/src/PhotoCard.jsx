import { useEffect, useState } from "react"
import { authFetch } from "./api"

function PhotoCard({ photo, onClick }) {
    const [thumbSrc, setThumbSrc] = useState(null)

    useEffect(() => {
        let objectUrl = null
        authFetch(`http://127.0.0.1:8000/objects/${photo.id}/download`)
            .then(res => res.blob())
            .then(blob => {
                objectUrl = URL.createObjectURL(blob)
                setThumbSrc(objectUrl)
            })
        return () => { if (objectUrl) URL.revokeObjectURL(objectUrl) }
    }, [photo.id])

    return (
        <div
            className="bg-white rounded shadow overflow-hidden cursor-pointer hover:ring-2 hover:ring-blue-400"
            onClick={onClick}
        >
            <div className="w-full h-40 bg-gray-100 flex items-center justify-center">
                {thumbSrc
                    ? <img src={thumbSrc} className="w-full h-full object-cover" alt={photo.name} />
                    : <span className="text-gray-300 text-3xl">🖼</span>
                }
            </div>
            <p className="text-xs text-center truncate px-2 py-1 text-gray-600">{photo.name}</p>
        </div>
    )
}

export default PhotoCard
