import { useState } from "react"
import { API_URL } from "./config"

function JoinView({ token, onJoin }) {
  const [username, setUsername] = useState("")
  const [password, setPassword] = useState("")
  const [error, setError] = useState(null)
  const [loading, setLoading] = useState(false)

  async function handleSubmit(e) {
    e.preventDefault()
    setError(null)
    setLoading(true)
    try {
      const res = await fetch(`${API_URL}/tenant/join`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ token, username, password }),
      })
      const data = await res.json()
      if (!res.ok) throw new Error(data.detail ?? "join failed")
      localStorage.setItem("token", data.access_token)
      window.history.replaceState({}, "", "/")
      onJoin(data.access_token)
    } catch (err) {
      setError(err.message)
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="min-h-screen flex items-center justify-center bg-gray-50">
      <form onSubmit={handleSubmit} className="bg-white rounded shadow p-8 w-full max-w-sm flex flex-col gap-4">
        <h1 className="text-xl font-semibold text-gray-700">Join workspace</h1>
        {error && <p className="text-sm text-red-500">{error}</p>}
        <input
          className="border rounded px-3 py-2 text-sm"
          placeholder="Username"
          value={username}
          onChange={e => setUsername(e.target.value)}
          required
        />
        <input
          type="password"
          className="border rounded px-3 py-2 text-sm"
          placeholder="Password"
          value={password}
          onChange={e => setPassword(e.target.value)}
          required
        />
        <button
          type="submit"
          disabled={loading}
          className="bg-blue-500 text-white rounded py-2 text-sm font-medium hover:bg-blue-600 disabled:opacity-50"
        >
          {loading ? "Joining..." : "Join"}
        </button>
      </form>
    </div>
  )
}

export default JoinView
