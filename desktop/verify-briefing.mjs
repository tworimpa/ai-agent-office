import assert from 'node:assert/strict'
import {execFileSync} from 'node:child_process'
import {randomUUID} from 'node:crypto'
import {readdir, readFile, writeFile} from 'node:fs/promises'
import {join} from 'node:path'
import {setTimeout as delay} from 'node:timers/promises'

const answer = '자동 변환합니다'
const regression = 'test_windows_legacy_theme_survives_reopen'
const id = () => randomUUID().replaceAll('-', '')

async function until(read, description, seconds = 180) {
  for (const deadline = Date.now() + seconds * 1000; Date.now() < deadline;) {
    const value = await read()
    if (value) return value
    await delay(350)
  }
  throw Error(description)
}

export async function verifyBriefing(ctx) {
  const {currentPage, json, mode, evidence, provider, record, screenshot, studio, close, launch} = ctx
  let page = currentPage()
  const after = mode === 'briefing-after'
  const roomId = new URL(page.url()).searchParams.get('workspace')
  const room = '/api/rooms/' + roomId
  const route = run => '/api/runs/' + run
  const readGoal = async run => (await json(route(run) + '/goal')).goal
  const inbox = async run => (await json(route(run) + '/inbox')).requests
  const pending = (run, kind) => until(async () => {
    const found = (await inbox(run)).find(q => q.kind === kind && q.status === 'pending')
    if (!found) {
      const g = await readGoal(run)
      assert.notEqual(g.recovery?.disposition, 'closed', 'Goal closed before ' + kind + ': ' + g.stop_reason)
    }
    return found
  }, 'Missing ' + kind + ' for ' + run)
  const completed = run => until(async () => {
    const g = await readGoal(run)
    if (g.recovery?.disposition !== 'closed') return
    assert.equal(g.state, 'READY_FOR_HUMAN_REVIEW', JSON.stringify(g).slice(-3000))
    return g
  }, 'Candidate did not finish ' + run)
  const journal = []
  async function action(label, perform) {
    await perform()
    journal.push({label, at: Date.now(), run: new URL(page.url()).searchParams.get('run')})
  }
  async function post(path, body) {
    return page.evaluate(async ({path, body}) => {
      const r = await fetch(path, {method: 'POST', headers: {'Content-Type':'application/json','X-Agent-Office':'studio'},
        body: JSON.stringify(body)})
      if (!r.ok) throw Error('HTTP ' + r.status + ' ' + path + ' ' + (await r.text()).slice(0, 500))
      return r.json()
    }, {path, body})
  }
  async function waitDecisionPoll(run, requestId) {
    const response = await page.waitForResponse(async r => {
      if (!r.ok() || r.request().method() !== 'GET') return false
      const path = new URL(r.url()).pathname
      const standalone = path === room + '/briefing'
      if (!standalone && !['briefing', 'full', 'full-usage'].some(mode => path === room + '/bundle/' + run + '/' + mode)) return false
      const data = await r.json()
      const board = standalone ? data : data.briefing
      return board?.workspace_id === roomId && board.decisions.some(row => row.run_id === run && row.request_id === requestId)
    }, {timeout: 30000})
    const data = await response.json()
    const observed = (data.briefing ?? data).observed_at
    await page.waitForFunction(stamp => Date.parse(document.querySelector('#brief-time')?.dateTime) >= Date.parse(stamp), observed)
  }
  async function select(run) {
    await page.locator('#run-list a[href*="' + run + '"]').click()
  }
  async function answerRow(run, q, board) {
    if (board) {
      const button = page.locator('#brief-questions [data-action="answer"][data-run="' + run + '"]')
      await action('브리핑에서 결정 열기', async () => {await button.focus(); await button.press('Enter')})
    } else if (!await page.locator('#requests').evaluate(el => el.open)) {
      await action('작업실 요청함 열기', () => page.locator('#inbox').click())
    }
    const container = board ? '#office-thread' : '#request-log'
    const row = page.locator(container + ' [data-request="' + run + ':' + q.id + '"]')
    const edit = row.locator('textarea')
    if (board) assert.ok(await edit.evaluate(el => el === document.activeElement))
    await edit.fill(answer)
    if (board) {
      await waitDecisionPoll(run, q.id)
      assert.equal(await edit.inputValue(), answer)
      assert.ok(await edit.evaluate(el => el === document.activeElement))
      await screenshot('03-keyboard-answer')
    }
    await action('제품 선택 전달', () => row.locator('[data-control="answer"]').click())
    await until(async () => (await inbox(run)).some(item => item.id === q.id && item.delivered_at), 'Answer not delivered')
  }
  // Register and sync through the public connection UI. All issuer keys, tokens,
  // HTTP writes and upstream refs belong to the disposable loopback provider.
  await page.setViewportSize({width:1366,height:768})
  await page.locator('#settings').click()
  await page.locator('#github-settings > summary').click()
  await page.locator('#github-repo').selectOption('71')
  await page.locator('#github-actors').fill('42')
  await page.locator('#github-answers').fill('42')
  await page.locator('#github-enabled').check()
  await page.locator('#github-save').click()
  await page.locator('#github-settings-note').filter({hasText:'저장했습니다'}).waitFor()
  await page.getByRole('button',{name:'설정 닫기'}).click()
  provider.feed()
  await page.locator('#github-sync').click()
  const source = await until(async () => (await json(room + '/github')).issues.find(i => i.number === 41 && i.run_id),
    'Issue #41 could not be imported')
  const first = source.run_id
  const choice = await pending(first, 'product_decision')
  const dependency = {source_run:first, stage:'implement', condition:'product_decision', request_id:choice.id,
    reason:'설정 검증은 저장 형식 결정을 사용합니다.'}
  const assignment = (run, message) => ({id:run,message,allow_edits:true,goal:true,goal_criteria:[message],delegation:false,
    role_engines:{developer:'codex',reviewer:'antigravity'}})
  async function direct(message, dep) {
    await page.locator('.seat[data-person="coordinator"]').click()
    if (await page.locator('#receipt-new').isVisible()) await page.locator('#receipt-new').click()
    await page.locator('#message').fill(message)
    await page.locator('#allow-edits').check()
    assert.ok(await page.locator('#overnight-goal').isChecked())
    const opts = page.locator('#assignment-options')
    if (!await opts.evaluate(el => el.open)) await opts.locator('summary').click()
    await page.locator('#goal-criteria').fill(message)
    if (dep) {
      await page.locator('#assignment-areas').fill('설정 형식')
      await page.locator('#assignment-source').selectOption(dep.source_run)
      await page.locator('#assignment-stage').selectOption(dep.stage)
      await page.locator('#assignment-condition').selectOption(dep.condition)
      await page.locator('#assignment-request').selectOption(dep.request_id)
      await page.locator('#assignment-reason').fill(dep.reason)
      await page.locator('#assignment-add').click()
      assert.match(await page.locator('#assignment-preview').innerText(), /1개 조건/)
    }
    const accepted = page.waitForResponse(r => r.url().endsWith('/assignments') && r.request().method() === 'POST')
    await page.locator('#send').click()
    const r = await accepted
    assert.equal(r.status(), 202, await r.text())
    await page.getByRole('button', {name:'대화 닫기'}).click()
    return r.request().postDataJSON()
  }
  if (after) await page.locator('#brief-questions [data-request="' + choice.id + '"]').waitFor()
  const secondBody = await direct('#42 Windows에서 설정 파일을 다시 열면 테마가 초기화되는 오류 재현과 수정', after ? dependency : null)
  const second = secondBody.id, third = (await direct('#43 GitHub 상태 댓글에서 검증 완료와 사용자 검토 대기를 구분해 주세요')).id
  const goal43 = await completed(third)
  assert.ok(goal43.candidates.some(c => c.review_status === 'approved' && c.files.includes('status_comment.py') &&
    c.files.includes('tests/test_status_comment.py')))
  const thirdTree = (await json(route(third))).run.workspace
  const python = (cwd, args) => execFileSync(ctx.python, ['-X', 'utf8', ...args], {cwd,encoding:'utf8',windowsHide:true})
  const comment = python(thirdTree, ['-c', 'from status_comment import render; print(render(43, "ready"))']).trim()
  assert.equal(comment, '#43 · 후보 검증 완료 · 사용자 검토 대기')
  const commentCheck = python(thirdTree, ['-m','pytest','-q','tests/test_status_comment.py'])
  assert.match(commentCheck,/1 passed/)
  await writeFile(join(evidence,'status-comment.txt'), comment + '\n' + commentCheck)
  const secondDetail = (await json(route(second))).run
  const originalTest = await until(async () => {
    const g = await readGoal(second)
    return g.checks.find(c => c.attempt === 0 && c.status === 'failed' &&
      c.failed_ids.some(name => name.includes(regression)))
  }, '#42 did not prove the regression in the original commit')
  assert.match(await readFile(join(secondDetail.workspace, 'tests/test_windows_restore.py'), 'utf8'), /assert reopen\(path\) == "dark"/)
  assert.match(await readFile(join(secondDetail.workspace, 'settings.py'), 'utf8'), /def convert\(data\):\s+return data\r?\n/)
  assert.ok((await inbox(first)).some(q => q.id === choice.id && q.status === 'pending'))
  let waiting, board
  if (after) {
    board = await until(async () => {
      const b = await json(room + '/briefing')
      return b.decisions?.some(d => d.request_id === choice.id && d.impacts.some(i => i.run_id === second && i.stage === 'implement')) &&
        b.work.some(w => w.run_id === second && w.stage === 'implement' && w.wait_reasons.length) &&
        b.candidates.some(c => c.run_id === third) && b
    }, 'No confirmed #42 dependency in #41 decision')
    assert.equal((await inbox(second)).length, 0)
    const section = page.locator('#workspace-briefing')
    await section.getByText(choice.prompt, {exact:false}).first().waitFor()
    const review = page.locator('#brief-review [data-action="candidate"][data-run="' + third + '"]')
    await review.waitFor()
    const extraIds = []
    for (let i = 0; i < 8; i++) {
      const run = id()
      const body = assignment(run, '#' + (61 + i) + ' 이후 설정 확인')
      body.coordination = {priority:0,areas:[],dependencies:[{
        source_run:first, stage:'start', condition:'completion', reason:'저장 형식 후보를 완성한 뒤 확인합니다.'
      }]}
      await post(room + '/assignments', body)
      extraIds.push(run)
    }
    const active = [first, second, ...extraIds]
    board = await until(async () => {
      const b = await json(room + '/briefing')
      const waits = b.work.filter(w => [second,...extraIds].includes(w.run_id) && w.wait_reasons.length)
      return active.every(run => b.work.some(w => w.run_id === run && w.editable)) && waits.length === 9 && b
    }, 'The GitHub source, direct dependent and eight queued goals must be present together')
    assert.equal(board.work.filter(w => w.editable).length, 10)
    await page.locator('#brief-work [data-run="' + extraIds.at(-1) + '"]').first().waitFor({state:'attached'})
    await page.evaluate(() => scrollTo(0, 0))
    const questionButton = page.locator('#brief-questions [data-request="' + choice.id + '"]')
    const views = []
    const proof = section.locator('[data-impact-run="' + second + '"] .brief-proof').filter({hasText:regression})
    await proof.waitFor()
    async function assertProofVisible() {
      const box = await proof.boundingBox()
      const viewport = page.viewportSize()
      assert.ok(box && box.x >= 0 && box.y >= 0 && box.x + box.width <= viewport.width && box.y + box.height <= viewport.height,
        'The measured Windows reopen failure must fit on the initial viewport')
      const question = await questionButton.boundingBox()
      assert.ok(question && question.y >= 0 && question.y + question.height <= viewport.height)
      const candidate = await review.boundingBox()
      assert.ok(candidate && candidate.y >= 0 && candidate.y + candidate.height <= viewport.height,
        'The verified candidate action must fit the initial viewport')
      assert.equal(await page.evaluate(() => scrollY), 0)
      views.push({...viewport,scrollY:0,questionButton:question,candidateButton:candidate,failureEvidence:box})
    }
    await assertProofVisible()
    await screenshot('01-briefing-desktop')
    await page.setViewportSize({width:390,height:844})
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth))
    await assertProofVisible()
    await screenshot('02-briefing-narrow')
    await page.setViewportSize({width:1366,height:768})
    const progress = page.locator('#brief-progress')
    assert.equal(await progress.evaluate(el => el.open), false)
    await progress.locator(':scope > summary').click()
    for (const run of active) await progress.locator('[data-action="work"][data-run="' + run + '"]').waitFor()
    const live = []
    for (const file of await readdir(ctx.traceDir)) {
      const trace = JSON.parse(await readFile(join(ctx.traceDir, file),'utf8'))
      const run = active.find(id => trace.cwd.endsWith('task-' + id))
      if (!run || trace.ended) continue
      try { process.kill(trace.pid, 0); live.push({run,engine:trace.engine,pid:trace.pid}) }
      catch (err) { if (err.code !== 'ESRCH') throw err }
    }
    assert.equal(live.length, 1, 'Only the source has an open coding CLI at this observation')
    assert.equal(live[0].run, first)
    await writeFile(join(evidence,'ten-goals.json'), JSON.stringify({
      observed_at:new Date().toISOString(), briefing_observed_at:board.observed_at, source_issue_number:source.number,
      run_ids:active, registered:active.length, blocked_on_dependency:9, live_fixture_cli_count:live.length,
      live_fixture_processes:live, views, work:board.work.filter(w => active.includes(w.run_id))
    }, null, 2))
    await screenshot('06-ten-goals')
    await progress.locator(':scope > summary').click()
    for (const run of extraIds.toReversed()) await post(route(run) + '/stop', {})
    await action('검토 후보 열기', () => review.click())
    await page.locator('[data-goal-attempt="1"][open]').waitFor()
    assert.equal(new URL(page.url()).searchParams.get('run'), third)
  } else {
    waiting = await pending(second, 'product_decision')
    assert.equal(waiting.prompt, choice.prompt, 'Both workers requested the same product choice')
    await action('작업 기록에서 #43 검토 열기', () => select(third))
    await action('검토 후보 펼치기', () => page.locator('[data-goal-attempt="1"] > summary').click())
    await screenshot('01-before-three-tasks')
  }
  await answerRow(first, choice, after)
  const permission = await pending(first, 'permission')
  if (after) {
    const delivered = (await inbox(first)).find(row => row.id === choice.id)
    const duplicate = await post(route(first) + '/inbox/' + choice.id + '/decision', {action:'answer',answer})
    assert.equal(duplicate.request.delivered_at, delivered.delivered_at, 'Replayed delivery must retain the same receipt')
  }
  if (!after) await answerRow(second, waiting, false)
  const goal42 = await completed(second)
  assert.ok(goal42.candidates.some(c => c.review_status === 'approved' && c.files.includes('tests/test_windows_restore.py')))
  const restoration = JSON.parse(python(secondDetail.workspace, ['-c', [
    'from tempfile import TemporaryDirectory', 'from pathlib import Path', 'from settings import reopen', 'import json',
    'with TemporaryDirectory() as tmp:', '    path = Path(tmp) / "settings.json"',
    '    path.write_bytes(b\'\\xef\\xbb\\xbf{\\r\\n  "theme": "dark"\\r\\n}\\r\\n\')',
    '    initial = reopen(path)', '    saved = json.loads(path.read_text(encoding="utf8"))',
    '    print(json.dumps({"restored": initial, "persisted": saved, "reopened": reopen(path)}))'
  ].join('\n')]))
  assert.deepEqual(restoration,{restored:'dark',persisted:{appearance:{theme:'dark'}},reopened:'dark'})
  await writeFile(join(evidence,'windows-settings.json'), JSON.stringify(restoration,null,2))
  assert.ok((await inbox(first)).some(q => q.id === permission.id && q.status === 'pending'))
  if (after) {
    const b = await json(room + '/briefing')
    const shared = b.work.find(w => w.run_id === second).decisions.filter(d => d.request_id === choice.id)
    assert.equal(shared.length, 1)
    assert.equal(shared[0].answer, answer)
    assert.equal(shared[0].source_run, first)
    assert.equal(shared[0].revision, 1)
  }
  if (after) {
    await action('브리핑에서 권한 열기', () => page.locator('#brief-permissions [data-action="permission"][data-run="' + first + '"]').click())
  } else if (!await page.locator('#requests').evaluate(el => el.open)) {
    await action('권한 요청함 열기', () => page.locator('#inbox').click())
  }
  const permissionRow = page.locator('#requests [data-request="' + first + ':' + permission.id + '"]')
  await permissionRow.waitFor()
  await screenshot('04-separate-permission')
  await action('파일 변경 1회 승인', () => permissionRow.locator('[data-control="approve"]').click())
  const goal41 = await completed(first)
  assert.equal(goal41.candidates[0].review_status, 'approved')
  const goals = [goal41, goal42, goal43]
  assert.ok(goals.every(g => g.candidates.length === 1 && g.candidates[0].review_status === 'approved'))
  assert.equal(goal42.checks.filter(c => c.attempt === 0 && c.status === 'failed' &&
    c.failed_ids.some(name => name.includes(regression))).length, 1)
  const metrics = {actions:journal, product_answers:journal.filter(a => a.label === '제품 선택 전달').length,
    views_opened:journal.filter(a => !['제품 선택 전달', '파일 변경 1회 승인'].includes(a.label)).length,
    independent_goal_user_wait_seconds:goal43.waiting_seconds,
    independent_goal_dependency_wait_seconds:goal43.dependency_wait_seconds ?? 0,
    dependent_goal_user_wait_seconds:goal42.waiting_seconds,
    dependent_goal_dependency_wait_seconds:goal42.dependency_wait_seconds ?? null,
    regression_failure_at:new Date(Date.parse(originalTest.started_at) + originalTest.elapsed_seconds * 1000).toISOString(),
    independent_candidate_reviewed_at:goal43.candidates[0].review_at,
    primary_question_at:choice.created_at, source_answer_at:(await inbox(first)).find(q => q.id === choice.id).delivered_at,
    independent_idle_seconds:null, independent_idle_note:'검증이 기록하는 단계 사이에 운영체제 스케줄링과 실행 대기를 구분할 수 없습니다.'}
  await writeFile(join(evidence,'briefing.json'),JSON.stringify({mode,first,second,third,metrics,board,goals},null,2))
  if (after) {
    await page.getByRole('button',{name:'요청함 닫기'}).click()
    await post(room + '/assignments', secondBody)
    await page.locator('#github-sync').click()
    const original = await studio.running()
    await close()
    await launch()
    assert.equal((await studio.running()).bootId, original.bootId)
    await close()
    await studio.stop(original.bootId)
    await launch()
    page = currentPage()
    assert.notEqual((await studio.running()).bootId, original.bootId)
    assert.equal((await studio.running()).journalId, original.journalId)
    const restored = await json(room)
    assert.equal(restored.runs.length, 11)
    for (const goal of goals) {
      const reopened = await readGoal(goal.run_id)
      assert.deepEqual(reopened.candidates, goal.candidates)
      assert.deepEqual(reopened.checks, goal.checks)
      assert.deepEqual(reopened.events, goal.events)
    }
    assert.equal((await inbox(second)).length, 0)
    assert.equal((await inbox(first)).length, 2)
    assert.equal((await json(room + '/github')).issues.find(i => i.number === 41).run_id, first)
    await screenshot('05-restored')
  }
  assert.deepEqual(provider.errors, [])
  record('three_runs', {first,second,third,product_answers:metrics.product_answers})
  return {id:first}
}
