import DriveView from "./DriveView"
import PhotosView from "./PhotosView"
import TrashView from "./TrashView"
import ManagementView from "./ManagementView"

export const APPS = [
    { id: "drive",      label: "Drive",      component: DriveView      },
    { id: "photos",     label: "Photos",     component: PhotosView     },
    { id: "trash",      label: "Trash",      component: TrashView      },
    { id: "management", label: "Management", component: ManagementView },
]
