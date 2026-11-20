import assert from 'node:assert/strict'
import { execFileSync } from 'node:child_process'
import { mkdir, mkdtemp, readFile, realpath, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { delimiter, dirname, join } from 'node:path'
import { setTimeout as delay } from 'node:timers/promises'
import { fileURLToPath } from 'node:url'
import { _electron as electron } from 'playwright'
import { LocalStudio } from './studio.mjs'

if (!process.argv[2]) throw new Error('Usage: node verify-install.mjs PATH_TO_PACKAGED_EXECUTABLE [--live]')
const exe = await realpath(process.argv[2])
const live = process.argv.includes('--live')
const windows = process.platform === 'win32'
const python = join(dirname(exe), 'resources', 'runtime', windows ? 'python.exe' : 'bin/python3')
const root = await mkdtemp(join(tmpdir(), 'agent-office-desktop-'))
const profile = join(root, 'profile')
const fixtureDir = join(root, 'mock-bin')
const repo = join(root, '작업공간')
const fixture = fileURLToPath(new URL('./fixtures/codex.py', import.meta.url))
const peerProfile = join(root, 'peer')
const resultFile = join(root, 'result.json')
let app
console.log(`Evidence directory: ${root}`)

function git(...args) {
  return execFileSync('git', args, { cwd: repo, timeout: 15000, stdio: ['ignore', 'pipe', 'pipe'] }).toString()
}

function shellQuote(value) {
  return "'" + value.replaceAll("'", "'\"'\"'") + "'"
}

await mkdir(fixtureDir)
await mkdir(repo)
await writeFile(join(repo, '.gitignore'), '__pycache__/\n.pytest_cache/\n')
await writeFile(join(repo, 'note.txt'), 'original\n')
await writeFile(join(repo, 'read_note.py'), [
  'from pathlib import Path',
  'def text():',
  '    return Path("note.txt").read_text(encoding="utf-8")',
  '',
].join('\n'))
await mkdir(join(repo, 'tests'))
await writeFile(join(repo, 'tests', 'test_local.py'), [
  'from read_note import text',
  'def test_assignment_kept_note():',
  '    assert text() == "desktop verified\\n"',
  '',
].join('\n'))
git('init', '-q')
git('add', '.gitignore', 'note.txt', 'read_note.py', 'tests/test_local.py')
git('-c', 'user.name=Office Fixture', '-c', 'user.email=fixture@example.invalid', 'commit', '-qm', 'initial')
await writeFile(join(fixtureDir, windows ? 'codex.cmd' : 'codex'),
  windows ? `@"${python}" "${fixture}" %*\r\n`
    : `#!/bin/sh\nexec ${shellQuote(python)} ${shellQuote(fixture)} "$@"\n`,
  { mode: 0o700 })
const pathKey = Object.keys(process.env).find(key => key.toUpperCase() === 'PATH') ?? 'PATH'
const env = live ? { ...process.env } : { ...process.env, [pathKey]: fixtureDir + delimiter + process.env[pathKey] }
const attached = new LocalStudio(peerProfile, python)
const standalone = new LocalStudio(join(profile, 'local-studio'), python)

async function launch() {
  app = await electron.launch({ executablePath: exe, args: [`--user-data-dir=${profile}`], env,
    chromiumSandbox: true, timeout: 30000 })
  const window = await app.firstWindow({ timeout: 30000 })
  window.context().setDefaultTimeout(live ? 150000 : 20000)
  return window
}

async function close() {
  const launched = app
  const child = launched.process()
  const closing = launched.close()
  // On Windows a detached runtime can outlive the Playwright launcher's pipe.
  await Promise.race([closing, (async () => {
    for (const deadline = Date.now() + 15000; Date.now() < deadline;) {
      if (child.exitCode !== null || child.signalCode !== null) return
      await delay(75)
    }
    throw new Error('Desktop process did not exit.')
  })()])
  app = null
}

function nextWindow() {
  return app.waitForEvent('window', { timeout: 20000 })
}

async function register(window) {
  await window.locator('#register').click()
  await window.locator('#workspace-name').fill('설치판 검증')
  await window.locator('#workspace-paths').fill(repo)
  await window.locator('#save-workspace').click()
  await window.getByRole('heading', { name: '설치판 검증' }).waitFor()
}

async function read(origin, path) {
  const response = await fetch(origin + path, { signal: AbortSignal.timeout(5000) })
  assert.equal(response.status, 200, path)
  return response.json()
}

async function finishedRun(origin, id) {
  let run
  for (const deadline = Date.now() + (live ? 240000 : 40000); Date.now() < deadline;) {
    run = (await read(origin, '/api/runs/' + id)).run
    if (!['QUEUED', 'RUNNING', 'TESTING', 'REVIEWING', 'STOP_REQUESTED'].includes(run.state)) return run
    await delay(150)
  }
  throw new Error('Run still active: ' + JSON.stringify({ id, state: run?.state, meeting: run?.meeting?.status }))
}

async function submitAssignment(window, message) {
  await window.locator('#message').fill(message)
  const accepted = window.waitForResponse(response => response.url().endsWith('/assignments') && response.request().method() === 'POST')
  await window.locator('#send').click()
  const submission = await accepted
  const payload = await submission.json()
  assert.equal(submission.status(), 202, JSON.stringify(payload))
  assert.equal(payload.run.assignment.meeting, true)
  await window.getByRole('button', { name: '대화 닫기' }).click()
  return payload.run
}

async function approvePlan(window, revision, edit) {
  const form = window.locator('#meeting-decision')
  await form.waitFor()
  await window.locator('#meeting-phase').filter({ hasText: '계획 ' + revision + '판' }).waitFor()
  // Both independent product decisions are submitted by the same operator action.
  assert.equal(await form.locator('input[type=radio]').count(), 4)
  await form.getByRole('radio', { name: '한 줄', exact: true }).check()
  await form.getByRole('radio', { name: '유지', exact: true }).check()
  if (edit) {
    await form.locator('.meeting-editor summary').click()
    await form.locator('#meeting-outcome').fill('수정한 목표: 지정 문구와 검증 근거를 함께 확인')
  }
  const reply = window.waitForResponse(response => response.url().endsWith('/meeting/decision') && response.request().method() === 'POST')
  await form.locator('#approve-plan').click()
  const response = await reply
  const body = await response.json()
  assert.equal(response.status(), 200, JSON.stringify(body))
  assert.equal(body.meeting.decision.revision, revision)
  assert.deepEqual(body.meeting.answers, { q1: '한 줄', q2: '유지' })
  return body.meeting
}

try {
  const peer = await attached.start()
  let window = await launch()
  await window.locator('#address').fill('http://example.invalid:8766')
  await window.getByRole('button', { name: '기존 작업실 연결' }).click()
  await window.getByRole('alert').filter({ hasText: '이 컴퓨터의 Studio 주소' }).waitFor()
  await window.locator('#address').fill(peer.origin)
  await window.getByRole('button', { name: '기존 작업실 연결' }).click()
  const openedPeer = nextWindow()
  await window.locator('#open').click()
  window = await openedPeer
  await window.locator('#register').waitFor()
  assert.equal(new URL(window.url()).origin, peer.origin)
  await register(window)
  await close()
  assert.equal((await attached.running()).journalId, peer.journalId)
  assert.equal((await read(peer.origin, '/api/workspaces')).workspaces.length, 1)
  console.log('Existing Studio connection survived closing the desktop app.')

  window = await launch()
  await window.getByRole('heading', { name: '설치판 검증' }).waitFor()
  await app.evaluate(({ Menu }) => Menu.getApplicationMenu().items[0].submenu.items[0].click())
  window = app.windows().find(page => page.url().includes('setup.html')) ?? await nextWindow()
  await window.getByRole('button', { name: '로컬 작업실 시작' }).click()
  await window.getByText('로컬 작업실 실행 중', { exact: true }).waitFor()
  const openedLocal = nextWindow()
  await window.locator('#open').click()
  window = await openedLocal
  const local = await standalone.running()
  assert.notEqual(local.journalId, peer.journalId)
  assert.equal(new URL(window.url()).origin, local.origin)
  await register(window)

  let codex
  for (const deadline = Date.now() + 30000; Date.now() < deadline;) {
    const { engines } = await read(local.origin, '/api/engines')
    codex = engines.find(engine => engine.engine === 'codex')
    if (codex?.headless_support && codex.auth_status === 'authenticated' && (live || codex.version === '0.169.1')) break
    await delay(150)
  }
  assert.ok(codex?.headless_support && codex.auth_status === 'authenticated', 'Codex metadata probe is not ready')
  if (!live) assert.equal(codex.version, '0.169.1')
  await window.locator('#settings').click()
  await window.locator('#reviewer-engine').selectOption('codex')
  await window.locator('#save-settings').click()
  await window.locator('#room-settings').waitFor({ state: 'hidden' })
  let simpleId
  if (!live) {
    await window.locator('.seat[data-person="coordinator"]').click()
    const simple = await submitAssignment(window, '작업 파일을 설명해 주세요. 파일 수정 없이 read_note.py와 tests/test_local.py를 확인하세요.')
    const automatic = await finishedRun(local.origin, simple.id)
    assert.equal(automatic.state, 'PLAN_ONLY', JSON.stringify(automatic))
    assert.equal(automatic.meeting.revision, 1)
    assert.equal(automatic.meeting.revisions[0].approved_by, 'coordinator')
    assert.deepEqual(automatic.meeting.contributions.map(entry => entry.role), ['developer', 'reviewer'])
    assert.deepEqual(automatic.meeting.choices, [])
    assert.equal(automatic.meeting.decision, null)
    assert.equal(automatic.test_output, '')
    assert.equal((await readFile(join(automatic.workspace, 'note.txt'), 'utf8')).replaceAll('\r\n', '\n'), 'original\n')
    simpleId = simple.id
  }
  await window.locator('.seat[data-person="coordinator"]').click()
  await window.locator('#allow-edits').check()
  if (!live) await window.locator('#review-plan').check()
  await window.locator('#assignment-options summary').click()
  await window.locator('#test-files').fill('tests/test_local.py')
  const run = await submitAssignment(window, live ? [
    '기획 회의는 질문 없이 자동 진행하세요. 완료 기준에 tests/test_local.py를 연결하세요. 아래 기술 질문은 실행 단계에서만 합니다.',
    '완료 기준은 note.txt의 내용이 desktop verified 한 줄과 끝 줄바꿈으로 바뀌는 것입니다. 개발자의 첫 응답은 아래 기술 질문 두 줄로 보내고 답을 기다리세요.',
    'AGENT_OFFICE_QUESTION',
    '문서에 변경을 추가할까요?',
    '첫 질문에 답변을 받으면 추가 지시를 받을 준비가 되었다고만 응답하고 턴을 끝내세요. 실제 수정은 담당자 초상화에서 추가 지시를 받은 후 진행하세요.',
    '파일 수정은 현재 작업 트리 안에서 승인된 샌드박스를 유지하며 실행하세요. 다른 파일, 커밋, 외부 연결, 추가 권한 요청은 하지 마세요.',
    '검토자는 위 질문의 대상이 아닙니다. 현재 스레드에서 직접 변경된 파일과 작업실 테스트 결과를 읽고 독립적인 검토 판정을 내려 주세요.',
  ].join('\n') : 'note.txt의 문장 형식과 마지막 줄바꿈을 결정한 뒤 지정 문장을 추가하고 테스트해 주세요.')
  if (!live) {
    await window.locator('#meeting-decision').waitFor()
    const waiting = (await read(local.origin, '/api/runs/' + run.id)).run
    assert.equal(waiting.meeting.status, 'awaiting_review')
    assert.equal(waiting.developer_output, '')
    assert.equal((await readFile(join(waiting.workspace, 'note.txt'), 'utf8')).replaceAll('\r\n', '\n'), 'original\n')
    const approved = await approvePlan(window, 1, true)
    assert.equal(approved.revision, 2)
    assert.equal(approved.revisions.at(-1).source, 'operator')
  }
  await window.locator('.seat[data-person="developer"] .seat-line').filter({ hasText: '사용자 응답을 기다리고 있습니다.' }).waitFor()
  await window.locator('.seat[data-person="developer"]').click()
  await window.locator('#message').fill('추가 지시입니다. note.txt의 한 줄을 desktop verified로 교체하고, 줄바꿈을 남기고 실제 내용이 바뀌었는지 확인해 주세요. 다른 파일은 그대로 두세요.')
  await window.locator('#send').click()
  await window.locator('#chat-log').getByText('전달 대기', { exact: false }).waitFor()
  await close()
  assert.equal((await standalone.running()).bootId, local.bootId)
  assert.equal((await read(local.origin, `/api/runs/${run.id}/directions`)).directions[0].delivery, 'queued')
  console.log('Local run and queued direction survived closing the desktop app.')

  window = await launch()
  await window.waitForURL(url => url.searchParams.get('run') === run.id)
  await window.locator('.seat[data-person="developer"]').click()
  await window.locator('#chat-log .request-item textarea').fill('네. 대기 중인 추가 지시를 적용해 주세요.')
  await window.locator('#conversation').getByRole('button', { name: '답변 보내기' }).click()
  await window.getByRole('button', { name: '대화 닫기' }).click()
  if (!live) {
    const approved = await approvePlan(window, 3, false)
    assert.equal(approved.revisions.at(-1).source, 'direction')
  }
  const final = await finishedRun(local.origin, run.id)
  assert.equal(final.state, 'READY_FOR_HUMAN_REVIEW', JSON.stringify({
    id: run.id, outcome: final.outcome, diagnostic: final.diagnostic, test: final.test_output, meeting: final.meeting?.error,
  }))
  await window.locator('#ledger-label').filter({ hasText: '검토 결과 확인' }).waitFor()
  assert.match(final.diff, /\+desktop verified/)
  assert.match(final.test_output, /1 passed/)
  assert.match(final.review_output, /VERDICT: APPROVED/)
  assert.equal(await readFile(join(repo, 'note.txt'), 'utf8'), 'original\n')
  assert.equal((await readFile(join(final.workspace, 'note.txt'), 'utf8')).replaceAll('\r\n', '\n'), 'desktop verified\n')
  const direction = (await read(local.origin, `/api/runs/${run.id}/directions`)).directions[0]
  assert.equal(direction.delivery, 'delivered')
  assert.equal(final.meeting.run_id, run.id)
  const revised = final.meeting.revisions.find(version => version.direction_id === direction.id)
  assert.ok(revised, 'Direction must have a revision in the original run')
  assert.equal(revised.reason, direction.message)
  assert.ok(final.meeting.verification.some(check => check.status === 'passed' && check.evidence.includes('tests/test_local.py')))
  if (!live) {
    assert.deepEqual(final.meeting.verification.map(row => row.status), ['passed', 'not_verified'])
    assert.equal(final.meeting.revisions.length, 3)
    assert.equal(final.meeting.revisions[1].verification[0].status, 'not_verified')
    assert.deepEqual(final.meeting.contributions.filter(row => row.revision === 3).map(row => row.role), ['developer', 'reviewer'])
    assert.match(final.meeting.plan.tasks[0].description, /desktop verified/)
  }
  await window.screenshot({ path: join(root, 'window.png') })
  await writeFile(resultFile, JSON.stringify({
    provider: live ? 'codex' : 'fixture', platform: process.platform, exe, attached: peer, local,
    runId: run.id, simpleId, sameRouteAfterReopen: true, state: final.state, meeting: final.meeting,
    persistedDirection: direction.id, test: final.test_output, review: final.review_output,
  }, null, 2))
  console.log(`Desktop verification passed: ${resultFile}`)
} finally {
  if (app) await close()
  for (const runtime of [standalone, attached]) {
    const running = await runtime.running()
    if (running) await runtime.stop(running.bootId)
  }
}
