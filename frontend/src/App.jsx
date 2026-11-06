import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import SampleLibrary from './SampleLibrary.jsx'
import DetectionPanel from './DetectionPanel.jsx'
import DshConsole from './DshConsole.jsx'

const labels = {
  PENDING: '排队中', INVESTIGATING: '调查中', COMPLETED: '检测完成', WAITING_APPROVAL: '待审批',
  NEEDS_REVIEW: '需人工处理', ORDER_CREATED: '已创建工单', REJECTED: '已驳回',
  NORMAL: '正常', ABNORMAL: '发现异常', UNCERTAIN: '不确定', ERROR: '检测失败',
  HIGH: '高风险', MEDIUM: '中风险', LOW: '低风险',
  ENGINEER: '质量工程师', MANAGER: '质量主管', INSPECTOR: '质检员',
  ADMIN: '系统管理员', AUDITOR: '审计员',
  OPEN: '待处置', IN_PROGRESS: '处理中', PENDING_REINSPECTION: '待复检', CLOSED: '已关闭',
  PASS: '未见原异常', FAIL: '仍见异常',
  APPROVED: '已批准', RETURNED: '已退回',
}

function label(value) { return labels[value] || value || '—' }
const toolLabels = { get_batch_info: '批次查询', get_inspection_history: '检测历史', search_quality_cases: '历史案例检索', search_sop_documents: 'SOP 检索' }
function date(value) { return value ? new Date(value).toLocaleString('zh-CN', { hour12: false }) : '—' }

async function request(path, token, options = {}) {
  const headers = { ...(options.headers || {}) }
  if (token) headers.Authorization = `Bearer ${token}`
  let body = options.body
  if (body && !(body instanceof FormData)) {
    headers['Content-Type'] = 'application/json'
    body = JSON.stringify(body)
  }
  let response
  try { response = await fetch(`/api/v1${path}`, { ...options, headers, body }) }
  catch { throw new Error('无法连接业务服务，请确认 API 正在运行。') }
  const data = await response.json().catch(() => ({}))
  if (!response.ok) throw new Error(data.message || `请求失败（${response.status}）`)
  return data
}

function Badge({ value }) {
  const tone = ['HIGH', 'ABNORMAL', 'ERROR', 'REJECTED', 'NEEDS_REVIEW', 'FAIL'].includes(value) ? 'red'
    : ['COMPLETED', 'NORMAL', 'APPROVED', 'CLOSED', 'PASS'].includes(value) ? 'green'
      : ['MEDIUM', 'UNCERTAIN', 'INVESTIGATING', 'WAITING_APPROVAL', 'PENDING_REINSPECTION', 'PENDING'].includes(value) ? 'amber' : 'blue'
  return <span className={`badge ${tone}`}>{label(value)}</span>
}

function Message({ error, success }) {
  if (error) return <div className="message error" role="alert">{error}</div>
  if (success) return <div className="message success" role="status">{success}</div>
  return null
}

function Login({ onLogin }) {
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  async function submit(event) {
    event.preventDefault(); setError(''); setBusy(true)
    try {
      const auth = await request('/auth/login', null, { method: 'POST', body: { username, password } })
      onLogin({ token: auth.access_token, role: auth.role, username,
                mustChangePassword: auth.must_change_password })
    } catch (reason) { setError(reason.message) }
    finally { setBusy(false) }
  }
  return <div className="login-shell">
    <div className="login-hero"><div className="logo-mark">Q<span>F</span></div>
      <p className="overline">QUALITY OPERATIONS · 2.0</p>
      <h1>每一次质量判断，<br />都有证据可循。</h1>
      <p>从图像检测到调查报告、共同审批与复检，沿同一条记录追溯事实和决定。</p>
      <div className="hero-steps"><span>01 检测</span><span>02 调查</span><span>03 审批</span><span>04 处置</span></div>
    </div>
    <div className="login-side"><form className="login-card" onSubmit={submit}>
      <p className="overline">WELCOME BACK</p><h2>登录质量工作台</h2>
      <p className="muted">使用管理员配置的本地账户。演示账户密码保存在项目本地 .env 中。</p>
      <label>账号<input autoComplete="username" required value={username} onChange={e => setUsername(e.target.value)} placeholder="例如 inspector.demo" /></label>
      <label>密码<input type="password" autoComplete="current-password" required value={password} onChange={e => setPassword(e.target.value)} placeholder="输入账户密码" /></label>
      <Message error={error} /><button className="button primary wide" disabled={busy}>{busy ? '正在登录…' : '进入工作台 →'}</button>
      <p className="login-foot">账户由本租户管理员创建；系统不提供自助注册。</p>
    </form></div>
  </div>
}

function ChangePassword({ session, onChanged, onLogout }) {
  const [oldPassword, setOldPassword] = useState('')
  const [newPassword, setNewPassword] = useState('')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  async function submit(event) {
    event.preventDefault(); setBusy(true); setError('')
    try {
      await request('/auth/change-password', session.token, { method: 'POST', body: {
        old_password: oldPassword, new_password: newPassword } })
      onChanged()
    } catch (reason) { setError(reason.message) }
    finally { setBusy(false) }
  }
  return <div className="password-shell"><form className="login-card" onSubmit={submit}>
    <div className="logo-mark">Q<span>F</span></div><h2>首次登录请修改密码</h2>
    <p className="muted">临时密码仅用于首次登录。新密码至少 12 位。</p>
    <label>临时密码<input type="password" autoComplete="current-password" required value={oldPassword} onChange={e => setOldPassword(e.target.value)} /></label>
    <label>新密码<input type="password" autoComplete="new-password" minLength="12" required value={newPassword} onChange={e => setNewPassword(e.target.value)} /></label>
    <Message error={error} /><button className="button primary wide" disabled={busy}>保存新密码</button>
    <button className="text-button" type="button" onClick={onLogout}>退出登录</button>
  </form></div>
}

function SectionHead({ eyebrow, title, description, right }) {
  return <div className="section-head"><div><p className="overline">{eyebrow}</p><h1>{title}</h1><p className="muted">{description}</p></div>{right}</div>
}

function TaskTable({ items, onOpen, empty = '暂无任务' }) {
  if (!items.length) return <div className="empty">{empty}</div>
  return <div className="table-wrap"><table><thead><tr><th>任务 / 批次</th><th>检测结果</th><th>流程状态</th><th>创建时间</th><th></th></tr></thead>
    <tbody>{items.map(task => <tr key={task.task_id}>
      <td><strong>{task.task_id}</strong><small>{task.batch_id} · {task.product_code}</small></td>
      <td><Badge value={task.detection_status} /></td><td><Badge value={task.status} /></td>
      <td>{date(task.created_at)}</td><td><button className="text-button" onClick={() => onOpen(task.task_id)}>查看详情 ↗</button></td>
    </tr>)}</tbody></table></div>
}

function Overview({ tasks, loading, error, reload, openTask }) {
  const [query, setQuery] = useState('')
  const filtered = useMemo(() => tasks.filter(task => `${task.task_id} ${task.batch_id} ${task.product_code}`.toLowerCase().includes(query.toLowerCase())), [tasks, query])
  const count = status => tasks.filter(t => t.status === status).length
  return <><SectionHead eyebrow="OPERATIONS / OVERVIEW" title="质量任务总览" description="查看任务正式状态；检测分数仅是模型输出，不等于缺陷概率。"
    right={<button className="button secondary" onClick={reload}>刷新任务</button>} />
    <div className="metric-grid"><div className="metric"><span>当前任务</span><strong>{tasks.length}</strong><small>最近 100 条以内</small></div>
      <div className="metric"><span>待审批</span><strong>{count('WAITING_APPROVAL')}</strong><small>需要人工审阅报告</small></div>
      <div className="metric"><span>需人工处理</span><strong>{count('NEEDS_REVIEW')}</strong><small>检测或证据校验异常</small></div>
      <div className="metric"><span>已创建工单</span><strong>{count('ORDER_CREATED')}</strong><small>进入现场处置</small></div></div>
    <div className="panel"><div className="panel-head"><div><h2>检测任务</h2><p>以数据库中的状态为准</p></div>
      <input className="search" value={query} onChange={e => setQuery(e.target.value)} placeholder="搜索任务号、批次或产品" /></div>
      <Message error={error} />{loading ? <div className="empty">正在加载任务…</div> : <TaskTable items={filtered} onOpen={openTask} empty="当前范围内暂无检测任务" />}</div></>
}

function NewInspection({ token, batches, onCreated }) {
  const [batchId, setBatchId] = useState('')
  const [file, setFile] = useState(null)
  const [remark, setRemark] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const batch = batches.find(item => item.id === batchId)
  async function submit(event) {
    event.preventDefault(); setError('')
    if (!batch || !file) { setError('请选择生产批次和检测图像。'); return }
    if (!['image/jpeg', 'image/png'].includes(file.type) || file.size > 20 * 1024 * 1024) {
      setError('仅支持不超过 20 MB 的 JPG 或 PNG 图像。'); return
    }
    const body = new FormData()
    body.set('batch_id', batch.id); body.set('product_code', batch.product_code)
    body.set('remark', remark); body.set('image', file)
    setBusy(true)
    try {
      const created = await request('/inspections', token, { method: 'POST',
        headers: { 'Idempotency-Key': crypto.randomUUID() }, body })
      onCreated(created.task_id)
    } catch (reason) { setError(reason.message) }
    finally { setBusy(false) }
  }
  return <><SectionHead eyebrow="INSPECTION / NEW" title="新建检测" description="上传原始图像并核对批次；任务创建后会自动执行检测与调查。" />
    <div className="two-column"><form className="panel form-panel" onSubmit={submit}>
      <div className="panel-head"><div><h2>检测资料</h2><p>请先核对样品所属批次</p></div><span className="step-dot">01</span></div>
      <label>生产批次 <em>*</em><select required value={batchId} onChange={e => setBatchId(e.target.value)}><option value="">请选择批次</option>{batches.map(item => <option key={item.id} value={item.id}>{item.id} · {item.product_code}</option>)}</select></label>
      <div className="info-strip"><span>产品编码</span><strong>{batch?.product_code || '选择批次后自动带出'}</strong></div>
      <label>检测图像 <em>*</em><input type="file" accept="image/png,image/jpeg" required onChange={e => setFile(e.target.files?.[0] || null)} /></label>
      {file && <p className="file-info">{file.name} · {(file.size / 1024 / 1024).toFixed(2)} MB</p>}
      <label>备注<textarea rows="4" value={remark} onChange={e => setRemark(e.target.value)} placeholder="记录取样位置或拍摄条件（可选）" /></label>
      <Message error={error} /><button className="button primary" disabled={busy}>{busy ? '正在提交…' : '提交检测任务 →'}</button>
    </form><aside className="panel guide"><p className="overline">PROCESS GUIDE</p><h2>提交之后</h2>
      <div className="guide-item"><b>1</b><div><strong>视觉检测</strong><p>返回正常、异常、不确定或错误状态。</p></div></div>
      <div className="guide-item"><b>2</b><div><strong>证据调查</strong><p>异常与不确定任务查询资料、生成报告，并核验每条证据引用。</p></div></div>
      <div className="guide-item"><b>3</b><div><strong>人工决定</strong><p>高风险需工程师与主管共同批准后才创建工单。</p></div></div>
      <p className="guide-note">当前使用合成场景与模拟检测。报告需人工核对，不自动确认根因。</p>
    </aside></div></>
}

function ReportView({ report, taskId, token }) {
  const [evidence, setEvidence] = useState(null)
  const [error, setError] = useState('')
  async function openEvidence(id) {
    setError('')
    try { setEvidence(await request(`/inspections/${taskId}/evidence/${id}`, token)) }
    catch (reason) { setError(reason.message) }
  }
  return <section className="panel report-panel"><div className="panel-head"><div><p className="overline">INVESTIGATION REPORT · V{report.report_version}</p>
    <h2>调查报告</h2><p>{report.report_id} · {date(report.generated_at)} · {report.analysis_mode === 'llm' ? 'AI 工具调查' : '模拟调查'}</p></div><Badge value={report.risk_level} /></div>
    <div className="report-alert"><strong>{report.defect_summary}</strong><span>当前证据不足以确认根因，建议按下方计划现场复核。</span></div>
    <div className="report-grid"><div><h3>已观察事实</h3>{report.observed_facts.map((fact, index) => <div className="fact" key={index}>
      <span className="fact-number">{String(index + 1).padStart(2, '0')}</span><div><p>{fact.text}</p>
      <div className="evidence-links">{fact.evidence_ids.map(id => <button key={id} onClick={() => openEvidence(id)}>{id} ↗</button>)}</div></div></div>)}</div>
    <div><h3>待核实与建议</h3><ul className="compact-list">{report.missing_evidence.map(item => <li key={item}>{item}</li>)}</ul>
      <div className="subhead">建议动作</div><ul className="compact-list">{report.recommended_actions.map(item => <li key={item}>{item}</li>)}</ul></div></div>
    <div className="hypothesis"><span>待验证假设</span>{report.hypotheses.map((item, index) => <div key={index}><strong>{item.cause}</strong><p>{item.uncertainty}</p><p>验证方式：{item.verification_plan}</p></div>)}</div>
    {report.tool_trace?.length > 0 && <details className="tool-trace"><summary>查看调查工具记录 · {report.tool_trace.length} 次调用</summary>
      <p className="subtle">检索采用本地字符向量相似度；参考资料仍需核对现场适用性。</p>
      <div className="table-wrap"><table><thead><tr><th>只读工具</th><th>执行结果</th><th>返回条数</th><th>耗时</th></tr></thead>
        <tbody>{report.tool_trace.map((item, index) => <tr key={index}><td>{toolLabels[item.tool_name] || item.tool_name}</td>
          <td>{item.status === 'OK' ? '成功' : item.status === 'EMPTY' ? '无结果' : `失败：${item.error_code}`}</td>
          <td>{item.result_count}</td><td>{item.elapsed_ms} ms</td></tr>)}</tbody></table></div></details>}
    <Message error={error} />{evidence && <div className="evidence-box"><button className="close" onClick={() => setEvidence(null)} aria-label="关闭证据">×</button>
      <p className="overline">EVIDENCE SOURCE</p><h3>{evidence.title}</h3><p>{evidence.excerpt}</p>
      <dl><dt>来源</dt><dd>{evidence.source_type} · {evidence.source_id}</dd><dt>来源版本</dt><dd>{evidence.source_version}</dd><dt>检索时间</dt><dd>{date(evidence.retrieved_at)}</dd></dl>
    </div>}</section>
}

function ApprovalView({ approval, role, token, onChanged, isCurrent = true }) {
  const [decision, setDecision] = useState('APPROVE')
  const [reason, setReason] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const needed = approval.stage === 'MANAGER' ? 'MANAGER' : 'ENGINEER'
  const canDecide = isCurrent && approval.status === 'PENDING' && role === needed
  async function submit(event) {
    event.preventDefault(); setError('')
    if (!canDecide) return
    if ((approval.risk_level === 'HIGH' || decision !== 'APPROVE') && !reason.trim()) {
      setError('此决定必须填写判断依据。'); return
    }
    setBusy(true)
    try {
      await request(`/approvals/${approval.approval_id}/decision`, token, { method: 'POST', body: {
        decision, reason, expected_version: approval.version,
        expected_report_version: approval.report_version } })
      setReason(''); onChanged()
    } catch (cause) { setError(cause.message) }
    finally { setBusy(false) }
  }
  return <section className="panel"><div className="panel-head"><div><p className="overline">HUMAN APPROVAL</p><h2>报告审批</h2>
    <p>{approval.approval_id} · 报告 v{approval.report_version}</p></div><Badge value={approval.status} /></div>
    <div className="approval-stage"><div className="stage active"><b>01</b><span>工程师审核</span></div><div className="stage-line" />
      <div className={`stage ${approval.risk_level === 'HIGH' ? 'active' : 'muted-stage'}`}><b>02</b><span>主管共同确认{approval.risk_level !== 'HIGH' ? '（无需）' : ''}</span></div></div>
    <p className="subtle">{approval.status === 'PENDING' ? `当前阶段：${label(needed)}` : `审批已结束：${label(approval.status)}`} · 风险：{label(approval.risk_level)}。高风险两次决定须由不同账户完成。</p>
    {approval.decisions.length > 0 && <div className="decision-history"><h3>审批记录</h3>{approval.decisions.map((item, index) => <div key={index}>
      <span>{label(item.actor_role)} · {item.decision}</span><small>{date(item.decided_at)}</small><p>{item.reason || '未填写意见'}</p></div>)}</div>}
    {canDecide && <form className="approval-form" onSubmit={submit}><label>审批决定<select value={decision} onChange={e => setDecision(e.target.value)}>
      <option value="APPROVE">批准</option><option value="REJECT">驳回</option><option value="RETURN_FOR_MORE_EVIDENCE">退回补证</option></select></label>
      <label>判断依据{(approval.risk_level === 'HIGH' || decision !== 'APPROVE') && <em> *</em>}
        <textarea rows="3" value={reason} onChange={e => setReason(e.target.value)} placeholder="说明证据判断与批准条件" /></label>
      <Message error={error} /><button className="button primary" disabled={busy}>{busy ? '正在提交…' : approval.risk_level === 'HIGH' && approval.stage === 'ENGINEER' ? '批准并送主管复核 →' : '提交审批决定 →'}</button></form>}
    {!canDecide && approval.status === 'PENDING' && <div className="info-strip">当前阶段需要{label(needed)}账户审批。</div>}
    {!isCurrent && <div className="info-strip">此为历史报告的审批记录，仅供查阅。</div>}
    {approval.work_order_id && <div className="info-strip">已创建工单：<strong>{approval.work_order_id}</strong></div>}
  </section>
}

function OrderView({ order, token, role, onChanged }) {
  const [reason, setReason] = useState('')
  const [action, setAction] = useState('')
  const [file, setFile] = useState(null)
  const [result, setResult] = useState('PASS')
  const [note, setNote] = useState('')
  const [reviewReason, setReviewReason] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const latest = order.reinspections.at(-1)
  async function command(path, body) {
    setError(''); setBusy(true)
    try { await request(`/work-orders/${order.work_order_id}${path}`, token, { method: 'POST', body }); onChanged() }
    catch (cause) { setError(cause.message) }
    finally { setBusy(false) }
  }
  async function submitReinspection(event) {
    event.preventDefault(); setError('')
    if (!file || !['image/png', 'image/jpeg'].includes(file.type) || file.size > 20 * 1024 * 1024) {
      setError('请选择不超过 20 MB 的 JPG 或 PNG 图像。'); return
    }
    const body = new FormData()
    body.set('image', file); body.set('result', result); body.set('note', note)
    body.set('expected_version', String(order.version))
    setBusy(true)
    try { await request(`/work-orders/${order.work_order_id}/reinspections`, token, { method: 'POST', body }); setNote(''); setFile(null); onChanged() }
    catch (cause) { setError(cause.message) }
    finally { setBusy(false) }
  }
  return <section className="panel"><div className="panel-head"><div><p className="overline">WORK ORDER · V{order.version}</p><h2>工单处置</h2>
    <p>{order.work_order_id} · 来源任务 {order.task_id}</p></div><Badge value={order.status} /></div>
    {order.reinspections.length > 0 && <div className="rounds"><h3>复检历史</h3>{order.reinspections.map((item, index) => <div className="round" key={item.id}>
      <b>第 {index + 1} 轮 · {item.id}</b><Badge value={item.result} />
      <p>{item.note}</p><small>工单版本 {item.order_version} · {date(item.created_at)}</small>
      <p className="round-decision">工程师决定：{item.status === 'RECAPTURE_REQUIRED' ? '要求补拍' : item.review_decision || '待确认'}{item.review_reason ? ` · ${item.review_reason}` : ''}</p>
    </div>)}</div>}
    {role === 'ENGINEER' && order.status === 'OPEN' && <div className="action-block"><h3>开始处置</h3><label>开始原因<input value={reason} onChange={e => setReason(e.target.value)} placeholder="说明现场处置目的" /></label>
      <button className="button primary" disabled={busy || !reason.trim()} onClick={() => command('/start', { reason, expected_version: order.version })}>开始处置</button></div>}
    {role === 'ENGINEER' && order.status === 'IN_PROGRESS' && <div className="action-block"><h3>记录处置措施</h3><label>具体措施<textarea rows="3" value={action} onChange={e => setAction(e.target.value)} placeholder="写明清洁、调整或复核动作" /></label>
      <button className="button primary" disabled={busy || !action.trim()} onClick={() => command('/handling', { action, expected_version: order.version })}>提交并进入待复检</button></div>}
    {role === 'INSPECTOR' && order.status === 'PENDING_REINSPECTION' && (!latest || latest.review_decision) && <form className="action-block" onSubmit={submitReinspection}><h3>提交新一轮复检</h3>
      <label>复检图像<input type="file" accept="image/png,image/jpeg" required onChange={e => setFile(e.target.files?.[0] || null)} /></label>
      <label>观察结论<select value={result} onChange={e => setResult(e.target.value)}><option value="PASS">未见原异常</option><option value="FAIL">仍见异常</option><option value="UNCERTAIN">无法判断</option></select></label>
      <label>观察说明<textarea rows="3" required value={note} onChange={e => setNote(e.target.value)} placeholder="记录取样位置、拍摄条件与观察结果" /></label>
      <button className="button primary" disabled={busy}>提交复检记录</button></form>}
    {role === 'ENGINEER' && order.status === 'PENDING_REINSPECTION' && latest && !latest.review_decision && <div className="action-block"><h3>确认当前复检</h3>
      <p className="subtle">质检员结论：{label(latest.result)}。无法判断时不得关闭工单。</p>
      <label>确认依据<textarea rows="3" value={reviewReason} onChange={e => setReviewReason(e.target.value)} placeholder="写明确认或补拍依据" /></label>
      <div className="button-row">{(latest.result === 'PASS' ? [['CONFIRM_PASS', '通过并关闭'], ['CONFIRM_FAIL', '不通过，退回处理']]
        : latest.result === 'UNCERTAIN' ? [['REQUEST_RECAPTURE', '要求重新取样 / 补拍'], ['CONFIRM_FAIL', '确认不合格，退回处理']]
          : [['CONFIRM_FAIL', '不通过，退回处理'], ['REQUEST_RECAPTURE', '要求补拍']]).map(([decision, text]) =>
          <button key={decision} className="button secondary" disabled={busy || !reviewReason.trim()} onClick={() => command(`/reinspections/${latest.id}/review`, {
            decision, reason: reviewReason, expected_version: order.version })}>{text}</button>)}</div></div>}
    <Message error={error} />
  </section>
}

function SupplementPanel({ task, items, token, role, onQueued, versions }) {
  const [file, setFile] = useState(null)
  const [note, setNote] = useState('')
  const [key, setKey] = useState(() => crypto.randomUUID())
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [preview, setPreview] = useState(null)
  const [previewLabel, setPreviewLabel] = useState('补证图像，待人工核验')
  useEffect(() => () => { if (preview) URL.revokeObjectURL(preview) }, [preview])
  async function submit(event) {
    event.preventDefault(); setError('')
    if (!file || !['image/png', 'image/jpeg'].includes(file.type) || file.size > 20 * 1024 * 1024) {
      setError('请选择不超过 20 MB 的 JPG 或 PNG 图像。'); return
    }
    const body = new FormData()
    body.set('image', file); body.set('note', note)
    body.set('expected_version', String(task.version))
    body.set('expected_report_version', String(task.latest_report_version))
    setBusy(true)
    try {
      await request(`/inspections/${task.task_id}/supplements`, token, { method: 'POST',
        headers: { 'Idempotency-Key': key }, body })
      setFile(null); setNote(''); setKey(crypto.randomUUID()); onQueued()
    } catch (reason) { setError(reason.message) }
    finally { setBusy(false) }
  }
  async function showImage(id) {
    setError('')
    try {
      const suffix = id ? `/supplements/${id}/image` : '/image'
      const response = await fetch(`/api/v1/inspections/${task.task_id}${suffix}`, {
        headers: { Authorization: `Bearer ${token}` } })
      if (!response.ok) throw new Error('补证图像加载失败，请刷新后重试。')
      setPreviewLabel(id ? '补证图像，待人工核验' : '任务原始检测图像')
      setPreview(URL.createObjectURL(await response.blob()))
    } catch (reason) { setError(reason.message) }
  }
  const returned = task.status === 'NEEDS_REVIEW' && task.error_code === 'MORE_EVIDENCE_REQUIRED'
  if (!returned && !items.length) return null
  return <section className="panel supplement-panel"><div className="panel-head"><div><p className="overline">ADDITIONAL EVIDENCE</p>
    <h2>补证与重新调查</h2><p>新材料按轮次追加，原始图像和历史报告继续保留。</p></div>
      <button className="text-button" onClick={() => showImage(null)}>查看原始图像 ↗</button></div>
    {returned && <div className="report-alert"><strong>报告 v{task.latest_report_version} 已退回补证</strong><span>{task.return_reason}</span></div>}
    {items.map(item => <div className="round" key={item.supplement_id}><strong>报告 v{item.report_version} 的补证材料</strong>
      <p>{item.note}</p><small>{date(item.created_at)} · {item.supplement_id}</small>
      <p className="subtle">回应退回意见：{versions.find(version => version.approval_id === item.returned_approval_id)?.decisions.at(-1)?.reason || '见历史审批记录'}</p>
      <div><button className="text-button" onClick={() => showImage(item.supplement_id)}>查看补证图像 ↗</button></div></div>)}
    {preview && <div className="supplement-preview"><button className="text-button" onClick={() => setPreview(null)}>关闭图像</button>
      <img src={preview} alt={previewLabel} /></div>}
    {returned && role === 'INSPECTOR' && <form className="action-block supplement-form" onSubmit={submit}>
      <h3>提交材料并生成报告 v{task.latest_report_version + 1}</h3>
      <label>补证图像<input type="file" required accept="image/png,image/jpeg" onChange={e => { setFile(e.target.files?.[0] || null); setKey(crypto.randomUUID()) }} /></label>
      <label>补证说明<textarea required maxLength="4000" rows="4" value={note} onChange={e => { setNote(e.target.value); setKey(crypto.randomUUID()) }} placeholder="回应退回原因，记录取样位置、拍摄条件和观察变化" /></label>
      <p className="subtle">补证图像保存供人工核验，AI 结合说明重新调查；当前补证图像尚未接入自动检测。</p>
      <button className="button primary" disabled={busy}>{busy ? '正在提交…' : '提交补证并重新调查 →'}</button>
    </form>}
    {returned && role !== 'INSPECTOR' && <p className="subtle">等待原任务质检员补充图像和说明后，系统会创建新一版调查。</p>}
    <Message error={error} />
  </section>
}

function Detail({ task, report, approval, order, token, role, reload, versions, selectedVersion, selectVersion, supplements, onQueued }) {
  if (!task) return <div className="empty">正在加载任务详情…</div>
  return <><SectionHead eyebrow={`INSPECTION / ${task.task_id}`} title="检测详情" description={`批次 ${task.batch_id} · 产品 ${task.product_code}`}
    right={<button className="button secondary" onClick={reload}>刷新状态</button>} />
    <div className="detail-summary panel"><div><span>流程状态</span><Badge value={task.status} /></div><div><span>检测结论</span><Badge value={task.detection_status} /></div>
      <div><span>异常分数</span><strong>{task.anomaly_score == null ? '—' : task.anomaly_score.toFixed(2)}</strong><small>模型输出，非缺陷概率</small></div>
      <div><span>版本</span><strong>v{task.version}</strong><small>{task.model_version || '等待检测'}</small></div></div>
    {task.status === 'PENDING' && <div className="message info">{task.execution?.status === 'RUNNING' ? '正在执行检测与调查。' : task.execution?.attempts > 0 ? '任务正在等待自动重试。' : '任务已入队，等待检测与调查。'}页面会自动刷新。</div>}
    {task.status === 'INVESTIGATING' && <div className="message info">正在查询资料并生成调查报告，页面会自动刷新。</div>}
    {task.error_code && <div className="message error">当前需要人工处理：{task.error_code}。系统不会将此结果当作正常或已批准。</div>}
    {versions.length > 0 && <section className="panel version-panel"><div><h2>报告版本与审批历史</h2><p className="subtle">当前调查版本 v{task.current_report_version}；历史记录只读。</p></div>
      <label>查看版本<select aria-label="查看报告版本" value={selectedVersion ?? 'latest'} onChange={e => selectVersion(e.target.value === 'latest' ? null : Number(e.target.value))}>
        <option value="latest">跟随最新报告</option>{versions.map(item => <option key={item.report_id} value={item.report_version}>报告 v{item.report_version} · {label(item.approval_status)}</option>)}</select></label>
      {report && report.report_version < task.current_report_version && <div className="message info">下方为历史报告 v{report.report_version}；当前调查为 v{task.current_report_version}。</div>}
    </section>}
    <div className="detail-stack">
      <DetectionPanel key={task.task_id} task={task} token={token} />
      <SupplementPanel key={task.task_id} task={task} items={supplements} versions={versions} token={token} role={role} onQueued={onQueued} />
      {report && <ReportView key={report.report_id} report={report} taskId={task.task_id} token={token} />}
      {approval && <ApprovalView key={`${approval.approval_id}-${approval.version}`} approval={approval} isCurrent={approval.report_version === task.current_report_version} role={role} token={token} onChanged={reload} />}
      {order && <OrderView key={`${order.work_order_id}-${order.version}`} order={order} role={role} token={token} onChanged={reload} />}</div></>
}

function Admin({ token }) {
  const [users, setUsers] = useState([])
  const [error, setError] = useState('')
  const [created, setCreated] = useState(null)
  const [form, setForm] = useState({ username: '', display_name: '', role: 'INSPECTOR', active: true, reason: '' })
  const [busy, setBusy] = useState(false)
  const load = useCallback(async () => {
    try { setUsers((await request('/users', token)).items); setError('') }
    catch (reason) { setError(reason.message) }
  }, [token])
  useEffect(() => { load() }, [load])
  async function submit(event) {
    event.preventDefault(); setError(''); setCreated(null); setBusy(true)
    try {
      const result = await request('/users', token, { method: 'POST', body: form })
      setCreated(result); setForm({ username: '', display_name: '', role: 'INSPECTOR', active: true, reason: '' }); load()
    } catch (reason) { setError(reason.message) }
    finally { setBusy(false) }
  }
  return <><SectionHead eyebrow="SYSTEM / ACCESS" title="账户与权限" description="仅本租户管理员可以创建账户。临时密码只在创建响应中出现一次。" />
    <div className="two-column"><div className="panel"><div className="panel-head"><div><h2>本租户账户</h2><p>{users.length} 个账户</p></div><button className="text-button" onClick={load}>刷新</button></div>
      <div className="user-list">{users.map(user => <div key={user.id}><span><strong>{user.display_name}</strong><small>{user.username}</small></span><Badge value={user.role} /><span className={user.active ? 'account-active' : 'account-disabled'}>{user.active ? '启用' : '停用'}</span></div>)}</div></div>
      <form className="panel form-panel" onSubmit={submit}><div className="panel-head"><div><h2>创建账户</h2><p>密码由服务端随机生成</p></div></div>
        <label>账号<input required value={form.username} onChange={e => setForm({ ...form, username: e.target.value })} placeholder="例如 new.inspector" /></label>
        <label>姓名<input required value={form.display_name} onChange={e => setForm({ ...form, display_name: e.target.value })} /></label>
        <label>角色<select value={form.role} onChange={e => setForm({ ...form, role: e.target.value })}>{['INSPECTOR', 'ENGINEER', 'MANAGER', 'ADMIN', 'AUDITOR'].map(role => <option key={role} value={role}>{label(role)}</option>)}</select></label>
        <label className="check-label"><input type="checkbox" checked={form.active} onChange={e => setForm({ ...form, active: e.target.checked })} /> 创建后立即启用</label>
        <label>创建原因<textarea required rows="3" value={form.reason} onChange={e => setForm({ ...form, reason: e.target.value })} /></label>
        <Message error={error} /><button className="button primary" disabled={busy}>创建账户</button>
        {created && <div className="one-time"><strong>一次性临时密码</strong><code>{created.temporary_password}</code>
          <p>请立即通过安全渠道交付。离开此页面后不再显示；首次登录必须修改。</p><button className="text-button" type="button" onClick={() => setCreated(null)}>我已记录，隐藏密码</button></div>}
      </form></div></>
}

export default function App() {
  const [session, setSession] = useState(() => {
    try { return JSON.parse(sessionStorage.getItem('qualityflow-session') || 'null') }
    catch { return null }
  })
  const [view, setView] = useState('overview')
  const [tasks, setTasks] = useState([])
  const [batches, setBatches] = useState([])
  const [loading, setLoading] = useState(false)
  const [listError, setListError] = useState('')
  const [selectedId, setSelectedId] = useState(null)
  const [detail, setDetail] = useState(null)
  const [report, setReport] = useState(null)
  const [approval, setApproval] = useState(null)
  const [order, setOrder] = useState(null)
  const [detailError, setDetailError] = useState('')
  const [versions, setVersions] = useState([])
  const [supplements, setSupplements] = useState([])
  const [selectedVersion, setSelectedVersion] = useState(null)
  const detailSequence = useRef(0)
  const token = session?.token
  const role = session?.role

  function saveSession(next) { setSession(next); sessionStorage.setItem('qualityflow-session', JSON.stringify(next)) }
  function logout() { sessionStorage.removeItem('qualityflow-session'); setSession(null); setTasks([]); setSelectedId(null); setView('overview') }
  const loadTasks = useCallback(async () => {
    if (!token) return
    setLoading(true)
    try { setTasks((await request('/inspections?limit=100', token)).items); setListError('') }
    catch (reason) { setListError(reason.message) }
    finally { setLoading(false) }
  }, [token])
  const loadDetail = useCallback(async () => {
    if (!token || !selectedId) return
    const sequence = ++detailSequence.current
    try {
      const current = await request(`/inspections/${selectedId}`, token)
      const [reportResult, versionsResult, supplementResult, orderResult] = await Promise.allSettled([
        current.report_id ? request(`/inspections/${selectedId}/report${selectedVersion ? `?version=${selectedVersion}` : ''}`, token) : Promise.resolve(null),
        request(`/inspections/${selectedId}/reports`, token), request(`/inspections/${selectedId}/supplements`, token),
        current.work_order_id ? request(`/work-orders/${current.work_order_id}`, token) : Promise.resolve(null),
      ])
      const loadedReport = reportResult.status === 'fulfilled' ? reportResult.value : null
      const loadedApproval = loadedReport?.approval_id && ['ENGINEER', 'MANAGER', 'AUDITOR'].includes(role)
        ? await request(`/approvals/${loadedReport.approval_id}`, token) : null
      if (sequence !== detailSequence.current) return
      setDetail(current); setDetailError(''); setReport(loadedReport); setApproval(loadedApproval)
      setVersions(versionsResult.status === 'fulfilled' ? versionsResult.value.items : [])
      setSupplements(supplementResult.status === 'fulfilled' ? supplementResult.value.items : [])
      setOrder(orderResult.status === 'fulfilled' ? orderResult.value : null)
      loadTasks()
    } catch (reason) { setDetailError(reason.message) }
  }, [token, selectedId, selectedVersion, role, loadTasks])
  useEffect(() => {
    if (!token || session?.mustChangePassword) return
    loadTasks()
    request('/batches', token).then(data => setBatches(data.items)).catch(() => {})
  }, [token, session?.mustChangePassword, loadTasks])
  useEffect(() => { loadDetail() }, [loadDetail])
  useEffect(() => {
    if (!['PENDING', 'INVESTIGATING'].includes(detail?.status) && !['READY', 'RUNNING'].includes(detail?.execution?.status)) return
    const timer = setInterval(loadDetail, 1500)
    return () => clearInterval(timer)
  }, [detail?.status, detail?.execution?.status, loadDetail])
  function openTask(id) { detailSequence.current++; setSelectedId(id); setSelectedVersion(null); setVersions([]); setSupplements([]); setDetail(null); setReport(null); setApproval(null); setOrder(null); setView('detail') }
  if (!session) return <Login onLogin={saveSession} />
  if (session.mustChangePassword) return <ChangePassword session={session}
    onChanged={() => saveSession({ ...session, mustChangePassword: false })} onLogout={logout} />

  const nav = [
    { id: 'overview', icon: '▦', text: '任务总览' },
    { id: 'samples', icon: '▧', text: '视觉样本库' },
    { id: 'dsh', icon: '⌘', text: 'DSH 控制台' },
    ...(role === 'INSPECTOR' ? [{ id: 'new', icon: '＋', text: '新建检测' }] : []),
    ...(['ENGINEER', 'MANAGER', 'AUDITOR'].includes(role) ? [{ id: 'approvals', icon: '◇', text: '审批中心' }] : []),
    ...(['INSPECTOR', 'ENGINEER', 'MANAGER', 'AUDITOR'].includes(role) ? [{ id: 'orders', icon: '▤', text: '工单中心' }] : []),
    ...(role === 'ADMIN' ? [{ id: 'admin', icon: '◎', text: '用户与权限' }] : []),
  ]
  const approvalTasks = tasks.filter(task => task.approval_id && task.status === 'WAITING_APPROVAL')
  const orderTasks = tasks.filter(task => task.work_order_id)
  return <div className="app-shell"><aside className="sidebar"><div className="brand"><div className="logo-mark">Q<span>F</span></div><div><strong>QualityFlow</strong><small>AI 2.0 · QUALITY SYSTEM</small></div></div>
    <p className="nav-caption">工作空间</p><nav>{nav.map(item => <button key={item.id} className={view === item.id ? 'active' : ''} onClick={() => setView(item.id)}><span>{item.icon}</span>{item.text}</button>)}</nav>
    <div className="sidebar-bottom"><div className="sidebar-note"><strong>演示环境</strong><p>合成业务场景；检测模式与结果以任务详情为准。</p></div>
      <div className="profile"><div className="avatar">{session.username.slice(0, 1).toUpperCase()}</div><div><strong>{session.username}</strong><small>{label(role)}</small></div></div></div></aside>
    <div className="main-shell"><header className="topbar"><div><span className="live-dot" /> 模拟工厂 A <span className="top-separator">/</span> 质量工作台</div>
      <div className="top-actions"><span>{label(role)}</span><button className="text-button" onClick={logout}>退出登录</button></div></header>
      <main>{view === 'overview' && <Overview tasks={tasks} loading={loading} error={listError} reload={loadTasks} openTask={openTask} />}
        {view === 'new' && <NewInspection token={token} batches={batches} onCreated={openTask} />}
        {view === 'samples' && <SampleLibrary token={token} role={role} batches={batches} request={request} />}
        {view === 'dsh' && <DshConsole tasks={tasks} loading={loading} listError={listError} reload={loadTasks}
          role={role} token={token} request={request} openTask={openTask} />}
        {view === 'detail' && <><button className="back-link" onClick={() => setView('overview')}>← 返回任务总览</button>
          <Message error={detailError} /><Detail task={detail} report={report} approval={approval} order={order} token={token} role={role} reload={loadDetail}
            versions={versions} supplements={supplements} selectedVersion={selectedVersion} selectVersion={setSelectedVersion}
            onQueued={() => { setSelectedVersion(null); loadDetail() }} /></>}
        {view === 'approvals' && <><SectionHead eyebrow="HUMAN REVIEW" title="审批中心" description="审批绑定不可变报告版本；高风险需要工程师与主管共同批准。" />
          <div className="panel"><div className="panel-head"><h2>待审批任务</h2><span className="count-pill">{approvalTasks.length}</span></div>
            <TaskTable items={approvalTasks} onOpen={openTask} empty="当前没有待审批任务" /></div></>}
        {view === 'orders' && <><SectionHead eyebrow="WORK ORDER" title="工单中心" description="处置、复检与工程师决定沿任务链保留。" />
          <div className="panel"><div className="panel-head"><h2>已创建工单</h2><span className="count-pill">{orderTasks.length}</span></div>
            <TaskTable items={orderTasks} onOpen={openTask} empty="当前没有工单" /></div></>}
        {view === 'admin' && role === 'ADMIN' && <Admin token={token} />}</main>
      <footer>QualityFlow AI 2.0 · 演示环境 · 检测模式与结果以任务详情为准；调查模式以报告标识为准。</footer>
    </div></div>
}
