import { useEffect, useRef, useState } from 'react'
import './dsh-console.css'

const statuses = {
  PENDING: '排队中', INVESTIGATING: '调查中', COMPLETED: '检测完成', WAITING_APPROVAL: '待审批',
  NEEDS_REVIEW: '需人工处理', ORDER_CREATED: '已创建工单', REJECTED: '已驳回',
  NORMAL: '正常', ABNORMAL: '发现异常', UNCERTAIN: '不确定', ERROR: '失败',
  READY: '等待执行', RUNNING: '执行中', SUCCEEDED: '已完成', FAILED: '失败',
  SUCCESS: '成功', OK: '成功', EMPTY: '无结果', HIGH: '高风险', MEDIUM: '中风险', LOW: '低风险',
}
const toolNames = {
  get_batch_info: '批次查询', get_inspection_history: '检测历史',
  search_quality_cases: '历史案例检索', search_sop_documents: 'SOP 检索',
}
const runtimeLabels = {
  available: '运行环境已配置', cli_missing: 'DSH CLI 待配置',
  node_missing: '缺少 Node.js', pnpm_missing: '缺少 pnpm',
}
const statusText = value => statuses[value] || value || '—'
function time(value) {
  if (!value) return '—'
  // SQLite may return the stored UTC timestamp without an offset.
  const timestamp = /(?:Z|[+-]\d{2}:?\d{2})$/i.test(value) ? value : `${value}Z`
  return new Date(timestamp).toLocaleString('zh-CN', { hour12: false })
}

function Notice({ children, error = false }) {
  return <p className={`dsh-notice${error ? ' is-error' : ''}`} role={error ? 'alert' : 'status'}>{children}</p>
}

function Investigation({ task, token, request, openTask }) {
  const [record, setRecord] = useState(null)
  const [version, setVersion] = useState(null)
  const [revision, setRevision] = useState(0)
  const currentVersion = version ?? task.current_report_version ?? 1
  const reportExists = task.latest_report_version >= currentVersion
  const taskId = task.task_id
  const recordKey = `${currentVersion}/${reportExists}/${revision}`
  const current = record?.key === recordKey ? record : null

  useEffect(() => {
    let active = true
    const path = `/inspections/${encodeURIComponent(taskId)}`
    Promise.allSettled([
      request(`${path}/reports`, token),
      reportExists ? request(`${path}/report?version=${currentVersion}`, token) : Promise.resolve(null),
      request(`${path}/tools?version=${currentVersion}`, token),
    ]).then(([versions, report, tools]) => {
      if (active) setRecord({ key: recordKey, versions, report, tools })
    })
    return () => { active = false }
  }, [currentVersion, recordKey, reportExists, request, taskId, token])

  const report = current?.report.value
  const tools = current?.tools.value?.items || []
  const versions = [...new Set([task.current_report_version ?? 1,
    ...(record?.versions.value?.items || []).map(item => item.report_version)])].sort((a, b) => b - a)

  return <article className="dsh-investigation">
    <header className="dsh-detail-head">
      <div><p className="dsh-kicker">INSPECTION / {task.batch_id}</p><h2>{task.product_code} 调查</h2>
        <code>{taskId}</code></div>
      <button className="dsh-button" onClick={() => openTask(taskId)}>查看任务 ↗</button>
    </header>
    <dl className="dsh-facts">
      <div><dt>任务状态</dt><dd>{statusText(task.status)}</dd></div>
      <div><dt>检测结论</dt><dd>{statusText(task.detection_status)}</dd></div>
      <div><dt>执行方式</dt><dd>{task.execution?.mode === 'llm' ? 'AI 调查' : task.execution?.mode === 'mock' ? '模拟调查' : '—'}</dd></div>
    </dl>
    <div className="dsh-section-head dsh-version-row">
      <label>报告版本 <select value={currentVersion} onChange={event => setVersion(Number(event.target.value))}>
        {versions.map(value => <option key={value} value={value}>v{value}</option>)}
      </select></label>
      <button className="dsh-button" onClick={() => setRevision(value => value + 1)} disabled={!current}>刷新轨迹</button>
    </div>
    {!current && <Notice>正在加载调查记录…</Notice>}
    {current?.versions.status === 'rejected' && <Notice error>版本列表加载失败。{current.versions.reason.message}</Notice>}
    {current && <section className="dsh-trace-section">
      <h3>调查报告</h3>
      {current.report.status === 'rejected' ? <Notice error>{current.report.reason.message}</Notice>
        : report ? <div className="dsh-report"><div><strong>{statusText(report.risk_level)}</strong><small>{time(report.generated_at)}</small></div>
          {report.defect_summary && <p>{report.defect_summary}</p>}
          {report.hypotheses?.length > 0 && <p><b>待验证假设</b>　{report.hypotheses[0].cause}</p>}
        </div> : <Notice>该版本尚未生成报告。</Notice>}
    </section>}
    {current && <section className="dsh-trace-section">
      <div className="dsh-section-head"><h3>工具轨迹</h3><small>{tools.length} 条记录</small></div>
      {current.tools.status === 'rejected' ? <Notice error>{current.tools.reason.message}</Notice>
        : tools.length === 0 ? <Notice>该版本暂无工具调用。</Notice>
          : <ol className="dsh-trace">{tools.map(item => <li key={item.id}>
            <span className="dsh-trace-mark" aria-hidden="true">›</span>
            <div><strong>{toolNames[item.tool_name] || item.tool_name}</strong>
              <small>{time(item.created_at)}{item.error_code ? ` · ${item.error_code}` : ''}</small></div>
            <span className="dsh-trace-result">{statusText(item.status)}{item.elapsed_ms != null && ` · ${item.elapsed_ms} ms`}</span>
          </li>)}</ol>}
    </section>}
  </article>
}

function Sessions({ tasks, loading, listError, reload, token, request, openTask }) {
  const [query, setQuery] = useState('')
  const [taskId, setTaskId] = useState(null)
  const search = query.trim().toLowerCase()
  const filtered = tasks.filter(task => `${task.task_id} ${task.batch_id} ${task.product_code}`.toLowerCase().includes(search))
  const task = filtered.find(item => item.task_id === taskId) || filtered[0]
  return <div className="dsh-session-grid">
    <aside className="dsh-sessions" aria-label="巡检会话列表">
      <div className="dsh-section-head"><h2>巡检会话</h2><button className="dsh-button" onClick={reload} disabled={loading}>刷新</button></div>
      <input type="search" value={query} onChange={event => setQuery(event.target.value)}
        aria-label="搜索巡检会话" placeholder="搜索任务、批次或产品" />
      {listError && <Notice error>{listError}</Notice>}
      <div className="dsh-session-list">
        {filtered.map(item => <button key={item.task_id} onClick={() => setTaskId(item.task_id)}
          aria-pressed={item.task_id === task?.task_id} className="dsh-session">
          <span className="dsh-session-title"><strong>{item.product_code}</strong><small>{statusText(item.status)}</small></span>
          <span className="dsh-session-meta">{item.batch_id} · {item.task_id.slice(0, 8)}</span>
        </button>)}
        {filtered.length === 0 && !listError && <Notice>{loading ? '正在加载会话…' : search ? '没有匹配的巡检任务。' : '暂无可查看的巡检任务。'}</Notice>}
      </div>
    </aside>
    {task ? <Investigation key={task.task_id} task={task} token={token} request={request} openTask={openTask} />
      : <div className="dsh-empty"><span aria-hidden="true">›_</span><h2>调查工作区</h2><p>选择一条巡检会话，查看报告版本与工具轨迹。</p></div>}
  </div>
}

function Plugins({ token, request }) {
  const [inventory, setInventory] = useState(null)
  const [loading, setLoading] = useState(true)
  const [pending, setPending] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [confirmation, setConfirmation] = useState(null)
  const dialog = useRef(null)

  async function refresh() {
    setLoading(true); setError('')
    try { setInventory(await request('/dsh/plugins', token)) }
    catch (reason) { setInventory(null); setError(reason.message) }
    finally { setLoading(false) }
  }

  useEffect(() => { refresh() }, [request, token])
  useEffect(() => {
    if (confirmation && !dialog.current.open) dialog.current.showModal()
  }, [confirmation])

  async function change(event) {
    event.preventDefault()
    const { plugin, action } = confirmation
    dialog.current.close()
    setConfirmation(null); setPending(true); setNotice(''); setError('')
    try {
      setInventory(await request(`/dsh/plugins/${plugin.id}`, token, { method: 'POST', body: { action } }))
      setNotice(action === 'install' ? '已确认插件写入 DSH 配置。启动或重启配置后可验证加载。' : '已确认插件从 DSH 配置移除。运行中的实例需重启。')
    } catch (reason) {
      const failure = reason.message
      await refresh()
      setError(failure)
    } finally { setPending(false) }
  }

  return <div className="dsh-plugins" aria-busy={loading || pending}>
    <div className="dsh-section-head"><div><p className="dsh-kicker">APPROVED BUNDLES</p><h2>插件管理</h2></div>
      <button className="dsh-button" onClick={refresh} disabled={loading || pending}>刷新状态</button></div>
    {error && <Notice error>{error}</Notice>}
    {notice && <Notice>{notice}</Notice>}
    {pending && <Notice>DSH 正在更新插件配置，请等待操作完成…</Notice>}
    {loading && !inventory && <Notice>正在读取插件清单…</Notice>}
    {inventory && <>
      <div className="dsh-runtime"><div><strong>{runtimeLabels[inventory.runtime]}</strong>
        <small>独立配置 <code>{inventory.profile}</code> · 仅管理员可操作</small></div>
        <span className="dsh-pill" data-ready={inventory.runtime === 'available'}>{inventory.runtime === 'available' ? 'CLI 可用' : '待配置'}</span>
      </div>
      {inventory.runtime !== 'available' && <Notice>在服务端配置 DSH CLI、Node.js 与 pnpm 后，刷新此页即可安装项目插件。</Notice>}
      {inventory.busy && !pending && <Notice>已有插件操作在进行。操作完成后刷新状态。</Notice>}
      {inventory.plugins.map(plugin => {
        const installed = plugin.installed_version && plugin.declared && plugin.selected
        const present = plugin.installed_version || plugin.declared || plugin.selected
        const disabled = inventory.runtime !== 'available' || inventory.busy || pending || loading
        return <article className="dsh-plugin" key={plugin.id}>
          <div className="dsh-plugin-head"><div className="dsh-plugin-icon" aria-hidden="true">⌘</div>
            <div><h3>{plugin.name}</h3><code>{plugin.package}</code></div>
            <span className="dsh-pill" data-ready={Boolean(installed)}>{installed ? '已安装' : present ? '状态待核查' : '未安装'}</span></div>
          <p>为 DSH 提供 <code>qualityflow_health</code>，读取本机质量 API 的健康状态。</p>
          <dl className="dsh-plugin-facts">
            <div><dt>来源</dt><dd>项目内置</dd></div>
            <div><dt>版本</dt><dd>{plugin.installed_version || plugin.available_version || '—'}</dd></div>
            <div><dt>运行时加载</dt><dd>{plugin.activation === 'unverified' ? '待启动或重启验证' : '未加入配置'}</dd></div>
          </dl>
          <div className="dsh-plugin-actions">
            {!installed && <button className="dsh-button dsh-primary" disabled={disabled || !plugin.available_version}
              onClick={() => setConfirmation({ plugin, action: 'install' })}>{present ? '重新安装' : '安装插件'}</button>}
            {present && <button className="dsh-button" disabled={disabled}
              onClick={() => setConfirmation({ plugin, action: 'remove' })}>卸载插件</button>}
          </div>
        </article>
      })}
    </>}
    <dialog className="dsh-dialog" ref={dialog} onClose={() => setConfirmation(null)} aria-labelledby="dsh-confirm-title">
      {confirmation && <form onSubmit={change}>
        <h2 id="dsh-confirm-title">{confirmation.action === 'install' ? '安装插件' : '卸载插件'}</h2>
        <p>此操作会更新 <code>{inventory.profile}</code> 配置中的 <strong>{confirmation.plugin.name}</strong>。</p>
        <div className="dsh-plugin-actions"><button type="button" className="dsh-button" autoFocus onClick={() => dialog.current.close()}>取消</button>
          <button className="dsh-button dsh-primary" type="submit">确认{confirmation.action === 'install' ? '安装' : '卸载'}</button></div>
      </form>}
    </dialog>
  </div>
}

export default function DshConsole({ tasks, loading, listError, reload, role, token, request, openTask }) {
  const [view, setView] = useState('sessions')
  const [pluginsVisited, setPluginsVisited] = useState(false)
  const admin = role === 'ADMIN'
  const plugins = view === 'plugins' && admin
  return <section className="dsh-console" aria-label="DSH 控制台">
    <header className="dsh-hero">
      <div><p className="dsh-kicker">DEEPSEEK HARNESS · QUALITYFLOW</p>
        <h1>DSH 控制台</h1><p>查看巡检调查轨迹，管理 DeepSeek Harness 插件。</p></div>
      <span className="dsh-engine"><span /> 巡检执行 · LangGraph</span>
    </header>
    {admin && <div className="dsh-switch" role="group" aria-label="控制台视图">
      <button aria-pressed={!plugins} onClick={() => setView('sessions')}>调查会话</button>
      <button aria-pressed={plugins} onClick={() => { setPluginsVisited(true); setView('plugins') }}>插件管理</button>
    </div>}
    <div hidden={plugins}><Sessions {...{ tasks, loading, listError, reload, token, request, openTask }} /></div>
    {admin && pluginsVisited && <div hidden={!plugins}><Plugins token={token} request={request} /></div>}
  </section>
}
