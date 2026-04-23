function UploadButton({ onUpload }) {
  function handleChange(e) {
    const file = e.target.files[0]  // ไฟล์ที่เลือก
    if (!file) return

    const form = new FormData()
    form.append("file", file)

    fetch(`http://127.0.0.1:8000/objects/upload?obj_type=file&name=${file.name}`, {
      method: "POST",
      body: form,
    })
      .then(res => res.json())
      .then(() => onUpload())  // บอก App ให้ refresh รายการ
  }

  return <input type="file" onChange={handleChange} />
}

export default UploadButton
