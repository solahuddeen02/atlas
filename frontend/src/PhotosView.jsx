import { useEffect, useState } from "react"
import { authFetch } from "./api"
import { API_URL } from "./config"
import PhotoCard from "./PhotoCard"

function PhotosView() {
    const LIMIT = 20
    const [photos, setPhotos] = useState([])
    const [offset, setOffset] = useState(0)
    const [hasMore, setHasMore] = useState(false)
    const [selected, setSelected] = useState(null)
    const [imgSrc, setImgSrc] = useState(null)
    const [q, setQ] = useState("")

    function loadPhotos(off = 0, append = false) {
        authFetch(`${API_URL}/photos?limit=${LIMIT}&offset=${off}`)
            .then(res => res.json())
            .then(data => {
                setPhotos(prev => append ? [...prev, ...data] : data)
                setHasMore(data.length === LIMIT)
                setOffset(off + data.length)
            })
    }

    useEffect(() => { loadPhotos(0, false) }, [])

    function openPhoto(photo) {
        setSelected(photo)
        authFetch(`${API_URL}/objects/${photo.id}/download`)
            .then(res => res.blob())
            .then(blob => setImgSrc(URL.createObjectURL(blob)))
    }

    function closeModal() {
        if (imgSrc) URL.revokeObjectURL(imgSrc)
        setSelected(null)
        setImgSrc(null)
    }

    function trashPhoto(id) {
        authFetch(`${API_URL}/objects/${id}`, { method: "DELETE" })
            .then(() => loadPhotos(0, false))
    }

    const filtered = q
        ? photos.filter(p => p.name.toLowerCase().includes(q.toLowerCase()))
        : photos

    if (photos.length === 0) {
        return <p className="text-center text-gray-500">No photos found.</p>
    }

    return (
        <>
            <input
                className="border rounded px-3 py-2 text-sm w-full mb-3"
                placeholder="Search photos..."
                value={q}
                onChange={e => setQ(e.target.value)}
            />

            {filtered.length === 0 ? (
                <p className="text-center text-gray-500">No photos match.</p>
            ) : (
                <div className="grid grid-cols-3 gap-4">
                    {filtered.map(photo => (
                        <PhotoCard
                            key={photo.id}
                            photo={photo}
                            onClick={() => openPhoto(photo)}
                            onDelete={() => trashPhoto(photo.id)}
                        />
                    ))}
                </div>
            )}
            {hasMore && !q && (
                <button
                    className="text-sm text-gray-500 hover:underline text-center py-2 w-full"
                    onClick={() => loadPhotos(offset, true)}
                >
                    Load more
                </button>
            )}

            {selected && (
                <div
                    className="fixed inset-0 bg-black/70 flex items-center justify-center z-50"
                    onClick={closeModal}
                >
                    <div
                        className="bg-white rounded-lg p-4 max-w-3xl max-h-[90vh] flex flex-col items-center gap-3"
                        onClick={e => e.stopPropagation()}
                    >
                        <p className="font-medium">{selected.name}</p>
                        {imgSrc
                            ? <img src={imgSrc} className="max-h-[75vh] max-w-full object-contain rounded" />
                            : <p className="text-gray-400 py-10">Loading...</p>
                        }
                        <div className="flex gap-4">
                            <button
                                className="text-sm text-red-400 hover:text-red-600"
                                onClick={() => { trashPhoto(selected.id); closeModal() }}
                            >
                                Delete
                            </button>
                            <button className="text-sm text-gray-500 hover:underline" onClick={closeModal}>
                                Close
                            </button>
                        </div>
                    </div>
                </div>
            )}
        </>
    )
}

export default PhotosView
