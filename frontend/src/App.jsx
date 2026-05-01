import { useState } from 'react'
import Sidebar from './Sidebar'
import DriveView from './DriveView'
import PhotosView from './PhotosView'
import LoginPage from './LoginPage'
import TrashView from './TrashView'

function App() {
  const [activeItem, setActiveItem] = useState("Drive")
  const [token, setToken] = useState(localStorage.getItem("token"))

  function logout() {
    localStorage.removeItem("token")
    setToken(null)
  }

  if (!token) return <LoginPage onLogin={setToken} />

  return (
    <div className="flex h-screen">
      <Sidebar activeItem={activeItem} setActiveItem={setActiveItem} />
      <div className="flex-1 bg-gray-100 p-4 overflow-auto">
        <div className="flex justify-end mb-4">
          <button
            className="text-sm text-gray-500 hover:text-red-500"
            onClick={logout}
          >
            Logout
          </button>
        </div>
        {activeItem === "Drive" && <DriveView />}
        {activeItem === "Photos" && <PhotosView />}
        {activeItem === "Trash" && <TrashView />}
      </div>
    </div>
  )
}

export default App
