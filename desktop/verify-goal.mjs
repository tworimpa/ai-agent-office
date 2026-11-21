import assert from 'node:assert/strict'
import {execFileSync, spawnSync} from 'node:child_process'
import {mkdir, mkdtemp, realpath, writeFile} from 'node:fs/promises'
import {tmpdir} from 'node:os'
import {delimiter, dirname, join, resolve} from 'node:path'
import {setTimeout as delay} from 'node:timers/promises'
import {fileURLToPath} from 'node:url'
import {_electron as electron} from 'playwright'
import {LocalStudio} from './studio.mjs'
import {closeDesktop, finishVerification} from './verify-lifecycle.mjs'

const [target, output, mode] = process.argv.slice(2)
const briefingMode = ['briefing-before', 'briefing-after'].includes(mode)
const githubMode = ['github', 'github-gate'].includes(mode) || briefingMode
if (!target || !output || !['before', 'after', 'privilege-gate', 'github', 'github-gate'].includes(mode) && !briefingMode)
  throw Error('Usage: node verify-goal.mjs INSTALLED_EXECUTABLE EVIDENCE_DIRECTORY before|after|privilege-gate|github|github-gate|briefing-before|briefing-after')
const executable = await realpath(target)
const started = Date.now()
const windows = process.platform === 'win32'
const root = await mkdtemp(join(tmpdir(), 'office-settings-'))
const profile = join(root, 'profile'),
  repo = join(root, 'project'),
  bin = join(root, 'bin')
const python = join(dirname(executable), 'resources', 'runtime', windows ? 'python.exe' : 'bin/python3')
const studio = new LocalStudio(join(profile, 'local-studio'), python)
const request = briefingMode ? '저장 설정 형식 변경' :
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
git('init', '-q', '-b', 'main')
if (briefingMode) {
  await writeFile(join(repo, 'briefing-fixture.txt'), 'Disposable workspace briefing fixture\n')
  await writeFile(join(repo, 'settings.py'), 'def valid(data):\n    return True\n\ndef convert(data):\n    return data\n')
  await writeFile(join(repo, 'tests/test_settings.py'), 'from settings import valid\n\ndef test_valid_setting():\n    assert valid({"value": "dark"})\n')
  await writeFile(join(repo, 'tests/test_defaults.py'), 'from settings import convert\n\ndef test_current_setting():\n    assert convert({"value": "dark"}) == {"value": "dark"}\n')
} else if (githubMode) await writeFile(join(repo, 'github-journey.txt'), 'GitHub Issue integration fixture\n')
git('add', '.')
git('-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid', 'commit', '-qm', 'settings fixture')
const source = fileURLToPath(new URL('fixtures/goal_worker.py', import.meta.url))
const shquote = (value) => "'" + value.replaceAll("'", "'\"'\"'") + "'"
const traceDir = join(root, 'fixture-processes')
if (briefingMode) await mkdir(traceDir)
for (const engine of ['codex', 'agy']) {
  const trace = briefingMode ? ' --fixture-trace ' + (windows ? '"' + traceDir + '"' : shquote(traceDir)) : ''
  const script = windows
    ? '@"' + python + '" "' + source + '" ' + engine + trace + ' %*\r\n'
    : '#!/bin/sh\nexec ' + shquote(python) + ' ' + shquote(source) + ' ' + engine + trace + ' "$@"\n'
  await writeFile(join(bin, engine + (windows ? '.cmd' : '')), script, {mode: 0o700})
}
const pathKey = Object.keys(process.env).find((key) => key.toUpperCase() === 'PATH') ?? 'PATH'
const env = {...process.env, [pathKey]: bin + delimiter + process.env[pathKey]}
let provider
if (githubMode) {
  const {githubFixture} = await import('./fixtures/github-provider.mjs')
  provider = await githubFixture(root, python, repo, request, briefingMode ? 41 : 7)
  if (briefingMode) provider.issue.body = '## 완료 기준\n- 기존 설정을 새 형식으로 읽는 방식을 제품 결정에 따라 구현하세요.'
  env.AGENT_OFFICE_GITHUB_CONFIG = provider.config
  git('remote', 'add', 'origin', provider.origin + '/fixture/settings.git')
}
let app, child, page, run, origin, failure
async function launch() {
  app = await electron.launch({
    executablePath: executable,
    args: ['--user-data-dir=' + profile],
    env,
    chromiumSandbox: true,
    timeout: 30000
  })
  child = app.process()
  page = await app.firstWindow()
  page.setDefaultTimeout(25000)
  await page.setViewportSize({width: 1280, height: 900})
}
async function close() {
  const launched = app
  app = null
  await closeDesktop(child, () => launched.close())
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
async function eventually(read, reason, seconds = 150) {
  for (const deadline = Date.now() + seconds * 1000; Date.now() < deadline; ) {
    const result = await read()
    if (result) return result
    await delay(400)
  }
  throw Error(reason + (provider?.errors.length ? ' · ' + provider.errors.join(', ') : ''))
}
async function githubJourney() {
  await page.locator('#settings').click()
  await page.locator('#github-settings > summary').click()
  await page.locator('#github-repo').selectOption('71')
  assert.equal(await page.locator('#github-branch').inputValue(), 'main')
  assert.equal(await page.locator('#github-draft').isChecked(), false)
  await page.locator('#github-test').click()
  await page.locator('#github-diagnostic').filter({hasText: '연결 확인됨'}).waitFor()
  assert.ok(!provider.calls.includes('POST /repos/fixture/settings/labels'), 'Testing a connection must not claim a repository')
  await page.locator('#github-settings > summary').evaluate(el => el.scrollIntoView({block: 'start'}))
  await screenshot('01a-github-diagnostic')
  await page.locator('#github-actors').fill('42')
  await page.locator('#github-answers').fill('42')
  await page.locator('#github-enabled').check()
  await page.locator('#github-save').click()
  await page.locator('#github-settings-note').filter({hasText: '저장했습니다'}).waitFor()
  assert.equal(provider.calls.filter(c => c === 'POST /repos/fixture/settings/labels').length, 1, 'Explicit intake opt-in registers one ownership label')
  await screenshot('01-github-connection')
  await page.getByRole('button', {name: '설정 닫기'}).click()
  const ws = new URL(page.url()).searchParams.get('workspace'), path = '/api/rooms/' + ws + '/github'
  const sync = async () => { await page.locator('#github-sync').click() }
  const state = () => json(path)
  const issue = async () => (await state()).issues.find(row => row.issue_id === 107)
  // Give the first empty scan a known completion before exposing the labelled issue.
  await sync()
  await eventually(async () => (await state()).status.last_sync, 'First manual synchronisation did not complete')
  const original = await studio.running()
  await close()
  await studio.stop(original.bootId)
  assert.equal(await studio.running(), null, 'The desktop host must be stopped before the issue is labelled')
  record('studio_stopped', {boot_id: original.bootId, journal_id: original.journalId})
  provider.feed()
  await launch()
  const working = await studio.running()
  assert.notEqual(working.bootId, original.bootId)
  origin = working.origin
  await page.locator('#room-title').filter({hasText: '설정 복원 검증'}).waitFor()
  record('studio_restarted', {boot_id: working.bootId, journal_id: working.journalId})
  await close()
  const linked = await eventually(async () => { const row = await issue(); return row?.run_id && row }, 'Background polling did not import the issue with the desktop closed')
  run = {id: linked.run_id}
  record('poll', 'Studio 종료 중 생긴 라벨과 닫힌 이슈를 재기동 후 자동 대조하여 열린 이슈 하나를 목표로 접수')
  await launch()
  origin = (await studio.running()).origin
  assert.equal((await studio.running()).bootId, working.bootId)
  const card = page.locator('[data-issue="107"]')
  await card.getByRole('button', {name: '연결된 목표 열기'}).click()
  await page.locator('[data-goal-section="github"]').getByText(/목표 ID/).waitFor()
  await page.locator('.seat[data-person=developer] .seat-task').filter({hasText: /설정/}).waitFor()
  await screenshot('02-github-issue-goal')
  await sync()
  if (mode === 'github') {
    const question = await eventually(async () => {
      const inbox = await json('/api/runs/' + run.id + '/inbox')
      return inbox.requests.find(row => row.kind === 'question' && row.status === 'pending')
    }, 'Technical question did not appear')
    await sync()
    await eventually(() => provider.comments.some(c => c.user.type === 'Bot' && c.body.includes(question.id)), 'GitHub status did not reference the pending question')
    provider.reply('/office answer ' + linked.goal_id + ' ' + question.id + ' 1')
    await sync()
    await eventually(async () => (await json('/api/runs/' + run.id + '/inbox')).requests.find(q => q.id === question.id)?.delivered_at,
      'The authorized GitHub reply did not reach the worker')
    record('answer', '이슈 댓글의 기술 답변이 해당 작업에 전달됨')
  }
  const goal = await eventually(async () => {
    const {goal} = await json('/api/runs/' + run.id + '/goal')
    return goal.recovery?.disposition === 'closed' && goal
  }, 'GitHub-linked run did not finish', 180)
  if (mode === 'github-gate') {
    assert.equal(goal.checks[0]?.status, 'unverified')
    assert.match(goal.checks[0]?.summary, /일반 사용자/)
    assert.equal(goal.candidates.length, 0)
  } else {
    assert.equal(goal.state, 'READY_FOR_HUMAN_REVIEW', goal.stop_reason)
    assert.equal(goal.best_attempt, 2)
    assert.ok(goal.checks.some(c => c.attempt === 2 && c.stage === 'full' && c.status === 'passed'))
  }
  await sync()
  await eventually(async () => {
    const row = await issue()
    return row?.comment_id && provider.comments.some(c => c.id === row.comment_id && c.user.type === 'Bot')
  }, 'Issue status was not published')
  await close()
  await studio.stop(working.bootId)
  await launch()
  const recovered = await studio.running()
  assert.notEqual(recovered.bootId, working.bootId)
  record('journal_restored', {boot_id: recovered.bootId, journal_id: recovered.journalId})
  origin = recovered.origin
  await page.locator('[data-issue="107"] button').filter({hasText: '연결된 목표 열기'}).waitFor()
  const restoredCard = page.locator('[data-issue="107"]')
  await restoredCard.getByRole('button', {name: '이슈 기록 및 최초 요청'}).click()
  await restoredCard.getByText('최초 요청', {exact: true}).waitFor()
  await restoredCard.locator('pre').filter({hasText: request}).waitFor()
  let restored = await state()
  assert.equal(restored.issues.find(row => row.issue_id === 107)?.run_id, run.id)
  assert.equal(restored.issues.find(row => row.issue_id === 108)?.run_id, null, 'An already closed issue cannot start a goal')
  const snapshot = await json('/api/rooms/' + ws)
  assert.equal(snapshot.runs.length, 1, 'Polling, manual requests and restart must converge on one run')
  assert.equal(provider.calls.some(call => call.startsWith('POST ') && call.endsWith('/pulls')), false)
  assert.equal(provider.comments.filter(c => c.user.type === 'Bot').length, 1, 'One maintained issue comment')
  assert.equal(provider.calls.filter(call => call === 'POST /repos/fixture/settings/issues/7/comments').length, 1, 'Reconcile a lost creation receipt without posting twice')
  await restoredCard.evaluate(el => el.scrollIntoView({block: 'center'}))
  await restoredCard.locator('.github-owner .portrait').waitFor()
  await screenshot('03-github-restored')
  if (mode === 'github') {
    await page.locator('#settings').click()
    await page.locator('#github-settings > summary').click()
    await page.locator('#github-repo option[value="71"]').waitFor({state: 'attached'})
    await page.locator('#github-draft').check()
    await page.locator('#github-save').click()
    await page.locator('#github-settings-note').filter({hasText: '저장했습니다'}).waitFor()
    await page.getByRole('button', {name: '설정 닫기'}).click()
    await sync()
    const published = await eventually(async () => {
      const row = await issue()
      return row?.pr_number && row
    }, 'Opt-in did not publish the verified candidate draft', 60)
    const pr = provider.draft()
    assert.equal(pr.draft, true)
    assert.equal(published.candidate_sha, pr.head.sha)
    assert.equal(pr.base.sha, git('rev-parse', 'HEAD').trim())
    assert.equal(provider.calls.filter(c => c === 'POST /repos/fixture/settings/pulls').length, 1)
    await restoredCard.getByText('PR #9 주소 보기', {exact: true}).click()
    assert.equal(await restoredCard.getByLabel('PR #9 주소').inputValue(), published.pr_url)
    await screenshot('04-verified-draft')
    record('draft', '옵트인 이후 검증된 소스로 초안 PR 생성 · ' + pr.head.sha)
    restored = await state()
  }
  assert.deepEqual(provider.errors, [])
  await writeFile(join(evidence, 'goal.json'), JSON.stringify(goal, null, 2))
  await writeFile(join(evidence, 'github.json'), JSON.stringify({restored, calls: provider.calls, comments: provider.comments}, null, 2))
  record('restore', '호스트 재시작 뒤 목표 ID와 이슈 연결 및 상태 댓글 한 개 확인')
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
async function verifyJourney() {
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
  if (githubMode) {
    if (briefingMode) {
      const {verifyBriefing} = await import('./verify-briefing.mjs')
      const currentPage = () => page
      run = await verifyBriefing({currentPage, json, mode, evidence, provider, traceDir, record, screenshot, studio,
        close, launch: async () => { await launch(); origin = (await studio.running()).origin }})
      return
    }
    await githubJourney()
    return
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
    await page.locator('#brief-questions [data-action="answer"][data-run="' + run.id + '"]').click()
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
}
try {
  await verifyJourney()
  await writeFile(
    join(evidence, 'result.json'),
    JSON.stringify({platform: process.platform, executable, root, mode, run_id: run.id, request, events}, null, 2)
  )
  console.log('Installed journey verified', evidence)
} catch (error) {
  failure = error
  console.error('Fixture evidence:', evidence, 'working directory:', root, error)
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
  if (page && !page.isClosed()) await page.screenshot({path: join(evidence, 'error.png'), timeout: 3000}).catch(() => {})
} finally {
  await finishVerification(failure, () => app && close(), async () => {
    const runtime = await studio.running()
    if (runtime) await studio.stop(runtime.bootId)
  }, () => provider?.close())
}
