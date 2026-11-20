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
git('add', 'note.txt', 'read_note.py', 'tests/test_local.py')
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

  await window.waitForFunction(async shouldBeReal => {
    const response = await fetch('/api/engines')
    const { engines } = await response.json()
    const codex = engines.find(engine => engine.engine === 'codex')
    return codex?.headless_support && codex?.auth_status === 'authenticated' &&
      (shouldBeReal || codex.version === '0.169.1')
  }, live)
  await window.locator('#settings').click()
  await window.locator('#reviewer-engine').selectOption('codex')
  await window.locator('#save-settings').click()
  await window.locator('#room-settings').waitFor({ state: 'hidden' })
  await window.locator('.seat[data-person="coordinator"]').click()
  await window.locator('#allow-edits').check()
  await window.locator('#assignment-options summary').click()
  await window.locator('#test-files').fill('tests/test_local.py')
  await window.locator('#message').fill(live ? [
    '이 실행의 개발 담당은 note.txt 한 파일의 기존 한 줄을 바꿉니다. 개발자의 첫 응답은 아래 기술 질문 두 줄로 보내고 답을 기다리세요.',
    'AGENT_OFFICE_QUESTION',
    '문서에 변경을 추가할까요?',
    '첫 질문에 답변을 받으면 추가 지시를 받을 준비가 되었다고만 응답하고 턴을 끝내세요. 실제 수정할 문장은 담당자 초상화에서 들어온 추가 지시에만 있습니다.',
    '파일 수정은 현재 작업 트리 안에서 승인된 샌드박스를 유지하며 실행하세요. 다른 파일, 커밋, 외부 연결, 추가 권한 요청은 하지 마세요.',
    '검토자는 위 질문의 대상이 아닙니다. 변경된 파일과 작업실 테스트 결과를 읽고 검토 판정을 내려 주세요.',
  ].join('\n') : 'note.txt에 지정 문장을 추가하고 테스트해 주세요.')
  const accepted = window.waitForResponse(response => response.url().endsWith('/assignments') && response.request().method() === 'POST')
  await window.locator('#send').click()
  const submission = await accepted
  const payload = await submission.json()
  assert.equal(submission.status(), 202, JSON.stringify(payload))
  const { run } = payload
  await window.getByRole('button', { name: '대화 닫기' }).click()
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
  await window.getByRole('button', { name: '답변 보내기' }).click()
  await window.locator('#chat-log').getByText('전달됨', { exact: false }).first().waitFor()
  await window.getByRole('button', { name: '대화 닫기' }).click()
  await window.locator('#ledger-label').filter({ hasText: '검토 결과 확인' }).waitFor({ timeout: live ? 240000 : 30000 })
  const final = (await read(local.origin, `/api/runs/${run.id}`)).run
  assert.equal(final.state, 'READY_FOR_HUMAN_REVIEW')
  assert.match(final.diff, /\+desktop verified/)
  assert.match(final.test_output, /1 passed/)
  assert.match(final.review_output, /VERDICT: APPROVED/)
  assert.equal(await readFile(join(repo, 'note.txt'), 'utf8'), 'original\n')
  assert.equal((await readFile(join(final.workspace, 'note.txt'), 'utf8')).replaceAll('\r\n', '\n'), 'desktop verified\n')
  const direction = (await read(local.origin, `/api/runs/${run.id}/directions`)).directions[0]
  assert.equal(direction.delivery, 'delivered')
  await window.screenshot({ path: join(root, 'window.png') })
  await writeFile(resultFile, JSON.stringify({
    provider: live ? 'codex' : 'fixture', platform: process.platform, exe, attached: peer, local,
    runId: run.id, sameRouteAfterReopen: true, state: final.state,
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
