import { useState, useEffect } from 'react'
import Sidebar from './Sidebar'
import DriveView from './DriveView'
import UploadButton from './UploadButton'
import PhotosView from './PhotosView'
import LoginPage from './LoginPage'

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
        <UploadButton onUpload={refreshFiles} />
        {activeItem === "Drive" && <DriveView files={files} />}
        {activeItem === "Photos" && <PhotosView />}
      </div>
    </div>
  )
}

export default App
