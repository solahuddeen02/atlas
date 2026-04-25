function DriveView({ files }) {
  if (files.length === 0){
    return <p className="text-gray-500">No files found.</p>
  } 

  return (
    <div className="grid grid-cols-1 gap-2">
      {files.map(file => (
        <div key={file.id} className="bg-white rounded shadow px-4 py-3 flex justify-between items-center">
          <span className="font-medium">{file.name}</span>
          <span className="text-sm text-gray-400">{file.mime_type ?? "-"}</span>
        </div>
      ))}
    </div>
  )
}

export default DriveView