import { createContext, useContext, useState } from "react"

const ToastContext = createContext(null)

export function useToast() {
    return useContext(ToastContext)
}

export function ToastProvider({ children }) {
    const [msg, setMsg] = useState(null)

    function showToast(message) {
        setMsg(message)
        setTimeout(() => setMsg(null), 4000)
    }

    return (
        <ToastContext.Provider value={showToast}>
            {children}
            {msg && (
                <div
                    className="fixed bottom-4 right-4 bg-red-500 text-white text-sm px-4 py-3 rounded shadow-lg z-50 max-w-sm cursor-pointer"
                    onClick={() => setMsg(null)}
                >
                    {msg}
                </div>
            )}
        </ToastContext.Provider>
    )
}
