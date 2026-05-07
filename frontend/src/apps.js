import DriveView from "./DriveView"
import PhotosView from "./PhotosView"
import TrashView from "./TrashView"
import WorkspaceView from "./WorkspaceView"
import SystemView from "./SystemView"

export const APPS = [
  { id: "drive",     label: "Drive",     path: "/drive",     icon: "📁", section: "personal",  component: DriveView                       },
  { id: "photos",    label: "Photos",    path: "/photos",    icon: "📷", section: "personal",  component: PhotosView                      },
  { id: "trash",     label: "Trash",     path: "/trash",     icon: "🗑️", section: "personal",  component: TrashView                       },
  { id: "workspace", label: "Workspace", path: "/workspace", icon: "👥", section: "workspace", component: WorkspaceView, minRole: "admin" },
  { id: "system",    label: "System",    path: "/system",    icon: "⚙️", section: "system",    component: SystemView,    minRole: "owner" },
]

export const SECTIONS = [
  { id: "personal",  label: "Personal"  },
  { id: "workspace", label: "Workspace" },
  { id: "system",    label: "System"    },
]
