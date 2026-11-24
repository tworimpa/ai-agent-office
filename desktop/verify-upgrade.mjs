// Start the new installed desktop on the exact profile populated by verify-install.
import assert from 'node:assert/strict'
import { readFile, realpath, writeFile } from 'node:fs/promises'
import { delimiter, dirname, join } from 'node:path'
import { _electron as electron } from 'playwright'
import { LocalStudio } from './studio.mjs'
import { closeDesktop, finishVerification } from './verify-lifecycle.mjs'

const [target, directory, version] = process.argv.slice(2)
if (!target || !directory || !version) throw Error('Usage: node verify-upgrade.mjs INSTALLED_EXECUTABLE SEED_DIRECTORY NEW_VERSION')
if (process.env.RUNNER_ENVIRONMENT !== 'github-hosted' || process.env.GITHUB_ACTIONS !== 'true') {
  throw Error('Testing the default desktop profile requires a disposable hosted runner')
}
const exe = await realpath(target)
const seed = JSON.parse(await readFile(join(directory, 'result.json'), 'utf8'))
assert.equal(seed.exe, exe, 'In-place upgrade must retain the installed application path')
assert.equal(seed.provider, 'fixture')
assert.equal(seed.installedVersion, '0.8.1')
assert.equal(seed.state, 'READY_FOR_HUMAN_REVIEW')
assert.ok(seed.workspaceId && seed.task && seed.runId && seed.simpleId && seed.persistedDirection)
assert.equal(seed.profileMode, 'default')
const profile = await realpath(process.env.OFFICE_VERIFY_DEFAULT_PROFILE)
assert.equal(profile, seed.observedProfile)
const python = join(dirname(exe), 'resources', 'runtime', process.platform === 'win32' ? 'python.exe' : 'bin/python3')
const studio = new LocalStudio(join(profile, 'local-studio'), python)
assert.equal(await studio.running(), null, 'Old Studio must stop before launching the upgraded application')
const pathKey = Object.keys(process.env).find(key => key.toUpperCase() === 'PATH')
const env = {...process.env, [pathKey]: join(directory, 'mock-bin') + delimiter + process.env[pathKey]}
let app, page, failure
try {
  app = await electron.launch({executablePath: exe, env, chromiumSandbox: true})
  page = await app.firstWindow()
  const observed = await app.evaluate(({app}) => ({version: app.getVersion(), profile: app.getPath('userData')}))
  assert.equal(observed.version, version)
  const upgradedProfile = await realpath(observed.profile)
  assert.equal(upgradedProfile, seed.observedProfile, 'The upgraded desktop selected a different default profile')
  await page.waitForURL(url => url.searchParams.get('workspace') === seed.workspaceId && url.searchParams.get('run') === seed.runId)
  await page.getByRole('heading', {name: seed.workspaceName, exact: true}).waitFor()
  await page.locator('#ledger-label').filter({hasText:'검토 결과 확인'}).waitFor()
  const active = await studio.running()
  assert.equal(new URL(page.url()).origin, active.origin)
  assert.equal(active.journalId, seed.local.journalId)
  assert.notEqual(active.bootId, seed.lastBootId)
  async function read(path) {
    const response = await fetch(active.origin + path, {redirect: 'error', signal: AbortSignal.timeout(5000)})
    assert.equal(response.status, 200, path)
    return response.json()
  }
  const room = await read('/api/rooms/' + seed.workspaceId)
  assert.equal(room.workspace.id, seed.workspaceId)
  assert.deepEqual(await Promise.all(room.workspace.folders.map(path => realpath(path))), [await realpath(seed.repository)])
  assert.equal(room.journal_id, seed.local.journalId)
  assert.equal(room.runs.find(run => run.id === seed.simpleId)?.state, 'PLAN_ONLY')
  assert.equal(room.runs.find(run => run.id === seed.runId)?.state, seed.state)
  const {run} = await read('/api/runs/' + seed.runId)
  assert.equal(run.workspace_id, seed.workspaceId)
  assert.equal(run.task, seed.task)
  assert.equal(run.test_output, seed.test)
  assert.equal(run.review_output, seed.review)
  assert.deepEqual(run.meeting.revisions, seed.meeting.revisions)
  const {directions} = await read('/api/runs/' + seed.runId + '/directions')
  assert.equal(directions.find(direction => direction.id === seed.persistedDirection)?.delivery, 'delivered')
  await page.screenshot({path: join(directory, 'upgrade-reopened.png')})
  await writeFile(join(directory, 'upgrade.json'), JSON.stringify({
    version: observed.version, installedExecutable: exe, journalId: active.journalId,
    previousDefaultProfile: seed.observedProfile, upgradedDefaultProfile: upgradedProfile,
    previousVersion: seed.installedVersion, previousBoot: seed.lastBootId, upgradedBoot: active.bootId, workspaceId: seed.workspaceId,
    runIds: [seed.simpleId, seed.runId], revisions: run.meeting.revisions.length,
    persistedDirection: seed.persistedDirection, state: run.state, provider: seed.provider,
  }, null, 2))
  console.log('Reopened the installed 0.8.1 workspace and verified both original tasks in version', observed.version)
} catch (error) {
  failure = error
  if (page) console.error('Upgrade verification failed on page:', page.url())
} finally {
  await finishVerification(failure, () => app && closeDesktop(app.process(), () => app.close()), async () => {
    const running = await studio.running()
    if (running) await studio.stop(running.bootId)
  })
}
