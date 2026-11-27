import assert from 'node:assert/strict'
import { setTimeout as delay } from 'node:timers/promises'

// Both commits are published in the same repository. The catalog pin adds the bundled skill.
const botId = 'package-inventory-check'
const catalogCommit = '72ee94272d827bf5ce877275466b30af9c1bccee'
const previousSampleCommit = '978ed4102f67a6b7188e1944504c9ec096f768e0'
const skill = 'skills/inventory-check/SKILL.md'

export async function verifyPublicCatalog(page, studio, workspaceId) {
  const {origin, journalId} = studio
  const room = '/api/rooms/' + workspaceId
  const bot = room + '/bots/' + botId
  const originalPage = page.url()
  const journalQuery = '?office_journal=' + journalId

  function checkResponse(payload, path, status, expectedStatus) {
    assert.equal(status, expectedStatus, path + ': ' + JSON.stringify(payload))
    return payload
  }

  async function request(path, body) {
    const response = await fetch(origin + path + journalQuery, {
      redirect: 'error',
      signal: AbortSignal.timeout(5000),
      ...(body === undefined ? {} : {
        method: 'POST', headers: {'Content-Type': 'application/json', 'X-Agent-Office': 'studio', Origin: origin},
        body: JSON.stringify(body),
      }),
    })
    return checkResponse(await response.json(), path, response.status, 200)
  }

  async function clickAndRead(selector, path, method = 'POST', expectedStatus = 200) {
    const [response] = await Promise.all([
      page.waitForResponse(reply => reply.url() === origin + path + journalQuery &&
        reply.request().method() === method, {timeout: 85000}),
      page.locator(selector).click(),
    ])
    return checkResponse(await response.json(), path, response.status(), expectedStatus)
  }

  await page.goto(origin + '/bots?workspace=' + workspaceId)
  const index = await clickAndRead('#botCatalogOpen', room + '/bot-catalog', 'GET')
  assert.equal(index.schemaVersion, 1)
  const entry = index.entries.find(candidate => candidate.id === 'office-inventory-example')
  assert.equal(entry?.botId, botId)
  assert.equal(entry.package.commit, catalogCommit)
  assert.ok(entry.skills.some(item => item.path === skill))
  await page.locator('#botCatalogResults button').filter({hasText: entry.name}).click()
  await page.locator('#botCatalogInfo').getByText(skill, {exact: true}).waitFor()
  await page.locator('#botCatalogAction').click()
  const preview = await clickAndRead('#packageCheck', room + '/bots/package-preview')
  assert.equal(preview.bot_id, botId)
  assert.equal(preview.source.commit, catalogCommit)
  assert.match(preview.digest, /^[a-f0-9]{64}$/)
  assert.ok(preview.files.includes(skill))
  const installed = await clickAndRead('#packageInstall', room + '/bots/package-install')
  assert.equal(installed.id, botId)
  assert.deepEqual(installed.source, preview.source)
  await page.locator('#chatTitle').filter({hasText: 'Inventory checker'}).waitFor()
  const roster = await request(room + '/bots')
  const installedBot = roster.bots.find(item => item.id === botId)
  assert.deepEqual(installedBot?.package, installed.source)

  // Wait for the installed Studio to discover the same local fixture used by the original journey.
  await page.waitForFunction(async () => {
    const response = await fetch('/api/engines')
    if (!response.ok) return false
    const {engines} = await response.json()
    return engines.some(engine => engine.engine === 'codex' && engine.headless_support &&
      engine.auth_status === 'authenticated' && engine.version === '0.169.1')
  }, undefined, {timeout: 30000})
  const task = '작업 파일을 설명해 주세요. 파일 수정 없이 read_note.py를 확인하세요.'
  await page.locator('#msg').fill(task)
  const {run: admitted} = await clickAndRead('#send', bot + '/commands', 'POST', 202)
  assert.equal(admitted.workspace_id, workspaceId)
  assert.equal(admitted.task, task)
  assert.equal(admitted.allow_edits, false)
  const selectedBot = admitted.assignment.bots.developer
  assert.equal(selectedBot.id, botId)
  assert.equal(selectedBot.digest, installedBot.digest)
  assert.equal(admitted.assignment.roles.developer.engine, 'codex')
  const runPath = '/api/runs/' + admitted.id
  let pending
  for (const deadline = Date.now() + 40000; !pending && Date.now() < deadline;) {
    const {requests} = await request(runPath + '/inbox')
    pending = requests.find(row => row.status === 'pending' && row.kind === 'question')
    if (!pending) await delay(150)
  }
  assert.ok(pending, 'The catalog bot run did not reach the fixture question')
  const {request: answered} = await request(runPath + '/inbox/' + pending.id + '/decision', {
    action: 'answer', answer: '네, 파일 읽기 절차를 설명해 주세요.',
  })
  assert.equal(answered.decision, 'answer')
  const fixtureReply = 'read_note.py가 note.txt를 읽습니다.'
  await page.locator('#thread').getByText(fixtureReply, {exact: true}).waitFor({timeout: 40000})

  const sourceDetails = page.locator('#packageSourceDetails')
  if (!await sourceDetails.evaluate(element => element.open)) await sourceDetails.locator('summary').click()
  await page.locator('#packageUpdate').click()
  await page.locator('#packageChangeVersion').fill(previousSampleCommit)
  const update = await clickAndRead('#packageChangeCheck', bot + '/package-update-preview')
  assert.deepEqual([update.current.commit, update.target.commit], [catalogCommit, previousSampleCommit])
  assert.deepEqual(update.conflicts, [])
  assert.ok(update.changes.some(change => change.path === skill && change.action === 'deleted'))
  const updated = await clickAndRead('#packageChangeApply', bot + '/package-update')
  assert.deepEqual(updated.source, update.target)
  assert.deepEqual(updated.rollback, installed.source)
  assert.ok(!Object.hasOwn((await request(bot + '/assets')).files, skill))

  const rollback = await clickAndRead('#packageRollback', bot + '/package-rollback-preview')
  assert.deepEqual(rollback.target, installed.source)
  assert.deepEqual(rollback.conflicts, [])
  assert.ok(rollback.changes.some(change => change.path === skill && change.action === 'added'))
  const restored = await clickAndRead('#packageChangeApply', bot + '/package-rollback')
  assert.deepEqual(restored.source, installed.source)
  assert.equal(restored.rollback, null)
  assert.ok(Object.hasOwn((await request(bot + '/assets')).files, skill))
  const {run} = await request(runPath)
  assert.equal(run.state, 'PLAN_ONLY')
  assert.deepEqual(run.assignment.bots.developer, selectedBot)
  assert.equal(run.developer_output, fixtureReply)
  await page.goto(originalPage)
  return {botId, source: installed.source, updated: updated.source, restored: restored.source,
    runId: run.id, state: run.state}
}
