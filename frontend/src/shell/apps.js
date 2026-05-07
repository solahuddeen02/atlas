import DriveView from "../plugins/drive/DriveView"
import PhotosView from "../plugins/photos/PhotosView"
import TrashView from "../plugins/trash/TrashView"
import AdminView from "../platform/AdminView"

// Plugin UIs — bundled but only shown if the plugin is installed in the backend
export const PLUGIN_REGISTRY = {
  drive:  { label: "Drive",  icon: "📁", path: "/drive",  section: "personal", component: DriveView  },
  photos: { label: "Photos", icon: "📷", path: "/photos", section: "personal", component: PhotosView },
  trash:  { label: "Trash",  icon: "🗑️", path: "/trash",  section: "personal", component: TrashView  },
}

// Platform apps — always present, not plugin-dependent
export const PLATFORM_APPS = [
  { id: "admin", label: "Admin", path: "/admin", icon: "⚙️", section: "platform", component: AdminView, minRole: "admin", fullHeight: true },
]

export const SECTIONS = [
  { id: "personal",  label: "Personal"  },
  { id: "platform",  label: "Platform"  },
]
