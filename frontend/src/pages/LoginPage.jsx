import { useState } from "react"
import { API_URL } from "../shared/config"
import RegisterPage from "./RegisterPage"

function LoginPage({ onLogin }) {
    const [username, setUsername] = useState("")
    const [password, setPassword] = useState("")
    const [error, setError]       = useState(null)
    const [showRegister, setShowRegister] = useState(false)

    async function handleSubmit() {
        try {
            const res = await fetch(`${API_URL}/auth/login`, {
                method: "POST",
                credentials: "include",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ username, password }),
            })
            if (!res.ok) {
                const body = await res.json().catch(() => ({}))
                throw new Error(body.detail ?? "Login failed")
            }
            const data = await res.json()

            // Notify parent so it can call /auth/me and update user state
            onLogin({ username: data.username ?? username, role: data.role })

            // Redirect back if we came from a specific subdomain
            const next = new URLSearchParams(window.location.search).get("next")
            if (next) window.location.href = next
        } catch (err) {
            setError(err.message)
        }
    }

    function handleKey(e) { if (e.key === "Enter") handleSubmit() }

    if (showRegister) return <RegisterPage onBack={() => setShowRegister(false)} />

    return (
        <div className="flex h-screen items-center justify-center bg-gray-100">
            <div className="bg-white p-8 rounded shadow w-80">
                <h1 className="text-xl font-bold mb-4">Atlas</h1>
                <input
                    className="w-full border p-2 rounded mb-2"
                    placeholder="Username"
                    value={username}
                    onChange={e => setUsername(e.target.value)}
                    onKeyDown={handleKey}
                />
                <input
                    className="w-full border p-2 rounded mb-4"
                    type="password"
                    placeholder="Password"
                    value={password}
                    onChange={e => setPassword(e.target.value)}
                    onKeyDown={handleKey}
                />
                {error && <p className="text-red-500 text-sm mb-2">{error}</p>}
                <button className="w-full bg-slate-900 text-white py-2 rounded" onClick={handleSubmit}>
                    Login
                </button>
                <button className="w-full text-sm text-gray-500 hover:underline mt-2" onClick={() => setShowRegister(true)}>
                    ยังไม่มีบัญชี? สมัครสมาชิก
                </button>
            </div>
        </div>
    )
}

export default LoginPage
