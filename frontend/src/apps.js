import DriveView from "./DriveView"
import PhotosView from "./PhotosView"
import TrashView from "./TrashView"

export const APPS = [
    { id: "drive",  label: "Drive",  component: DriveView  },
    { id: "photos", label: "Photos", component: PhotosView },
    { id: "trash",  label: "Trash",  component: TrashView  },
]
