import { useState, useEffect } from 'react'
import Sidebar from './Sidebar'
import DriveView from './DriveView'
import UploadButton from './UploadButton'
import PhotosView from './PhotosView'
import LoginPage from './LoginPage'
import TrashView from './TrashView'

function App() {
  const [activeItem, setActiveItem] = useState("Drive")
  const [files, setFiles] = useState([])

  const token = localStorage.getItem("token")
  if (!token) return <LoginPage />

  useEffect(() => {
    fetch("http://127.0.0.1:8000/objects")
      .then(res => res.json())
      .then(data => setFiles(data))
  }, [])

  function refreshFiles() {
    fetch("http://127.0.0.1:8000/objects")
      .then(res => res.json())
      .then(data => setFiles(data))
  }


  return (
    <div className="flex h-screen">
      <Sidebar activeItem={activeItem} setActiveItem={setActiveItem} />
      <div className="flex-1 bg-gray-100 p-4">
        <div className="flex justify-between items-center mb-4">
          <UploadButton onUpload={refreshFiles} />
          <button
            className="text-sm text-gray-500 hover:text-red-500"
            onClick={() => { localStorage.removeItem("token"); window.location.reload() }}
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
