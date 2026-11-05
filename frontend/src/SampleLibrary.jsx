import { useEffect, useRef, useState } from 'react'
import ProtectedImage from './ProtectedImage.jsx'

const labelName = { NORMAL: '正常', ABNORMAL: '异常' }
const splitName = { TRAIN: '训练集', VALIDATION: '验证集', TEST: '测试集' }

export default function SampleLibrary({ token, role, batches, request }) {
  const [knownProducts, setKnownProducts] = useState([])
  const [newProduct, setNewProduct] = useState('')
  const [product, setProduct] = useState('')
  const products = [...new Set([...batches.map(batch => batch.product_code), ...knownProducts, ...(product ? [product] : [])])]
  const [data, setData] = useState(null)
  const [capabilities, setCapabilities] = useState(null)
  const [label, setLabel] = useState('NORMAL')
  const [split, setSplit] = useState('TRAIN')
  const [source, setSource] = useState('')
  const [note, setNote] = useState('')
  const [files, setFiles] = useState([])
  const [busy, setBusy] = useState(false)
  const [exporting, setExporting] = useState(false)
  const [error, setError] = useState('')
  const [loadError, setLoadError] = useState('')
  const [success, setSuccess] = useState('')
  const [refresh, setRefresh] = useState(0)
  const [preview, setPreview] = useState(null)
  const fileInput = useRef(null)
  const canWrite = ['ENGINEER', 'MANAGER', 'ADMIN'].includes(role)
  useEffect(() => {
    let active = true
    request('/detector', token).then(result => {
      if (active) { setCapabilities(result); setKnownProducts(result.profiles.map(item => item.product_code)) }
    }).catch(reason => { if (active) setLoadError(reason.message) })
    return () => { active = false }
  }, [token, request])
  useEffect(() => { if (!product && products.length) setProduct(products[0]) }, [product, products.join('|')])
  useEffect(() => {
    if (!product) return
    let active = true
    setData(null); setLoadError(''); setPreview(null)
    Promise.all([request(`/visual-samples?product_code=${encodeURIComponent(product)}`, token), request('/detector', token)])
      .then(([samples, detector]) => { if (active) { setData(samples); setCapabilities(detector) } })
      .catch(reason => { if (active) setLoadError(reason.message) })
    return () => { active = false }
  }, [product, refresh, token, request])

  async function submit(event) {
    event.preventDefault(); setError(''); setSuccess('')
    if (!files.length || files.length > 50) { setError('每批请选择 1 至 50 张 JPG/PNG 图像。'); return }
    setBusy(true)
    let imported = 0, repeated = 0
    const failed = [], reasons = []
    for (const file of files) {
      try {
        if (!['image/png', 'image/jpeg'].includes(file.type) || file.size > 20 * 1024 * 1024) throw Error('仅支持 20 MB 以内的 JPG/PNG')
        const body = new FormData()
        for (const [name, value] of Object.entries({ product_code: product, label, split, source, note })) body.set(name, value)
        body.set('image', file)
        const result = await request('/visual-samples', token, { method: 'POST', body })
        if (result.replayed) repeated++; else imported++
      } catch (reason) { failed.push(file); reasons.push(`${file.name}：${reason.message}`) }
    }
    setFiles(failed); if (fileInput.current) fileInput.current.value = ''
    setSuccess(`已导入 ${imported} 张，识别到相同记录 ${repeated} 张，失败 ${failed.length} 张。`)
    setError(reasons.slice(0, 5).join('；')); setBusy(false); setRefresh(value => value + 1)
  }

  async function exportManifest() {
    setError(''); setExporting(true)
    try {
      const result = await request(`/visual-samples/manifest?product_code=${encodeURIComponent(product)}`, token)
      const url = URL.createObjectURL(new Blob([JSON.stringify(result, null, 2)], { type: 'application/json' }))
      const link = document.createElement('a')
      link.href = url; link.download = `visual-samples-${product}-${result.manifest_sha256.slice(0, 8)}.json`
      link.click(); setTimeout(() => URL.revokeObjectURL(url), 1000)
    } catch (reason) { setError(reason.message) }
    finally { setExporting(false) }
  }
  const profile = capabilities?.profiles.find(item => item.product_code === product)
  return <>
    <div className="section-head"><div><p className="overline">VISUAL DATA / DATASET</p><h1>视觉样本库</h1>
      <p className="muted">整理一个产品类别的正常与异常图像，为模型训练、阈值校准和独立评测准备数据。</p></div></div>
    <section className="panel sample-toolbar"><label>产品<select aria-label="样本产品" disabled={busy} value={product} onChange={e => setProduct(e.target.value)}>
      {!products.length && <option value="">暂无样本产品</option>}{products.map(item => <option key={item}>{item}</option>)}</select></label>
      <div><strong>{capabilities?.mode === 'http' ? '已选择外部检测接口' : '当前使用模拟检测'}</strong>
        <p>导入样本会保存人工标签与划分；模型训练和质量评测需单独执行。</p>
        {profile?.configured && <small>{profile.model_version} · 阈值 {profile.threshold_version}</small>}
        {profile && !profile.configured && <small>当前产品检测配置缺失，检测上传将被拦截。</small>}</div>
      <button className="button secondary" disabled={!data || exporting || busy} onClick={exportManifest}>{exporting ? '正在导出…' : '导出样本清单'}</button>
      {canWrite && <form className="sample-new-product" onSubmit={event => { event.preventDefault(); setProduct(newProduct.trim()); setNewProduct(''); setError(''); setSuccess('') }}>
        <label>新样本产品代码<input aria-label="新样本产品代码" required maxLength="64" pattern="[A-Za-z0-9][A-Za-z0-9_-]{0,63}" disabled={busy}
          value={newProduct} onChange={e => setNewProduct(e.target.value)} placeholder="例如 PART-A" /></label>
        <button className="button secondary" disabled={busy}>使用此类别</button>
        <small>可先建立样本类别；生产检测仍须关联正式产品批次。</small></form>}
    </section>
    <div className="sample-counts">{Object.entries(splitName).map(([key, name]) => <div className="metric" key={key}>
      <span>{name}</span><strong>{data ? Object.values(data.counts[key]).reduce((a, b) => a + b, 0) : '—'}</strong>
      <small>正常 {data?.counts[key].NORMAL ?? '—'} · 异常 {data?.counts[key].ABNORMAL ?? '—'}</small></div>)}</div>
    {success && <div className="message success" role="status">{success}</div>}
    {(error || loadError) && <div className="message error" role="alert">{error || loadError}</div>}
    <div className={canWrite ? 'two-column sample-columns' : ''}>
      <section className="panel"><div className="panel-head"><div><h2>已登记样本</h2><p>共 {data?.total ?? '—'} 张 · 展示最近 100 张</p></div>
        <button className="text-button" disabled={busy} onClick={() => setRefresh(value => value + 1)}>刷新</button></div>
        {data?.items.length ? <div className="table-wrap"><table><thead><tr><th>样本 / 来源</th><th>人工标签</th><th>划分</th><th>图像</th></tr></thead>
          <tbody>{data.items.map(item => <tr key={item.sample_id}><td><strong>{item.sample_id}</strong><small>{item.source}</small></td>
            <td>{labelName[item.label]}</td><td>{splitName[item.split]}</td><td><button className="text-button" onClick={() => setPreview(item)}>查看图像</button></td></tr>)}</tbody></table></div>
          : <div className="empty">{data ? '该产品暂无样本。先整理人工确认的正常图像，再补充验证集和测试集。' : loadError ? '样本加载失败，可点击刷新重试。' : '正在加载样本…'}</div>}
        {preview && <ProtectedImage key={preview.sample_id} path={preview.image_path} token={token} alt={`样本 ${preview.sample_id}，人工标签${labelName[preview.label]}`} onClose={() => setPreview(null)} />}
      </section>
      {canWrite && <form className="panel form-panel sample-form" onSubmit={submit}>
        <div className="panel-head"><div><h2>导入图像样本</h2><p>同批文件使用相同标签与数据划分</p></div></div>
        <fieldset disabled={busy || !product}>
          <label>人工标签<select aria-label="样本人工标签" value={label} onChange={e => { setLabel(e.target.value); if (e.target.value === 'ABNORMAL' && split === 'TRAIN') setSplit('VALIDATION') }}>
            <option value="NORMAL">正常</option><option value="ABNORMAL">异常</option></select></label>
          <label>数据划分<select aria-label="样本数据划分" value={split} onChange={e => setSplit(e.target.value)}>
            <option value="TRAIN" disabled={label !== 'NORMAL'}>训练集（仅正常图像）</option><option value="VALIDATION">验证集（用于校准阈值）</option><option value="TEST">测试集（独立评测）</option></select></label>
          <label>图像文件<input ref={fileInput} type="file" multiple accept="image/jpeg,image/png" onChange={e => setFiles([...e.target.files])} /></label>
          <p className="file-info">已选择 {files.length} 张 · 每批最多 50 张，每张不超过 20 MB</p>
          <label>样本来源<input required maxLength="200" value={source} onChange={e => setSource(e.target.value)} placeholder="例如：设备、拍摄批次或公开数据集名称" /></label>
          <label>备注<textarea maxLength="2000" rows="3" value={note} onChange={e => setNote(e.target.value)} placeholder="记录拍摄条件、缺陷说明和数据使用范围" /></label>
          <p className="subtle">同一图像只能登记一次，避免重复进入训练和评测集。请按工件、批次和拍摄时段分组划分；重复拍摄的近似图像仍需人工排查。</p>
          <label className="check-label"><input type="checkbox" required /> 我已人工核对标签、数据来源和划分</label>
          <button className="button primary" disabled={busy || !files.length}>{busy ? '正在逐张导入…' : '导入样本'}</button>
        </fieldset>
      </form>}
    </div>
  </>
}
