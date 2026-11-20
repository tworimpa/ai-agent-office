import assert from 'node:assert/strict'
import {execFileSync, spawnSync} from 'node:child_process'
import {mkdir, mkdtemp, realpath, writeFile} from 'node:fs/promises'
import {tmpdir} from 'node:os'
import {delimiter, dirname, join, resolve} from 'node:path'
import {setTimeout as delay} from 'node:timers/promises'
import {fileURLToPath} from 'node:url'
import {_electron as electron} from 'playwright'
import {LocalStudio} from './studio.mjs'

const [target, output, mode] = process.argv.slice(2)
if (!target || !output || !['before', 'after', 'privilege-gate'].includes(mode))
  throw Error('Usage: node verify-goal.mjs INSTALLED_EXECUTABLE EVIDENCE_DIRECTORY before|after|privilege-gate')
const executable = await realpath(target)
const started = Date.now()
const windows = process.platform === 'win32'
const root = await mkdtemp(join(tmpdir(), 'office-settings-'))
const profile = join(root, 'profile'),
  repo = join(root, 'project'),
  bin = join(root, 'bin')
const python = join(dirname(executable), 'resources', 'runtime', windows ? 'python.exe' : 'bin/python3')
const studio = new LocalStudio(join(profile, 'local-studio'), python)
const request =
  'Windows에서 설정이 사라지는 문제를 재현하고 수정한 뒤, 관련 테스트와 전체 회귀 검증을 실행해 아침에 검토할 후보를 남겨줘'
const events = []
const record = (action, detail) => events.push({elapsed_seconds: (Date.now() - started) / 1000, action, detail})
const evidence = resolve(output)
await mkdir(evidence, {recursive: true})
await mkdir(bin)
await mkdir(repo)
await mkdir(join(repo, 'tests'))
await writeFile(join(repo, '.gitignore'), '__pycache__/\n.pytest_cache/\n')
await writeFile(join(repo, 'settings.py'), 'import json\n\ndef restore(path):\n    return "light"\n')
await writeFile(
  join(repo, 'tests', 'test_settings.py'),
  'from settings import restore\n\ndef test_reopen_saved_theme(tmp_path):\n    path = tmp_path / "settings.json"\n    path.write_text(\'{"theme":"dark"}\', encoding="utf8")\n    assert restore(path) == "dark"\n'
)
await writeFile(
  join(repo, 'tests', 'test_defaults.py'),
  'from settings import restore\n\ndef test_first_start_uses_default(tmp_path):\n    assert restore(tmp_path / "missing") == "light"\n'
)
function git(...args) {
  return execFileSync('git', args, {cwd: repo, encoding: 'utf8'})
}
git('init', '-q')
git('add', '.')
git('-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid', 'commit', '-qm', 'settings fixture')
const source = fileURLToPath(new URL('fixtures/goal_worker.py', import.meta.url))
const shquote = (value) => "'" + value.replaceAll("'", "'\"'\"'") + "'"
for (const engine of ['codex', 'agy']) {
  const script = windows
    ? '@"' + python + '" "' + source + '" ' + engine + ' %*\r\n'
    : '#!/bin/sh\nexec ' + shquote(python) + ' ' + shquote(source) + ' ' + engine + ' "$@"\n'
  await writeFile(join(bin, engine + (windows ? '.cmd' : '')), script, {mode: 0o700})
}
const pathKey = Object.keys(process.env).find((key) => key.toUpperCase() === 'PATH') ?? 'PATH'
const env = {...process.env, [pathKey]: bin + delimiter + process.env[pathKey]}
let app, page, run, origin
async function launch() {
  app = await electron.launch({
    executablePath: executable,
    args: ['--user-data-dir=' + profile],
    env,
    chromiumSandbox: true,
    timeout: 30000
  })
  page = await app.firstWindow()
  page.setDefaultTimeout(25000)
  await page.setViewportSize({width: 1280, height: 900})
}
async function close() {
  const child = app.process()
  await Promise.race([
    app.close(),
    (async () => {
      for (let i = 0; i < 200; i++) {
        if (child.exitCode !== null || child.signalCode !== null) return
        await delay(75)
      }
      throw Error('Fixture desktop did not close')
    })()
  ])
  app = null
}
async function screenshot(name) {
  await page.screenshot({path: join(evidence, name + '.png')})
}
async function json(path) {
  const response = await fetch(origin + path, {signal: AbortSignal.timeout(8000)})
  assert.ok(response.ok, path + ' ' + response.status)
  return response.json()
}
async function manualFailure(cwd, args, test, file, detail) {
  const result = spawnSync(python, ['-m', 'pytest', '-q', ...args], {cwd, encoding: 'utf8', windowsHide: true})
  assert.equal(result.status, 1, result.stdout + result.stderr)
  assert.ok(result.stdout.includes(test), result.stdout)
  await writeFile(join(evidence, file), result.stdout)
  record('verification', detail)
}
async function decide() {
  await page.locator('#summary-action').filter({hasText: '계획 확인하기'}).click()
  const radio = page.locator('#meeting-decision').getByRole('radio', {name: '밝게', exact: true})
  await radio.waitFor()
  await screenshot('02-product-choice')
  await radio.check()
  await page.locator('#approve-plan').click()
  record('decision', '기본 테마를 밝게로 선택하고 계획 시작')
}
async function waitForQuestion(id) {
  for (const deadline = Date.now() + 120000; Date.now() < deadline; ) {
    const {requests} = await json('/api/runs/' + id + '/inbox')
    if (requests.some((row) => row.kind === 'question' && row.status === 'pending')) return
    const {goal} = await json('/api/runs/' + id + '/goal')
    if (goal?.recovery?.disposition === 'closed') {
      throw Error('Goal stopped before its product decision: ' + (goal.stop_reason || goal.state))
    }
    await delay(300)
  }
  throw Error('The fixture did not reach its product decision within two minutes')
}
async function verifyRestoredReport(captureOverview = false) {
  await page.locator('#goal-report').waitFor()
  await page
    .locator('[data-goal-section="baseline"] > details > summary')
    .filter({hasText: /기준 커밋.*실패/})
    .waitFor()
  if (captureOverview) {
    await page.locator('#goal-report').evaluate((el) => el.scrollIntoView({block: 'start'}))
    await screenshot('03a-morning-overview')
    await page.setViewportSize({width: 390, height: 840})
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true)
    await screenshot('03b-morning-mobile')
    await page.setViewportSize({width: 1280, height: 900})
  }
  for (const attempt of [1, 2]) {
    const item = page.locator('[data-goal-attempt="' + attempt + '"]')
    if (!(await item.evaluate((el) => el.open))) await item.locator(':scope > summary').click()
    await item.locator('[data-goal-diff]').click()
    await item.locator(':scope > pre').filter({hasText: 'settings.py'}).waitFor()
  }
  const compare = page.locator('[data-goal-compare="1-2"]')
  await compare.click()
  await page
    .locator('[data-goal-attempt="2"] pre')
    .filter({hasText: /두 후보의 파일 변경|각 후보의 패치 비교/})
    .filter({hasText: 'path.exists()'})
    .waitFor()
  await page
    .locator('[data-goal-section="verification"] summary')
    .filter({hasText: /2차 시도.*전체 회귀.*통과.*2개/})
    .waitFor()
  await page
    .locator('[data-goal-section="manual"]')
    .getByText(/실제 사용자 여정/)
    .waitFor()
  assert.equal(new URL(page.url()).searchParams.get('run'), run.id)
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true)
}
try {
  await launch()
  const opened = app.waitForEvent('window')
  await page.getByRole('button', {name: '내 작업실 시작'}).click()
  page = await opened
  page.setDefaultTimeout(25000)
  await page.setViewportSize({width: 1280, height: 900})
  await page.locator('#empty-register').click()
  await page.locator('#workspace-paths').fill(repo)
  await page.locator('#workspace-name').fill('설정 복원 검증')
  await page.locator('#save-workspace').click()
  await page.getByRole('heading', {name: '설정 복원 검증', exact: true}).waitFor()
  origin = new URL(page.url()).origin
  let engines = []
  for (const deadline = Date.now() + 30000; Date.now() < deadline; ) {
    engines = (await json('/api/engines')).engines
    if (
      engines.find((row) => row.engine === 'antigravity')?.version === '1.0.0-fixture' &&
      engines.find((row) => row.engine === 'codex')?.version
    )
      break
    await delay(200)
  }
  for (const [id, version] of [
    ['codex', '0.169.1'],
    ['antigravity', '1.0.0-fixture']
  ])
    assert.equal(engines.find((item) => item.engine === id)?.version, version, 'Local fixture ' + id)
  if (mode !== 'before') {
    await page.locator('#settings').click()
    await page.locator('#goal-policy > summary').click()
    const first = page.locator('#goal-check-fields fieldset').first()
    await first.locator('[name="argv"]').fill('python\n-m\npytest\n-q\ntests/test_settings.py')
    await page.locator('#goal-save-policy').click()
    await page.locator('#goal-policy-note').filter({hasText: '저장했습니다'}).waitFor()
    record('workspace_setup', '프로젝트 기본값으로 관련 테스트를 한 번 지정')
    await page.getByRole('button', {name: '설정 닫기'}).click()
  }
  await page.locator('.seat[data-person="coordinator"]').click()
  await page.locator('#message').fill(request)
  await page.locator('#allow-edits').check()
  if (mode === 'before') {
    await page.locator('#assignment-options summary').click()
    await page.locator('#test-files').fill('tests/test_settings.py')
    record('scope', '운영자가 관련 테스트 파일을 지정')
  } else {
    assert.equal(await page.locator('#overnight-goal').isChecked(), true, 'Saved verification settings must default edit assignments to a goal')
  }
  await screenshot('01-intake')
  const accepted = page.waitForResponse((r) => r.url().endsWith('/assignments') && r.request().method() === 'POST')
  await page.locator('#send').click()
  const receipt = await accepted
  assert.equal(receipt.status(), 202, await receipt.text())
  run = (await receipt.json()).run
  record('assignment', run.id)
  await page.getByRole('button', {name: '대화 닫기'}).click()
  if (mode === 'before') {
    await decide()
    let detail
    for (const deadline = Date.now() + 90000; Date.now() < deadline; ) {
      detail = (await json('/api/runs/' + run.id)).run
      if (!['QUEUED', 'RUNNING', 'TESTING', 'REVIEWING', 'WAITING_USER'].includes(detail.state)) break
      await delay(300)
    }
    assert.equal(
      detail.state,
      'READY_FOR_HUMAN_REVIEW',
      JSON.stringify({state: detail.state, result: detail.review_output, tests: detail.test_output})
    )
    await page.locator('#refresh').click()
    await page.locator('.seat[data-person="developer"]').click()
    await page.locator('[data-jump="tests"]').last().click()
    await page
      .locator('#evidence-tests')
      .getByText(/1 passed/)
      .last()
      .waitFor()
    await screenshot('03-related-only')
    record('read', '관련 테스트 통과를 확인')
    await manualFailure(
      repo,
      ['tests/test_settings.py'],
      'test_reopen_saved_theme',
      'original-failure.txt',
      '운영자가 기준 커밋에서 원래 테스트 실패를 별도로 재현'
    )
    await manualFailure(
      detail.workspace,
      [],
      'test_first_start_uses_default',
      'full-regression.txt',
      '운영자가 후보에서 전체 회귀를 별도로 실행하여 첫 실행 실패를 발견'
    )
  } else if (mode === 'privilege-gate') {
    let goal
    for (const deadline = Date.now() + 60000; Date.now() < deadline; ) {
      goal = (await json('/api/runs/' + run.id + '/goal')).goal
      if (goal.recovery?.disposition === 'closed') break
      await delay(250)
    }
    assert.equal(goal.checks[0]?.status, 'unverified')
    assert.match(goal.checks[0].summary, /일반 사용자/)
    assert.equal(goal.candidates.length, 0)
    assert.equal(goal.best_attempt, null)
    await writeFile(join(evidence, 'goal.json'), JSON.stringify(goal, null, 2))
    record('privilege-gate', '관리자 세션의 저장소 테스트를 실행하지 않고 미검증으로 기록')
  } else {
    await waitForQuestion(run.id)
    await page.locator('#summary-action').filter({hasText: '질문에 답하기'}).click()
    const editor = page.locator('#office-thread .request-item textarea')
    await editor.fill('밝게')
    await screenshot('02-product-choice')
    await page.locator('#office-thread').getByRole('button', {name: '답변 보내기'}).click()
    record('decision', '기본 테마를 밝게로 선택')
    let goal
    for (const deadline = Date.now() + 180000; Date.now() < deadline; ) {
      goal = (await json('/api/runs/' + run.id + '/goal')).goal
      if (goal.recovery?.disposition === 'closed') break
      await delay(500)
    }
    await writeFile(join(evidence, 'goal.json'), JSON.stringify(goal, null, 2))
    assert.equal(goal.state, 'READY_FOR_HUMAN_REVIEW', JSON.stringify(goal).slice(-4500))
    assert.ok(
      goal.checks.some(
        (row) => row.attempt === 0 && row.status === 'failed' && row.summary.includes('test_reopen_saved_theme')
      )
    )
    assert.equal(goal.candidates.length, 2)
    assert.equal(goal.best_attempt, 2)
    assert.equal(goal.candidates[0].status, 'failed')
    assert.equal(goal.candidates[1].status, 'passed')
    const decisions = (await json('/api/runs/' + run.id + '/inbox')).requests
    assert.equal(decisions.length, 1)
    assert.equal(decisions[0].answer, '밝게')
    assert.ok(decisions[0].delivered_at)
    const original = await studio.running()
    await close()
    await launch()
    await verifyRestoredReport(true)
    assert.equal((await studio.running()).bootId, original.bootId)
    await screenshot('03-reopened-report')
    record('read', '앱을 다시 열어 기준 실패, 두 시도의 변경, 최선 후보, 검증 범위와 수동 확인을 열람')
    await close()
    await studio.stop(original.bootId)
    await launch()
    await verifyRestoredReport()
    const restarted = await studio.running()
    assert.notEqual(restarted.bootId, original.bootId)
    origin = restarted.origin
    const restored = (await json('/api/runs/' + run.id + '/goal')).goal
    assert.deepEqual(restored.candidates, goal.candidates)
    assert.deepEqual(restored.checks, goal.checks)
    assert.deepEqual(restored.events, goal.events)
    await screenshot('04-runner-reopened')
    record('read', 'Studio 재시작 후 같은 목표의 검증·후보·이벤트 일치')
    await page.locator('[data-goal-adopt="2"]').click()
    await page.locator('[data-goal-attempt="2"] summary').filter({hasText: '채택됨'}).waitFor()
    const adopted = (await json('/api/runs/' + run.id + '/goal')).goal
    assert.equal(adopted.adopted_digest, goal.candidates[1].digest)
    assert.equal(adopted.merge_status, 'not_recorded')
    assert.equal(adopted.deployment_status, 'not_recorded')
    record('adopt', '검토한 2차 후보의 채택을 기록')
  }
  await writeFile(
    join(evidence, 'result.json'),
    JSON.stringify({platform: process.platform, executable, root, mode, run_id: run.id, request, events}, null, 2)
  )
  console.log('Installed journey verified', evidence)
} catch (error) {
  console.error('Fixture evidence:', evidence, 'working directory:', root)
  if (origin && run) {
    try {
      const {run: detail} = await json('/api/runs/' + run.id)
      const {goal} = await json('/api/runs/' + run.id + '/goal')
      const diagnostic = {
        run_id: run.id,
        state: detail.state,
        outcome: detail.outcome,
        diagnostic: detail.diagnostic,
        developer: detail.developer_output?.slice(-1200),
        test: detail.test_output?.slice(-1200),
        stop_reason: goal?.stop_reason,
        checks: goal?.checks.map(({attempt, stage, status, exit_code, summary}) => ({
          attempt,
          stage,
          status,
          exit_code,
          summary: summary.slice(-1200)
        })),
        pending: goal?.recovery?.pending_decisions,
        next_action: goal?.next_action
      }
      console.error('Installed goal diagnostic:', JSON.stringify(diagnostic))
      await writeFile(join(evidence, 'diagnostic.json'), JSON.stringify(diagnostic, null, 2))
    } catch (readError) {
      console.error('Could not read fixture diagnostic:', readError.message)
    }
  }
  if (page && !page.isClosed()) await screenshot('error').catch(() => {})
  throw error
} finally {
  if (app) await close()
  const runtime = await studio.running()
  if (runtime) await studio.stop(runtime.bootId)
}
