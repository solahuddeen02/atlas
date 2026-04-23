function Sidebar({ activeItem, setActiveItem }) {
  return (
    <div className="w-48 bg-slate-900 p-4 text-white">
      <h2 className="text-xl font-bold">Atlas</h2>
      {["Drive", "Photos", "Trash"].map(item => (
        <p
          key={item}
          onClick={() => setActiveItem(item)}
          className={`cursor-pointer px-2 py-1 rounded ${
            activeItem === item ? "bg-slate-600" : "hover:bg-slate-700"
          }`}
        >
          {item}
        </p>
      ))}
    </div>
  )
}

export default Sidebar
