import { useEffect, useState } from 'react'

export default function ProtectedImage({ path, token, alt, onClose }) {
  const [url, setUrl] = useState(null)
  const [error, setError] = useState('')
  useEffect(() => {
    const controller = new AbortController()
    let objectUrl
    setUrl(null); setError('')
    fetch(path, { headers: { Authorization: `Bearer ${token}` }, signal: controller.signal })
      .then(async response => {
        if (!response.ok) throw new Error('图像暂不可用，请刷新后重试。')
        const blob = await response.blob()
        if (controller.signal.aborted) return
        objectUrl = URL.createObjectURL(blob)
        setUrl(objectUrl)
      }).catch(reason => { if (!controller.signal.aborted) setError(reason.message) })
    return () => { controller.abort(); if (objectUrl) URL.revokeObjectURL(objectUrl) }
  }, [path, token])
  return <div className="media-preview"><button className="text-button" onClick={onClose}>关闭图像</button>
    {error ? <p role="alert">{error}</p> : url ? <img src={url} alt={alt} /> : <p>正在加载图像…</p>}</div>
}
