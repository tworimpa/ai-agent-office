import assert from 'node:assert/strict'
import { execFileSync } from 'node:child_process'
import { access, mkdir, mkdtemp, readFile, realpath, rename, writeFile } from 'node:fs/promises'
import { createServer } from 'node:http'
import { tmpdir } from 'node:os'
import { basename, delimiter, dirname, join } from 'node:path'
import { setTimeout as delay } from 'node:timers/promises'
import { fileURLToPath } from 'node:url'
import { _electron as electron } from 'playwright'
import { LocalStudio } from './studio.mjs'
import { closeDesktop, finishVerification } from './verify-lifecycle.mjs'

if (!process.argv[2]) throw new Error('Usage: node verify-install.mjs PATH_TO_PACKAGED_EXECUTABLE [--live]')
const exe = await realpath(process.argv[2])
const live = process.argv.includes('--live')
const nativePicker = process.argv.includes('--native-picker')
const windows = process.platform === 'win32'
const python = join(dirname(exe), 'resources', 'runtime', windows ? 'python.exe' : 'bin/python3')
const root = await mkdtemp(join(tmpdir(), 'agent-office-desktop-'))
const profile = join(root, 'profile')
const fixtureDir = join(root, 'mock-bin')
const repo = join(root, nativePicker ? 'desktop-project' : '작업공간')
const views = []
const peerProfile = join(root, 'peer')
const resultFile = join(root, 'result.json')
let app, child, failure
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
for (const command of ['codex', 'agy']) {
  const fixture = fileURLToPath(new URL(`./fixtures/${command}.py`, import.meta.url))
  await writeFile(join(fixtureDir, windows ? command + '.cmd' : command),
    windows ? `@"${python}" "${fixture}" %*\r\n`
      : `#!/bin/sh\nexec ${shellQuote(python)} ${shellQuote(fixture)} "$@"\n`,
    { mode: 0o700 })
}
const pathKey = Object.keys(process.env).find(key => key.toUpperCase() === 'PATH') ?? 'PATH'
const env = live ? { ...process.env } : { ...process.env, [pathKey]: fixtureDir + delimiter + process.env[pathKey] }
const attached = new LocalStudio(peerProfile, python)
const standalone = new LocalStudio(join(profile, 'local-studio'), python)

async function launch() {
  app = await electron.launch({ executablePath: exe, args: [`--user-data-dir=${profile}`], env,
    chromiumSandbox: true, timeout: 30000 })
  child = app.process()
  const window = await app.firstWindow({ timeout: 30000 })
  window.context().setDefaultTimeout(live ? 150000 : 20000)
  return window
}

async function close() {
  const launched = app
  app = null
  await closeDesktop(child, () => launched.close())
}

async function verifyClosingRestore() {
  const savedProfile = join(root, 'closing-profile')
  await mkdir(savedProfile)
  const journalId = 'c'.repeat(32)
  const restoring = Promise.withResolvers()
  const server = createServer((request, response) => {
    if (request.url === '/') {
      response.writeHead(200, {'Content-Type': 'text/html'})
      response.write('<!doctype html><title>Restoring</title>')
      restoring.resolve()
      return // Keep navigation in flight until the operator closes the last window.
    }
    const data = request.url === '/api/hosts' ? {hosts: [{id: 'local', name: 'fixture'}]} : {journal_id: journalId}
    response.writeHead(200, {'Content-Type': 'application/json'})
    response.end(JSON.stringify(data))
  })
  await new Promise((resolve, reject) => { server.once('error', reject); server.listen(0, '127.0.0.1', resolve) })
  try {
    const origin = 'http://127.0.0.1:' + server.address().port
    await writeFile(join(savedProfile, 'connection.json'), JSON.stringify({connection: {mode: 'attached', origin, journalId}}))
    app = await electron.launch({executablePath: exe, args: ['--user-data-dir=' + savedProfile], env, chromiumSandbox: true})
    child = app.process()
    await Promise.race([restoring.promise, delay(20000, null, {ref: false}).then(() => { throw Error('Office restore did not start') })])
    const launched = app
    app = null
    await closeDesktop(child, () => launched.evaluate(({BrowserWindow}) => {
      setImmediate(() => BrowserWindow.getAllWindows()[0].close())
    }))
    console.log('Closing the restoring office window exited the desktop.')
  } finally {
    server.closeAllConnections()
    await new Promise(resolve => server.close(resolve))
  }
}

function nextWindow() {
  return app.waitForEvent('window', { timeout: 20000 })
}

async function capture(window, stage) {
  for (const width of [1366, 680]) {
    await window.setViewportSize({ width, height: 768 })
    await window.screenshot({ path: join(root, stage + '-' + width + '.png') })
    const view = await window.evaluate(() => ({
      width: innerWidth, height: innerHeight, scrollWidth: document.documentElement.scrollWidth,
    }))
    assert.ok(view.scrollWidth <= view.width, stage + ' must fit horizontally')
    views.push({ stage, ...view })
  }
  await window.setViewportSize({ width: 1366, height: 768 })
}

async function register(window, local) {
  const picker = window.locator('#pick-folders')
  if (local) {
    if (nativePicker) {
      await writeFile(join(root, 'native-picker.json'), JSON.stringify({ pid: app.process().pid, repo }))
      console.log('Select the test folder in the native picker: ' + repo)
    } else {
      await app.evaluate(({ dialog }, folder) => {
        dialog.showOpenDialog = async (_parent, options) => {
          if (!options.properties.includes('openDirectory')) throw new Error('Expected a directory picker')
          return { canceled: false, filePaths: [folder] }
        }
      }, repo)
    }
    await window.locator('#empty-register').focus()
    await window.keyboard.press('Enter')
    try {
      await window.waitForFunction(name => document.querySelector('#workspace-name').value === name, basename(repo),
        { timeout: nativePicker ? 120000 : 15000 })
    } catch (error) {
      throw new Error('Folder selection did not register the fixture: ' + JSON.stringify({
        name: await window.locator('#workspace-name').inputValue(),
        paths: await window.locator('#workspace-paths').inputValue(),
        note: await window.locator('#register-note').textContent(),
      }), { cause: error })
    }
    await capture(window, '03-register')
  } else {
    await window.locator('#empty-register').click()
    assert.equal(await picker.isVisible(), false)
    await window.locator('#workspace-name').fill('설치판 검증')
    await window.locator('#workspace-paths').fill(repo)
  }
  await window.locator('#save-workspace').click()
  await window.getByRole('heading', { name: local ? basename(repo) : '설치판 검증', exact: true }).waitFor()
}

async function read(origin, path) {
  const response = await fetch(origin + path, { signal: AbortSignal.timeout(5000) })
  assert.equal(response.status, 200, path)
  return response.json()
}

async function decideRequest(window, button, runId) {
  const decision = window.waitForResponse(response => {
    const path = new URL(response.url()).pathname
    return path.startsWith('/api/runs/' + runId + '/inbox/') && path.endsWith('/decision') &&
      response.request().method() === 'POST'
  })
  await button.click()
  assert.ok((await decision).ok())
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
  await window.locator('#assignment-receipt').waitFor()
  const title = await window.locator('#receipt-task').textContent()
  assert.ok(title && message.startsWith(title.replace(/…$/, '')))
  assert.equal(await window.locator('#chat-form').isVisible(), false)
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
  await verifyClosingRestore()
  let window = await launch()
  await capture(window, '01-setup')
  assert.equal(await window.locator('#address').isVisible(), false)
  const openedLocal = nextWindow()
  await window.getByRole('button', { name: '내 작업실 시작' }).click()
  window = await openedLocal
  await window.locator('#empty-register').waitFor()
  const local = await standalone.running()
  assert.equal(new URL(window.url()).origin, local.origin)
  await capture(window, '02-empty')
  await register(window, true)
  await capture(window, '04-room')

  let codex, agy
  for (const deadline = Date.now() + 30000; Date.now() < deadline;) {
    const { engines } = await read(local.origin, '/api/engines')
    codex = engines.find(engine => engine.engine === 'codex')
    agy = engines.find(engine => engine.engine === 'antigravity')
    if (codex?.headless_support && codex.auth_status === 'authenticated' && (live || agy?.headless_support)) break
    await delay(150)
  }
  assert.ok(codex?.headless_support && codex.auth_status === 'authenticated',
    'Codex metadata probe is not ready: ' + JSON.stringify({ codex, agy }))
  if (live) {
    // The optional live walkthrough has always targeted Codex for both roles.
    await window.locator('#settings').click()
    await window.locator('#reviewer-engine').selectOption('codex')
    await window.locator('#save-settings').click()
    await window.locator('#room-settings').waitFor({ state: 'hidden' })
  } else {
    assert.equal(codex.version, '0.169.1')
    assert.equal(agy?.version, '1.0.0-fixture')
  }
  let simpleId
  if (!live) {
    const first = window.getByRole('button', {name: /첫 작업 맡기기/})
    await first.focus()
    await window.keyboard.press('Enter')
    await capture(window, '05-assignment')
    const simple = await submitAssignment(window, '작업 파일을 설명해 주세요. 파일 수정 없이 read_note.py와 tests/test_local.py를 확인하세요.')
    assert.equal(simple.assignment.roles.developer.engine, 'codex')
    assert.equal(simple.assignment.roles.reviewer.engine, 'antigravity')
    await capture(window, '06-receipt')
    await window.locator('#receipt-owner').focus()
    await window.keyboard.press('Enter')
    const chat = window.locator('#conversation')
    await chat.locator('.request-item textarea').waitFor()
    await chat.locator('#message').fill('테스트 파일도 함께 설명해 주세요.')
    const sent = window.waitForResponse(r => r.url().endsWith('/runs/' + simple.id + '/directions') && r.request().method() === 'POST')
    await chat.locator('#send').click()
    assert.ok((await sent).ok())
    await chat.locator('.request-item textarea').fill('네, 테스트도 설명해 주세요.')
    await decideRequest(window, chat.getByRole('button', { name: '답변 보내기' }), simple.id)
    await window.keyboard.press('Escape')
    const automatic = await finishedRun(local.origin, simple.id)
    assert.equal(automatic.state, 'PLAN_ONLY', JSON.stringify(automatic))
    const directions = (await read(local.origin, '/api/runs/' + simple.id + '/directions')).directions
    assert.equal(directions.length, 1)
    assert.equal(directions[0].delivery, 'delivered')
    assert.equal(automatic.meeting.revision, 2)
    assert.equal(automatic.meeting.revisions.at(-1).direction_id, directions[0].id)
    assert.deepEqual((await read(local.origin, '/api/rooms/' + simple.workspace_id)).runs.map(item => item.id), [simple.id])
    assert.equal(automatic.meeting.revisions[0].approved_by, 'coordinator')
    assert.deepEqual(automatic.meeting.contributions.filter(entry => entry.revision === 1).map(entry => entry.role), ['developer', 'reviewer'])
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
    await window.locator('#receipt-new').click()
    assert.equal(await window.locator('#message').inputValue(), '')
    assert.equal(await window.locator('#assignment-receipt').isVisible(), false)
  }
  await window.getByRole('button', { name: '대화 닫기' }).click()
  if (!live) {
    const planAction = window.locator(`#brief-questions [data-action="plan"][data-run="${run.id}"]`)
    await planAction.focus()
    await window.keyboard.press('Enter')
    assert.equal(await window.locator('#meeting-inputs input[type=radio]').first().evaluate(el => el === document.activeElement), true)
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
  const question = window.locator(`#brief-questions [data-action="answer"][data-run="${run.id}"]`)
  await question.waitFor()
  await capture(window, '07-question')
  await question.focus()
  await window.keyboard.press('Enter')
  const draft = window.locator('#office-thread .request-item textarea')
  assert.equal(await draft.evaluate(el => el === document.activeElement), true)
  await draft.fill('네. 대기 중인 추가 지시를 적용해 주세요.')
  await decideRequest(window, window.locator('#office-thread').getByRole('button', { name: '답변 보내기' }), run.id)
  if (!live) {
    const approved = await approvePlan(window, 3, false)
    assert.equal(approved.revisions.at(-1).source, 'direction')
    const permission = window.locator(`#brief-permissions [data-action="permission"][data-run="${run.id}"]`)
    await permission.focus()
    await window.keyboard.press('Enter')
    const pending = (await read(local.origin, '/api/runs/' + run.id + '/inbox')).requests
      .find(row => row.kind === 'permission' && row.status === 'pending')
    assert.ok(pending)
    const item = window.locator(`#requests [data-request="${run.id}:${pending.id}"]`)
    await item.waitFor()
    assert.ok(await item.evaluate(el => el.contains(document.activeElement)))
    const target = (await read(local.origin, '/api/runs/' + run.id)).run.workspace
    assert.equal((await readFile(join(target, 'note.txt'), 'utf8')).trim(), 'original')
    await decideRequest(window, item.getByRole('button', { name: '이번 요청만 승인' }), run.id)
    await window.keyboard.press('Escape')
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
  const peer = await attached.start()
  assert.notEqual(peer.journalId, local.journalId)
  const setup = nextWindow()
  await app.evaluate(({ Menu }) => Menu.getApplicationMenu().items[0].submenu.items[0].click())
  window = await setup
  await window.locator('#connection-options summary').first().click()
  await window.locator('#address').fill('http://example.invalid:8766')
  await window.locator('#connect button').click()
  await window.getByRole('alert').filter({ hasText: '이 컴퓨터의 Studio 주소' }).waitFor()
  await window.locator('#address').fill(peer.origin)
  const connected = nextWindow()
  await window.locator('#connect button').click()
  window = await connected
  await window.locator('#empty-register').waitFor()
  assert.equal(new URL(window.url()).origin, peer.origin)
  await register(window, false)
  await close()
  assert.equal((await attached.running()).journalId, peer.journalId)
  assert.equal((await read(peer.origin, '/api/workspaces')).workspaces.length, 1)
  window = await launch()
  await window.getByRole('heading', { name: '설치판 검증' }).waitFor()
  assert.equal(new URL(window.url()).origin, peer.origin)
  await close()
  const config = join(profile, 'connection.json')
  const saved = JSON.parse(await readFile(config, 'utf8'))
  const db = join(profile, 'local-studio', 'tasks.sqlite3')
  assert.ok(await standalone.stop(local.bootId))
  await rename(db, db + '.backup')
  saved.connection = { mode: 'local', origin: local.origin }
  await writeFile(config, JSON.stringify(saved))
  window = await launch()
  await window.getByRole('alert').filter({ hasText: '저장된 로컬 저널 ID' }).waitFor()
  await rename(config, config + '.backup')
  await window.locator('#retry').click()
  await window.getByRole('alert').filter({ hasText: 'connection.json' }).waitFor()
  assert.equal(await window.locator('#start-options').isVisible(), false)
  await assert.rejects(access(db), { code: 'ENOENT' })
  saved.connection.journalId = local.journalId
  await writeFile(config, JSON.stringify(saved))
  await window.locator('#retry').click()
  await window.getByRole('alert').filter({ hasText: local.journalId }).waitFor()
  await assert.rejects(access(db), { code: 'ENOENT' })
  await rename(db + '.backup', db)
  const repaired = nextWindow()
  await window.locator('#retry').click()
  window = await repaired
  await window.waitForURL(url => url.searchParams.get('run') === run.id)
  assert.equal((await standalone.running()).journalId, local.journalId)
  await writeFile(resultFile, JSON.stringify({
    provider: live ? 'codex' : 'fixture', platform: process.platform, exe, attached: peer, local,
    folderPicker: nativePicker ? 'operating-system' : 'dialog-result-fixture', views,
    runId: run.id, simpleId, sameRouteAfterReopen: true, state: final.state, meeting: final.meeting,
    persistedDirection: direction.id, test: final.test_output, review: final.review_output,
  }, null, 2))
  console.log(`Desktop verification passed: ${resultFile}`)
} catch (error) {
  failure = error
  console.error('Desktop installation verification failed:', error)
  for (const window of app?.windows() ?? []) {
    console.error('Window:', window.url())
    if (window.url().endsWith('/setup.html')) {
      const status = await window.locator('#error').textContent({ timeout: 1500 }).catch(() => '')
      if (status) console.error('Setup reported:', status)
    }
  }
} finally {
  await finishVerification(failure, () => app && close(), ...[standalone, attached].map(runtime => async () => {
    const running = await runtime.running()
    if (running) await runtime.stop(running.bootId)
  }))
}
