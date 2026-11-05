const { chromium } = require('playwright')
const assert = require('node:assert/strict')
const fs = require('node:fs')
const path = require('node:path')
const root = path.resolve(__dirname, '..')
const env = fs.readFileSync(path.join(root, '.env'), 'utf8')
const taskId = process.argv[2]
if (!taskId?.startsWith('QI-')) throw Error('Pass a returned demo task id')
const base = `/api/v1/inspections/${taskId}`
const note = '合成补证联调：同位置重新取样，记录光照条件变化；图像待工程师人工核验。'
const sample = path.join(root, 'design', 'preview-report.png')

async function login(page, role) {
  const password = env.match(new RegExp(`^DEMO_${role}_PASSWORD=(.*)$`, 'm'))?.[1].trim()
  if (!password) throw Error('Demo password not configured')
  await page.locator('input[autocomplete="username"]').fill(`${role.toLowerCase()}.demo`)
  await page.locator('input[autocomplete="current-password"]').fill(password)
  await page.getByRole('button', { name: '进入工作台' }).click()
  await page.getByRole('heading', { name: '质量任务总览' }).waitFor()
}
async function openTask(page) {
  await page.locator('tr').filter({ hasText: taskId }).getByRole('button', { name: '查看详情' }).click()
  await page.getByRole('heading', { name: '补证与重新调查' }).waitFor()
}
async function token(page) {
  return page.evaluate(() => JSON.parse(sessionStorage.getItem('qualityflow-session')).token)
}
async function main() {
  const browser = await chromium.launch({ headless: true,
    executablePath: 'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe' })
  try {
    const page = await browser.newPage({ viewport: { width: 1440, height: 980 } })
    const errors = []
    let key
    page.on('pageerror', error => errors.push(error.message))
    page.on('request', request => {
      if (request.method() === 'POST' && request.url().endsWith('/supplements')) key = request.headers()['idempotency-key']
    })
    await page.goto('http://127.0.0.1:5173/')
    await login(page, 'INSPECTOR')
    const inspector = { Authorization: `Bearer ${await token(page)}` }
    const oldReport = await (await page.request.get('http://127.0.0.1:5173' + base + '/report?version=1', { headers: inspector })).json()
    assert.equal(oldReport.approval_status, 'RETURNED')
    await openTask(page)
    await page.locator('.supplement-form input[type="file"]').setInputFiles(sample)
    await page.locator('.supplement-form textarea').fill(note)
    await page.getByRole('button', { name: '提交补证并重新调查' }).click()
    await page.getByText('当前调查版本 v2', { exact: false }).waitFor()
    console.log(`Queued supplementary v2: ${taskId}; waiting for worker.`)
    await page.locator('.report-panel .overline').filter({ hasText: 'V2' }).waitFor({ timeout: 180000 })
    await page.getByRole('button', { name: '查看补证图像' }).click()
    await page.locator('.supplement-preview img').waitFor()
    await page.waitForFunction(() => document.querySelector('.supplement-preview img')?.naturalWidth > 0)
    await page.getByRole('button', { name: '查看原始图像' }).click()
    await page.waitForFunction(() => {
      const image = document.querySelector('.supplement-preview img')
      return image?.alt === '任务原始检测图像' && image.naturalWidth === 8
    })
    const selector = page.getByRole('combobox', { name: '查看报告版本' })
    await selector.selectOption('1')
    await page.locator('.report-panel .overline').filter({ hasText: 'V1' }).waitFor()
    const after = await (await page.request.get('http://127.0.0.1:5173' + base + '/report?version=1', { headers: inspector })).json()
    assert.deepEqual(after, oldReport)
    await page.screenshot({ path: path.join(root, 'design', 'preview-supplements-v1.png'), fullPage: true })
    await page.getByRole('button', { name: '退出登录' }).click()

    await login(page, 'ENGINEER')
    await openTask(page)
    await page.getByRole('combobox', { name: '查看报告版本' }).selectOption('1')
    await page.locator('.report-panel .overline').filter({ hasText: 'V1' }).waitFor()
    assert.equal(await page.locator('.approval-form').count(), 0)
    await page.getByRole('combobox', { name: '查看报告版本' }).selectOption('latest')
    await page.locator('.report-panel .overline').filter({ hasText: 'V2' }).waitFor()
    await page.locator('.approval-form textarea').fill('工程师核对 v2 补证及引用')
    await page.getByRole('button', { name: '批准并送主管复核' }).click()
    await page.getByText('当前阶段：质量主管', { exact: false }).waitFor()
    await page.getByRole('button', { name: '退出登录' }).click()

    await login(page, 'MANAGER')
    await openTask(page)
    await page.locator('.approval-form textarea').fill('主管独立复核 v2 通过')
    await page.getByRole('button', { name: '提交审批决定' }).click()
    await page.locator('.detail-summary .badge').filter({ hasText: '已创建工单' }).waitFor()
    await page.getByRole('heading', { name: '工单处置' }).waitFor()
    const manager = { Authorization: `Bearer ${await token(page)}` }
    const stale = await page.request.post(`http://127.0.0.1:5173/api/v1/approvals/${oldReport.approval_id}/decision`, {
      headers: manager, data: { decision: 'APPROVE', reason: '旧版本应被拦截', expected_version: 1, expected_report_version: 1 } })
    assert.equal(stale.status(), 409)
    const replay = await page.request.post('http://127.0.0.1:5173' + base + '/supplements', {
      headers: { ...inspector, 'Idempotency-Key': key }, multipart: { note, expected_version: '-1', expected_report_version: '1',
        image: { name: 'retry.png', mimeType: 'image/png', buffer: fs.readFileSync(sample) } } })
    assert.equal(replay.status(), 202)
    assert.equal((await replay.json()).replayed, true)
    let detail
    for (let i = 0; i < 60; i++) {
      detail = await (await page.request.get('http://127.0.0.1:5173' + base, { headers: manager })).json()
      if (detail.execution.status === 'SUCCEEDED') break
      await new Promise(resolve => setTimeout(resolve, 500))
    }
    assert.equal(detail.execution.status, 'SUCCEEDED')
    assert.equal(detail.current_report_version, 2)
    const report = await (await page.request.get('http://127.0.0.1:5173' + base + '/report', { headers: manager })).json()
    assert.equal(report.report_version, 2)
    assert(report.evidence_refs.some(item => item.source_type === 'supplement'))
    await page.screenshot({ path: path.join(root, 'design', 'preview-supplements-v2.png'), fullPage: true })
    await page.setViewportSize({ width: 390, height: 844 })
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false)
    await page.screenshot({ path: path.join(root, 'design', 'preview-supplements-mobile.png'), fullPage: true })
    assert.deepEqual(errors, [])
    console.log(`Supplement UI check passed: ${taskId}; v1 immutable, image preview, v2 co-sign/order, old decision blocked, retry replayed, checkpoint finished.`)
  } finally { await browser.close() }
}
main().catch(error => { console.error(error); process.exit(1) })
