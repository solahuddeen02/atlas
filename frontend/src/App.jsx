import { useState } from "react"
import { Routes, Route, Navigate } from "react-router-dom"
import { APPS } from "./apps"
import { decodeToken, hasRole } from "./auth"
import Launcher from "./Launcher"
import AppShell from "./AppShell"
import LoginPage from "./LoginPage"
import JoinView from "./JoinView"

function App() {
  const [token, setToken] = useState(localStorage.getItem("token"))
  const user = decodeToken(token)

  function login(t) {
    localStorage.setItem("token", t)
    setToken(t)
  }

  function logout() {
    localStorage.removeItem("token")
    setToken(null)
  }

  if (!token) {
    const joinToken = new URLSearchParams(window.location.search).get("token")
    if (joinToken) return <JoinView token={joinToken} onJoin={login} />
    return <LoginPage onLogin={login} />
  }

  const visibleApps = APPS.filter(a => !a.minRole || hasRole(user?.role, a.minRole))

  return (
    <Routes>
      <Route path="/" element={<Launcher user={user} onLogout={logout} />} />
      {visibleApps.map(app => (
        <Route
          key={app.id}
          path={app.path}
          element={
            <AppShell label={app.label} icon={app.icon} onLogout={logout}>
              <app.component />
            </AppShell>
          }
        />
      ))}
      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  )
}

export default App
