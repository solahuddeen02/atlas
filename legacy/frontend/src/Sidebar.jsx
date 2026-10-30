import { APPS } from "./apps"

function Sidebar({ activeItem, setActiveItem }) {
  return (
    <div className="w-48 bg-slate-900 p-4 text-white">
      <h2 className="text-xl font-bold mb-2">Atlas</h2>
      {APPS.map(app => (
        <p
          key={app.id}
          onClick={() => setActiveItem(app.id)}
          className={`cursor-pointer px-2 py-1 rounded ${
            activeItem === app.id ? "bg-slate-600" : "hover:bg-slate-700"
          }`}
        >
          {app.label}
        </p>
      ))}
    </div>
  )
}

export default Sidebar
