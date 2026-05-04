import { useState } from "react"
import RegisterPage from "./RegisterPage"
import { API_URL } from "./config"

function LoginPage({ onLogin }) {
    const [username, setUsername] = useState("")
    const [password, setPassword] = useState("")
    const [error, setError] = useState(null)
    const [showRegister, setShowRegister] = useState(false)

    function handleSubmit() {
        fetch(`${API_URL}/auth/login`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ username, password }),
        })
            .then(res => {
                if (!res.ok) throw new Error("Username หรือ password ไม่ถูกต้อง")
                return res.json()
            })
            .then(data => {
                localStorage.setItem("token", data.access_token)
                onLogin(data.access_token)
            })
            .catch(err => setError(err.message))
    }

    if (showRegister) { return <RegisterPage onBack={() => setShowRegister(false)} /> }
    return (
        <div className="flex h-screen items-center justify-center bg-gray-100">
            <div className="bg-white p-8 rounded shadow w-80">
                <h1 className="text-xl font-bold mb-4">Atlas</h1>
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