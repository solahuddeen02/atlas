function DriveView({ files }) {
  if (files.length === 0){
    return <p className="text-gray-500">No files found.</p>
  }
  
  function formatSize(bytes) {
    if (!bytes) return "-"
    if (bytes < 1024) return bytes + " B"
    if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(2) + " KB"
    return (bytes / (1024 * 1024)).toFixed(2) + " MB"
  }

  return (
    <div className="grid grid-cols-1 gap-2">
      {files.map(file => (
        <div key={file.id} className="bg-white rounded shadow px-4 py-3 flex justify-between items-center">
          <span className="font-medium">{file.name}</span>
          <span className="text-sm text-gray-400">{formatSize(file.size)}</span>
          <span className="text-sm text-gray-400">{file.mime_type ?? "-"}</span>
        </div>
      ))}
    </div>
  )
}

export default DriveView