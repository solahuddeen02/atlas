function DriveView({ files }) {
  return (
    <ul>
      {files.map(file => (
        <li key={file.id}>{file.name}</li>
      ))}
    </ul>
  )
}

export default DriveView