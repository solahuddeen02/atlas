import { useEffect, useState } from "react"

function PhotosView() {
    const [photos, setPhotos] = useState([])

    useEffect(() => {
        fetch("http://127.0.0.1:8000/photos")
            .then(res => res.json())
            .then(data => setPhotos(data))
    }, [])

    if (photos.length === 0) {
        return <p className="text-center text-gray-500">No photos found.</p>
    }

    return (
        <div className="grid grid-cols-3 gap-4">
            {photos.map(photo => (
                <div key={photo.id} className="bg-white rounded shadow p-2">
                    {photo.name}
                </div>
            ))}
        </div>
    )
}

export default PhotosView