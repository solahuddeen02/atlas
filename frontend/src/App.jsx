import { useState, useEffect } from 'react'
import Sidebar from './Sidebar'
import DriveView from './DriveView'
import UploadButton from './UploadButton'
import PhotosView from './PhotosView'
import LoginPage from './LoginPage'
import TrashView from './TrashView'
import { authFetch } from './api'

function App() {
  const [activeItem, setActiveItem] = useState("Drive")
  const [files, setFiles] = useState([])
  const [token, setToken] = useState(localStorage.getItem("token"))

  useEffect(() => {
    if (!token) return
    authFetch("http://127.0.0.1:8000/objects")
      .then(res => {
        if (res.status === 401) { logout(); return [] }
        return res.json()
      })
      .then(data => setFiles(data))
  }, [token])

  function refreshFiles() {
    authFetch("http://127.0.0.1:8000/objects")
      .then(res => res.json())
      .then(data => setFiles(data))
  }

  function logout() {
    localStorage.removeItem("token")
    setToken(null)
  }


  if (!token) return <LoginPage onLogin={setToken} />

  return (
    <div className="flex h-screen">
      <Sidebar activeItem={activeItem} setActiveItem={setActiveItem} />
      <div className="flex-1 bg-gray-100 p-4">
        <div className="flex justify-between items-center mb-4">
          <UploadButton onUpload={refreshFiles} />
          <button
            className="text-sm text-gray-500 hover:text-red-500"
            onClick={logout}
          >
            Logout
          </button>
        </div>
        {activeItem === "Drive" && <DriveView files={files} />}
        {activeItem === "Photos" && <PhotosView />}
        {activeItem === "Trash" && <TrashView />}
      </div>
    </div>
  )
}

export default App
