// Opt-in browser check. Adds explicitly synthetic 8x8 fixtures to the demo sample library.
const { chromium } = require('playwright')
const assert = require('node:assert/strict')
const fs = require('node:fs')
const path = require('node:path')
const root = path.resolve(__dirname, '..')
const env = fs.readFileSync(path.join(root, '.env'), 'utf8')
const images = {
  white: Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAgAAAAICAIAAABLbSncAAAAFUlEQVR4nGP8//8/AzbAhFV00EoAAFbUAw037MyjAAAAAElFTkSuQmCC', 'base64'),
  red: Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAgAAAAICAIAAABLbSncAAAAEklEQVR4nGP8z4AdMOEQH6QSAM1BAQ/oQeJvAAAAAElFTkSuQmCC', 'base64'),
}
async function login(page, role) {
  const password = env.match(new RegExp(`^DEMO_${role}_PASSWORD=(.*)$`, 'm'))?.[1].trim()
  if (!password) throw Error('Demo password not configured')
  await page.locator('input[autocomplete="username"]').fill(`${role.toLowerCase()}.demo`)
  await page.locator('input[autocomplete="current-password"]').fill(password)
  await page.getByRole('button', { name: '进入工作台' }).click()
  await page.getByRole('heading', { name: '质量任务总览' }).waitFor()
}
async function main() {
  const browser = await chromium.launch({ headless: true,
    executablePath: 'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe' })
  try {
    const page = await browser.newPage({ viewport: { width: 1440, height: 980 } })
    const errors = []
    page.on('pageerror', error => errors.push(error.message))
    await page.goto('http://127.0.0.1:5173/')
    await login(page, 'ENGINEER')
    await page.getByRole('button', { name: '视觉样本库' }).click()
    await page.getByRole('heading', { name: '视觉样本库' }).waitFor()
    await page.getByRole('textbox', { name: '新样本产品代码', exact: true }).fill('QA-SAMPLE-CATEGORY')
    await page.getByRole('button', { name: '使用此类别', exact: true }).click()
    await page.getByText('该产品暂无样本。先整理人工确认的正常图像，再补充验证集和测试集。', { exact: true }).waitFor()
    await page.getByRole('combobox', { name: '样本产品' }).selectOption('VALVE-A')
    const form = page.locator('.sample-form')
    await form.getByLabel('样本来源').fill('合成 UI 测试图像，不用于模型训练')
    await form.getByLabel('备注').fill('8x8 纯色图，仅验证文件、人工标签和划分的保存。')
    await form.getByRole('checkbox').check()
    for (const color of ['white', 'red']) {
      if (color === 'red') {
        await form.getByRole('combobox', { name: '样本人工标签', exact: true }).selectOption('ABNORMAL')
        assert.equal(await form.getByRole('combobox', { name: '样本数据划分', exact: true }).inputValue(), 'VALIDATION')
        await page.waitForFunction(() => document.querySelector('.sample-form option[value="TRAIN"]')?.disabled === true)
      }
      await form.getByLabel('图像文件').setInputFiles({ name: `${color}.png`, mimeType: 'image/png', buffer: images[color] })
      const response = page.waitForResponse(r => r.url().endsWith('/visual-samples') && r.request().method() === 'POST')
      await form.getByRole('button', { name: '导入样本', exact: true }).click()
      assert.equal((await response).status(), 201)
      await page.getByRole('status').waitFor()
      await form.getByRole('button', { name: '导入样本', exact: true }).waitFor()
    }
    await page.locator('.sample-columns tbody tr').first().getByRole('button', { name: '查看图像' }).click()
    await page.waitForFunction(() => document.querySelector('.media-preview img')?.naturalWidth === 8)
    const downloadPromise = page.waitForEvent('download')
    await page.getByRole('button', { name: '导出样本清单' }).click()
    const download = await downloadPromise
    const manifest = JSON.parse(fs.readFileSync(await download.path(), 'utf8'))
    assert.equal(manifest.product_code, 'VALVE-A')
    assert(manifest.items.length >= 2 && manifest.manifest_sha256.length === 64)
    assert(manifest.items.every(item => item.image_path.startsWith('/api/v1/visual-samples/')))
    await page.screenshot({ path: path.join(root, 'design', 'preview-sample-library.png'), fullPage: true })
    await page.setViewportSize({ width: 390, height: 844 })
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false)
    await page.screenshot({ path: path.join(root, 'design', 'preview-sample-library-mobile.png'), fullPage: true })
    await page.setViewportSize({ width: 1440, height: 980 })
    await page.getByRole('button', { name: '退出登录' }).click()
    await login(page, 'INSPECTOR')
    await page.getByRole('button', { name: '视觉样本库' }).click()
    await page.getByRole('heading', { name: '视觉样本库' }).waitFor()
    assert.equal(await page.locator('.sample-form').count(), 0)
    await page.getByRole('button', { name: '新建检测' }).click()
    await page.locator('select').first().selectOption('BA-2026-1001')
    await page.locator('input[type="file"]').setInputFiles({ name: 'normal.png', mimeType: 'image/png', buffer: images.white })
    await page.getByRole('button', { name: '提交检测任务' }).click()
    await page.locator('.detail-summary .badge').filter({ hasText: '检测完成' }).waitFor({ timeout: 30000 })
    await page.getByText('模拟检测 · 未分析图像像素', { exact: true }).waitFor()
    assert.equal(await page.getByRole('button', { name: '查看异常定位图' }).count(), 0)
    await page.getByRole('button', { name: '查看检测原图' }).click()
    await page.waitForFunction(() => document.querySelector('.media-preview img')?.naturalWidth === 8)
    await page.screenshot({ path: path.join(root, 'design', 'preview-detection-interface.png'), fullPage: true })
    assert.deepEqual(errors, [])
    console.log('Sample UI check passed: labeled import, split guard, preview, manifest export, read-only inspector, mobile layout and detector metadata/original image.')
  } finally { await browser.close() }
}
main().catch(error => { console.error(error); process.exit(1) })
