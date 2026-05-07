import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import './index.css'
import App from './shell/App.jsx'
import { ToastProvider } from './shared/Toast.jsx'

createRoot(document.getElementById('root')).render(
  <StrictMode>
    <ToastProvider>
      <App />
    </ToastProvider>
  </StrictMode>,
)
