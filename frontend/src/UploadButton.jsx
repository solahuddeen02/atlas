import { useState } from "react"

function UploadButton({ onUpload }) {
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

    fetch(`http://127.0.0.1:8000/objects/upload?obj_type=file&name=${file.name}`, {
      method: "POST",
      body: form,
    })
      .then(res => {
        if (!res.ok) throw new Error("Upload failed")
        return res.json()
      })
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
