// Exercise the shipped renderer with repeatable long messages and multiple plan revisions.
import assert from 'node:assert/strict'
import { mkdir, mkdtemp, realpath, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { dirname, join, resolve } from 'node:path'
import { chromium, _electron as electron } from 'playwright'
import { LocalStudio } from './studio.mjs'

const [target, output, ...flags] = process.argv.slice(2)
if (!target || !output) throw new Error('Usage: node verify-chat.mjs EXECUTABLE OUTPUT_DIR [--browser] [--before]')
const exe = await realpath(target)
const root = await mkdtemp(join(tmpdir(), 'office-chat-'))
const installPython = join(dirname(exe), 'resources', 'runtime', process.platform === 'win32' ? 'python.exe' : 'bin/python3')
const studio = new LocalStudio(join(root, 'studio'), process.env.OFFICE_VERIFY_PYTHON || installPython)
const workspace = { id: 'a'.repeat(32), name: '작업실 개선', folders: ['/sample'] }
const id = 'b'.repeat(32)
const time = '2026-11-20T04:30:00Z'
const roles = { developer: {name:'서진',engine:'codex',skills:[]}, reviewer:{name:'정희',engine:'codex',skills:[]} }
const assignment = { roles, note:'서진에게 알림 설정 작업을 맡겼습니다.', delegation:false, meeting:true }
const plan = {
  outcome:'저장한 알림 설정을 복원한다.', scope:['설정 화면'], rationale:['저장된 사용자 선택을 적용한다.'],
  tasks:[{id:'T1',owner:'developer',description:'설정 읽기',depends_on:[]},
    {id:'T2',owner:'developer',description:'알림 표시',depends_on:[]},
    {id:'T3',owner:'reviewer',description:'통합 검증',depends_on:['T1','T2']}],
  criteria:[{criterion:'다시 열면 설정이 유지된다.',verification:'회귀 테스트'}], questions:[],
}
const meeting = {run_id:id, status:'approved',revision:2,plan, answers:{},choices:[],work:[],
  contributions:[{role:'developer',engine:'codex',text:'저장 시점을 확인했습니다.',created_at:time,revision:1}],
  verification:[{criterion:'알림 배치',status:'not_verified',evidence:'운영자 화면 확인 필요'}],
  revisions:[1,2].map(revision=>({revision,plan:{...plan,outcome:revision===1?'알림 설정을 저장한다.':plan.outcome},
    source:revision===1?'coordinator':'direction',reason:revision===1?'첫 계획':'재접속 확인 추가',verification:[],answers:{}}))}
const code = Array.from({length:24}, (_, i) => `    options["setting_${i}"] = defaults[${i}]`).join('\n')
const run = {id,workspace_id:workspace.id,task:'사무실을 다시 열었을 때 알림 설정을 복원해 주세요.',
  state:'READY_FOR_HUMAN_REVIEW',outcome:'APPROVED',attempts:1,created_at:time,updated_at:'2026-11-20T04:40:00Z',
  allow_edits:true,use_inbox:true,developer_engine:'codex',reviewer_engine:'codex',assignment,
  lines:{developer:'작업을 마쳤습니다.',reviewer:'리뷰를 마쳤습니다.'},pending_count:0,meeting,
  developer_output:'알림 설정을 저장하고 복원하도록 수정했습니다.\n\n## 변경 근거\n\n- 기존 설정 파일을 재사용했습니다.\n- `enabled` 값을 확인했습니다.\n\n```python\ndef restore(settings):\n'+code+'\n```\n\n복원 시나리오를 검증했습니다.',
  test_output:'Studio test invocation:\n{"cwd":"/sample","argv":["python","-m","pytest","-q"],"exit_code":0}\nCaptured test output:\n... [100%]\n3 passed in 0.34s',
  review_output:'VERDICT: APPROVED\n\n설정 복원과 테스트를 확인했습니다.',changes:' M settings.py\n M tests/test_settings.py',
  diff:'diff --git a/settings.py b/settings.py\n--- a/settings.py\n+++ b/settings.py\n@@ -1 +1 @@\n-enabled = False\n+enabled = settings.enabled\ndiff --git a/tests/test_settings.py b/tests/test_settings.py\n--- a/tests/test_settings.py\n+++ b/tests/test_settings.py\n@@ -1 +1 @@\n-assert not enabled\n+assert enabled\n'}
const paths = new Map([
  ['/api/hosts',{hosts:[{id:'local',name:'이 컴퓨터'}]}], ['/api/workspaces',{workspaces:[workspace]}],
  ['/api/rooms/'+workspace.id,{workspace,meeting_supported:true,settings:{...roles,delegation:false},engines:[],runs:[run]}],
  ['/api/runs/'+id,{run}], ['/api/runs/'+id+'/directions',{directions:[]}],
])
let application, browser
const runtime = await studio.start()
try {
  let page
  if (flags.includes('--browser')) {
    browser = await chromium.launch({channel:'chrome',chromiumSandbox:true})
    page = await browser.newPage()
  } else {
    const profile = join(root,'profile')
    await mkdir(profile)
    await writeFile(join(profile,'connection.json'), JSON.stringify({recent:{},remotes:[],
      connection:{mode:'attached',origin:runtime.origin,journalId:runtime.journalId}}))
    application = await electron.launch({executablePath:exe,args:['--user-data-dir='+profile],chromiumSandbox:true})
    page = await application.firstWindow()
    await page.locator('#register').waitFor({timeout:15000})
  }
  page.setDefaultTimeout(10000)
  const violations = []
  const errors = []
  const externalRequests = []
  const chartRequests = []
  page.on('pageerror', error => errors.push(error.message))
  await page.exposeFunction('reportPolicyViolation', text => violations.push(text))
  await page.addInitScript(() => document.addEventListener('securitypolicyviolation', event => {
    window.reportPolicyViolation(event.violatedDirective + ' ' + event.blockedURI)
  }))
  page.on('request', request => {
    const url = new URL(request.url())
    if (['http:', 'https:'].includes(url.protocol) && url.origin !== runtime.origin) externalRequests.push(url.origin)
    if (url.pathname === '/mermaid-flow-renderer.js') chartRequests.push(url.pathname)
  })
  await mkdir(resolve(output), {recursive:true})
  await page.route('**/api/**', route => {
    const path = new URL(route.request().url()).pathname
    const data = paths.get(path) ?? (path.endsWith('/inbox') ? {requests:[]} : null)
    return data ? route.fulfill({json:data}) : route.continue()
  })
  for (const width of [1280,390]) {
    await page.setViewportSize({width,height:800})
    await page.goto(`${runtime.origin}/?workspace=${workspace.id}&run=${id}`)
    await page.locator('.seat[data-person="developer"]').click()
    await page.locator('#chat-log').getByText(/알림 설정을 저장하고 복원하도록/).first().waitFor()
    const dialog = page.locator('#conversation')
    assert.ok(await dialog.isVisible())
    const stage = flags.includes('--before') ? 'before' : 'after'
    await page.screenshot({path:join(output,`${stage}-${width}.png`)})
    if (flags.includes('--before')) continue
    assert.equal(await dialog.locator('[data-view="simple"]').getAttribute('aria-pressed'), 'true')
    assert.equal(await dialog.getByText('options["setting_18"]', {exact:false}).first().isVisible(), false)
    await dialog.locator('.result-facts').filter({hasText:'변경 파일 2개'}).waitFor()
    if (width === 1280) assert.equal(chartRequests.length, 0)
    await dialog.locator('[data-jump="changes"]').focus()
    await page.keyboard.press('Enter')
    const diff = dialog.locator('#evidence-changes .office-diff')
    await diff.getByText('+enabled = settings.enabled', {exact:false}).waitFor()
    const other = diff.locator('summary').filter({hasText:'tests/test_settings.py'})
    await other.focus()
    await page.keyboard.press('Enter')
    await diff.getByText('+assert enabled', {exact:false}).waitFor()
    const wrap = other.locator('..').getByRole('button',{name:'줄바꿈'})
    await wrap.click()
    assert.equal(await wrap.getAttribute('aria-pressed'), 'true')
    await page.screenshot({path:join(output,`diff-${width}.png`)})
    await dialog.locator('[data-back]').click()
    assert.equal(await dialog.locator('[data-view="simple"]').getAttribute('aria-pressed'), 'true')
    await dialog.locator('[data-jump="tests"]').click()
    await dialog.locator('#evidence-tests pre').filter({hasText:'3 passed in 0.34s'}).waitFor()
    await dialog.locator('[data-back]').click()
    await dialog.locator('[data-view="technical"]').click()
    const outputPanel = dialog.locator('#evidence-developer')
    await outputPanel.locator(':scope > summary').click()
    await outputPanel.getByRole('heading',{name:'변경 근거'}).waitFor()
    assert.equal(await outputPanel.locator('li code').first().textContent(), 'enabled')
    const source = outputPanel.locator('summary').filter({hasText:'원문 보기'})
    await source.click()
    assert.equal(await source.locator('..').locator('pre').textContent(), run.developer_output)
    await page.reload()
    await page.locator('.seat[data-person="developer"]').click()
    assert.equal(await dialog.locator('[data-view="technical"]').getAttribute('aria-pressed'), 'true')
    await dialog.locator('[data-view="simple"]').click()
    await dialog.locator('[data-jump="meeting"]').click()
    const before = chartRequests.length
    const oldRevision = dialog.locator('details[data-revision="1"]')
    await oldRevision.locator(':scope > summary').click()
    const flow = oldRevision.locator('.plan-flow')
    await flow.getByRole('button',{name:'작업 흐름 보기'}).click()
    await flow.getByRole('img',{name:/계획 1/}).waitFor()
    assert.ok(before > 0 || chartRequests.length > 0)
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth), true)
    await page.screenshot({path:join(output,`flow-${width}.png`)})
    await dialog.locator('[data-back]').click()
    await dialog.getByRole('button',{name:'대화 닫기'}).click()
  }
  if (!flags.includes('--before')) {
    await page.route('**/mermaid-flow-renderer.js', route => route.abort())
    await page.reload()
    await page.locator('.seat[data-person="developer"]').click()
    const dialog = page.locator('#conversation')
    await dialog.locator('[data-jump="meeting"]').click()
    const flow = dialog.locator('.plan-flow').first()
    await flow.getByRole('button',{name:'작업 흐름 보기'}).click()
    await flow.getByRole('status').filter({hasText:'도식을 표시할 수 없습니다'}).waitFor()
    assert.equal(await flow.locator('li').count(), 3)
    await dialog.locator('[data-back]').click()
    await dialog.getByRole('button',{name:'대화 닫기'}).click()
    Object.assign(run, {state:'FAILED',outcome:'TEST_FAILED',meeting:null,review_output:'',
      test_output:'Studio test invocation:\n{"exit_code":1}\nCaptured test output:\nFAILED tests/test_settings.py::test_restore - AssertionError\n1 failed in 0.25s'})
    await page.reload()
    await page.locator('.seat[data-person="developer"]').click()
    await dialog.locator('#chat-log').getByText(/test_restore/).first().waitFor()
    await dialog.locator('[data-jump="tests"]').click()
    await dialog.locator('#evidence-tests pre').filter({hasText:'1 failed in 0.25s'}).waitFor()
    await dialog.locator('[data-back]').click()
    await dialog.getByRole('button',{name:'대화 닫기'}).click()
    Object.assign(run, {state:'RUNNING',outcome:null,developer_output:'',test_output:'',changes:'',diff:'',pending_count:1})
    const pending = {requests:[{
      id: 'f'.repeat(32), run_id:id, role:'developer',engine:'codex',attempt:1,kind:'permission',status:'pending',
      created_at:time,prompt:'settings/'+ 'directory/'.repeat(85) + 'config.json',
    }]}
    paths.set('/api/runs/'+id+'/inbox', pending)
    paths.set('/api/rooms/'+workspace.id+'/inbox', pending)
    await page.reload()
    await page.locator('.seat[data-person="developer"]').click()
    for (const view of ['technical','collaboration','simple']) {
      await dialog.locator(`[data-view="${view}"]`).click()
      const question = dialog.locator('.request-item')
      await question.getByText(/config\.json/).waitFor()
      assert.ok(await question.getByRole('button', {name:'요청함에서 확인'}).isEnabled())
      assert.ok(await question.evaluate(el => el.getBoundingClientRect().width <= window.innerWidth))
      assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth))
    }
    await dialog.getByRole('button', {name:'요청함에서 확인'}).click()
    const inbox = page.locator('#requests')
    await inbox.getByRole('button', {name:'이번 요청만 승인'}).waitFor()
    assert.ok(await inbox.getByRole('button',{name:'거절'}).isEnabled())
    assert.deepEqual(errors, [])
    assert.deepEqual(violations, [])
    assert.deepEqual(externalRequests, [])
  }
  console.log('Verified conversation rendering in '+resolve(output))
} finally {
  if (application) await application.close()
  if (browser) await browser.close()
  await studio.stop(runtime.bootId)
}
