import { SECTIONS } from "./apps"

function Sidebar({ apps, activeItem, setActiveItem, user }) {
  return (
    <div className="w-48 bg-slate-900 p-4 text-white flex flex-col gap-4">
      <h2 className="text-xl font-bold">Atlas</h2>

      {SECTIONS.map(section => {
        const sectionApps = apps.filter(a => a.section === section.id)
        if (sectionApps.length === 0) return null
        return (
          <div key={section.id} className="flex flex-col gap-0.5">
            <p className="text-xs text-slate-500 uppercase tracking-widest px-2 mb-1">
              {section.label}
            </p>
            {sectionApps.map(app => (
              <p
                key={app.id}
                onClick={() => setActiveItem(app.id)}
                className={`cursor-pointer px-2 py-1.5 rounded text-sm ${
                  activeItem === app.id ? "bg-slate-600" : "hover:bg-slate-700"
                }`}
              >
                {app.label}
              </p>
            ))}
          </div>
        )
      })}

      {user && (
        <div className="mt-auto pt-4 border-t border-slate-700">
          <p className="text-xs text-slate-400 truncate">{user.username}</p>
          <p className="text-xs text-slate-500">{user.role}</p>
        </div>
      )}
    </div>
  )
}

export default Sidebar
