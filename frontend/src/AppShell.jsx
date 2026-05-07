import { useNavigate } from "react-router-dom"

function AppShell({ label, icon, children, onLogout }) {
  const navigate = useNavigate()

  return (
    <div className="min-h-screen flex flex-col bg-gray-100">
      {/* Top bar */}
      <div className="bg-white border-b px-4 py-2 flex items-center gap-3">
        <button
          onClick={() => navigate("/")}
          className="text-gray-400 hover:text-gray-700 p-1 rounded hover:bg-gray-100"
          title="Back to launcher"
        >
          ⊞
        </button>
        <div className="w-px h-4 bg-gray-200" />
        <span className="text-base">{icon}</span>
        <span className="text-sm font-medium text-gray-700">{label}</span>
        <div className="flex-1" />
        <button
          onClick={onLogout}
          className="text-sm text-gray-400 hover:text-red-500"
        >
          Logout
        </button>
      </div>

      {/* Content */}
      <div className="flex-1 p-4 overflow-auto">
        {children}
      </div>
    </div>
  )
}

export default AppShell
