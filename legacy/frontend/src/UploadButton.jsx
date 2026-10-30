import { useState } from "react"
import { authFetch } from "./api"
import { API_URL } from "./config"

function UploadButton({ onUpload, parentId = null }) {
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState(null)
  const [success, setSuccess] = useState(false)

  function handleChange(e) {
    const file = e.target.files[0]  // ไฟล์ที่เลือก
    if (!file) return

    setLoading(true)
    setError(null)
    setSuccess(false)

    const form = new FormData()
    form.append("file", file)

    const parentParam = parentId ? `&parent_id=${parentId}` : ""
    authFetch(`${API_URL}/objects/upload?obj_type=file&name=${encodeURIComponent(file.name)}${parentParam}`, {
      method: "POST",
      body: form,
    })
      .then(res => res.json())
      .then(() => {
        setSuccess(true)
        onUpload()
      })
      .catch(err => setError(err.message))
      .finally(() => setLoading(false))
  }

  return (<div>
    <input type="file" onChange={handleChange} disabled={loading} />
    {loading && <p>Uploading...</p>}
    {error && <p style={{ color: "red" }}>{error}</p>}
    {success && <p style={{ color: "green" }}>Upload successful!</p>}
  </div>)
}

export default UploadButton
