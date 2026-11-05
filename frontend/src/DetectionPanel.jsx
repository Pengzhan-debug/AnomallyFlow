import { useState } from 'react'
import ProtectedImage from './ProtectedImage.jsx'

export default function DetectionPanel({ task, token }) {
  const [image, setImage] = useState(null)
  const finding = task.detection
  return <section className="panel detection-panel"><div className="panel-head"><div><p className="overline">VISUAL INSPECTION</p>
    <h2>图像与检测依据</h2><p>{task.detector_mode === 'http' ? '外部检测服务' : '模拟检测 · 未分析图像像素'}</p></div>
    <div className="detection-actions"><button className="text-button" onClick={() => setImage('image')}>查看检测原图</button>
      {task.has_anomaly_map && <button className="text-button" onClick={() => setImage('anomaly-map')}>查看异常定位图</button>}</div></div>
    {finding ? <><p>{finding.confidence_note}</p><small className="subtle">模型 {finding.model_version} · 阈值 {finding.threshold_version}
      {finding.inference_ms != null ? ` · 推理 ${finding.inference_ms} ms` : ''}</small>
      {finding.status === 'UNCERTAIN' && <div className="message info">检测结果不确定，需要人工复核。</div>}</> : <p className="subtle">等待检测结果；历史任务可能尚无完整检测元数据。</p>}
    {image && <ProtectedImage key={`${task.task_id}-${image}`} path={`/api/v1/inspections/${task.task_id}/${image}`} token={token}
      alt={image === 'image' ? '检测原图' : '模型输出异常定位图'} onClose={() => setImage(null)} />}
  </section>
}
