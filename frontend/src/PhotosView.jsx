import { useEffect, useState } from "react"
import { authFetch } from "./api"
import PhotoCard from "./PhotoCard"

function PhotosView() {
    const [photos, setPhotos] = useState([])
    const [selected, setSelected] = useState(null)
    const [imgSrc, setImgSrc] = useState(null)
    const [q, setQ] = useState("")

    useEffect(() => {
        authFetch("http://127.0.0.1:8000/photos")
            .then(res => res.json())
            .then(data => setPhotos(data))
    }, [])

    function openPhoto(photo) {
        setSelected(photo)
        authFetch(`http://127.0.0.1:8000/objects/${photo.id}/download`)
            .then(res => res.blob())
            .then(blob => setImgSrc(URL.createObjectURL(blob)))
    }

    function closeModal() {
        if (imgSrc) URL.revokeObjectURL(imgSrc)
        setSelected(null)
        setImgSrc(null)
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
                        <PhotoCard key={photo.id} photo={photo} onClick={() => openPhoto(photo)} />
                    ))}
                </div>
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
                        <button className="text-sm text-gray-500 hover:underline" onClick={closeModal}>
                            Close
                        </button>
                    </div>
                </div>
            )}
        </>
    )
}

export default PhotosView
