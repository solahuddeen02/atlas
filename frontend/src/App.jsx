import { useState } from 'react'
import Sidebar from './Sidebar'
import LoginPage from './LoginPage'
import JoinView from './JoinView'
import { APPS } from './apps'

const joinToken = new URLSearchParams(window.location.search).get("token")

function App() {
  const [activeItem, setActiveItem] = useState(APPS[0].id)
  const [token, setToken] = useState(localStorage.getItem("token"))

  function logout() {
    localStorage.removeItem("token")
    setToken(null)
  }

  if (joinToken && !token) return <JoinView token={joinToken} onJoin={setToken} />
  if (!token) return <LoginPage onLogin={setToken} />

  const ActiveComponent = APPS.find(a => a.id === activeItem)?.component

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
        {ActiveComponent && <ActiveComponent />}
      </div>
    </div>
  )
}

export default App
