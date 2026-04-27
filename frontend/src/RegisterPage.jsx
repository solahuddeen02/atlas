import { useState } from "react"

function RegisterPage({ onBack }) {
  const [username, setUsername] = useState("")
  const [password, setPassword] = useState("")
  const [error, setError] = useState(null)
  const [success, setSuccess] = useState(false)

  function handleSubmit() {
    fetch("http://127.0.0.1:8000/auth/register", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username, password }),
    })
      .then(res => {
        if (!res.ok) return res.json().then(d => { throw new Error(d.detail) })
        return res.json()
      })
      .then(() => setSuccess(true))
      .catch(err => setError(err.message))
  }

  if (success) return (
    <div className="flex h-screen items-center justify-center bg-gray-100">
      <div className="bg-white p-8 rounded shadow w-80 text-center">
        <p className="text-green-600 font-medium mb-4">สมัครสมาชิกสำเร็จ</p>
        <button className="text-blue-500 hover:underline" onClick={onBack}>กลับไป Login</button>
      </div>
    </div>
  )

  return (
    <div className="flex h-screen items-center justify-center bg-gray-100">
      <div className="bg-white p-8 rounded shadow w-80">
        <h1 className="text-xl font-bold mb-4">Register</h1>
        <input
          className="w-full border p-2 rounded mb-2"
          placeholder="Username"
          value={username}
          onChange={e => setUsername(e.target.value)}
        />
        <input
          className="w-full border p-2 rounded mb-4"
          type="password"
          placeholder="Password"
          value={password}
          onChange={e => setPassword(e.target.value)}
        />
        {error && <p className="text-red-500 mb-2">{error}</p>}
        <button className="w-full bg-slate-900 text-white py-2 rounded mb-2" onClick={handleSubmit}>
          Register
        </button>
        <button className="w-full text-sm text-gray-500 hover:underline" onClick={onBack}>
          มีบัญชีแล้ว? Login
        </button>
      </div>
    </div>
  )
}

export default RegisterPage
