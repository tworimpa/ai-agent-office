import assert from 'node:assert/strict'
import {mkdir, readFile, realpath, writeFile} from 'node:fs/promises'
import {join, resolve} from 'node:path'
import {launch, until} from './drivers.mjs'
import {prepare} from './handoff-fixture.mjs'
import {finishVerification} from '../verify-lifecycle.mjs'

const [electron, rust, output] = process.argv.slice(2)
if (!output) throw Error('Usage: node verify-handoff.mjs ELECTRON_EXE RUST_EXE EVIDENCE_DIR')
const exes = {electron:await realpath(electron),rust:await realpath(rust)}
const evidence = resolve(output)
await mkdir(evidence,{recursive:true})
const {root,repo,studio} = await prepare(exes.electron)
const runtime = await studio.start().catch(async error => {
  // Cold Python import may outlive the desktop launcher's interactive deadline.
  if (!error.message.includes('시간이 걸립니다')) throw error
  return until(() => studio.running(),'isolated Studio startup',45000)
})
let page, workspace, runId, failure, step
const registered = new Set()
const results = []
const setStep = name => { step = name; console.log(name) }
const json = async path => {
  const response = await fetch(runtime.origin + path,{signal:AbortSignal.timeout(8000)})
  assert.ok(response.ok,path + ' ' + response.status)
  return response.json()
}
const path = tail => '/api/runs/' + runId + tail
const detail = async () => (await json(path(''))).run
const requests = async () => (await json(path('/inbox'))).requests
const directions = async () => (await json(path('/directions'))).directions
async function close() {
  if (page) await page.close()
  page = null
}
async function open(kind) {
  setStep('Open ' + kind)
  page = await launch(kind,exes[kind],join(root,kind))
  if (!registered.has(kind)) {
    if (kind === 'electron') await page.activate('#connection-options > summary')
    await page.ready('#address')
    await page.fill('#address',runtime.origin)
    await page.activate(kind === 'electron' ? '#connect button' : '#connect')
    if (kind === 'rust') {
      await page.ready('#confirm')
      assert.ok((await page.run(() => document.querySelector('#identity').textContent)).includes(runtime.journalId))
      await page.activate('#confirm')
    }
    registered.add(kind)
  }
  await page.selectWindow(url => url.startsWith(runtime.origin + '/'))
  await page.size(1100)
  assert.equal(await page.run(() => location.origin),runtime.origin)
  const connected = await page.run(async () => (await fetch('/api/desktop-runtime')).json())
  assert.equal(connected.journal_id,runtime.journalId)
  assert.equal(connected.boot_id,runtime.bootId)
  if (workspace && !(await page.run(() => new URL(location.href).searchParams.get('workspace')))) {
    await page.activate('#workspaces a[href*="' + workspace + '"]')
  }
}
async function register() {
  await page.activate('#register')
  await page.ready('#workspace-paths')
  await page.fill('#workspace-paths', repo)
  await page.fill('#workspace-name','교차 데스크톱 검증')
  await page.activate('#save-workspace')
  workspace = await until(() => page.run(() => new URL(location.href).searchParams.get('workspace')),'registered workspace')
  assert.match(workspace,/^[a-f0-9]{32}$/)
  const engines = await until(async () => {
    const rows = (await json('/api/engines')).engines
    return rows.find(r => r.engine === 'codex')?.version === '0.169.1' &&
      rows.find(r => r.engine === 'antigravity')?.version === '1.0.0-fixture' && rows
  },'fixture engines')
  assert.equal(engines.find(r=>r.engine === 'codex').auth_status,'authenticated')
}
async function hangul(selector, value) {
  await page.ready(selector)
  await page.run((s,text) => {
    const target = document.querySelector(s)
    target.focus()
    for (const [type,data] of [['compositionstart',''],['compositionupdate',text]]) {
      target.dispatchEvent(new CompositionEvent(type,{bubbles:true,data}))
    }
  },selector,value)
  await page.fill(selector,value)
  const committed = await page.run((s,text) => {
    const target = document.querySelector(s)
    target.dispatchEvent(new CompositionEvent('compositionend',{bubbles:true,data:text}))
    return target.value
  },selector,value)
  assert.equal(committed,value)
}
async function selectRun() {
  if (await page.run(() => new URL(location.href).searchParams.get('run')) !== runId)
    await page.activate('#run-list a[href*="' + runId + '"]')
  await until(() => page.run(id => new URL(location.href).searchParams.get('run') === id,runId),'active run ' + runId)
}
async function assign(kind) {
  const task = kind + '에서 저장한 테마와 첫 실행 기본값을 확인해 주세요.'
  await page.activate('.seat[data-person="coordinator"]')
  await hangul('#message',task)
  await page.check('#allow-edits')
  await page.check('#overnight-goal')
  await page.activate('#send')
  await page.ready('#assignment-receipt')
  await page.activate('#conversation .close-button')
  const matching = (await json('/api/rooms/' + workspace)).runs.filter(run => run.task === task)
  assert.equal(matching.length,1)
  runId = matching[0].id
  await selectRun()
  const run = await detail()
  assert.equal(run.assignment.roles.developer.engine,'codex')
  assert.equal(run.assignment.roles.reviewer.engine,'antigravity')
  setStep(kind + ' assignment ' + runId)
  const review = '#brief-questions [data-action="plan"][data-run="' + runId + '"]'
  await page.activate(review)
  await page.check('#meeting-inputs input[type="radio"]')
  await page.activate('#approve-plan')
  const question = await until(async () => (await requests()).find(row => row.kind === 'product_decision' && row.status === 'pending'),'product decision',90000)
  assert.match(question.prompt,/테마/)
  return {task,question}
}
async function readRecords() {
  return page.run(async (workspace,run) => {
    const read = async suffix => {
      const response = await fetch('/api/runs/' + run + suffix)
      if (!response.ok) throw Error(suffix + ' ' + response.status)
      return response.json()
    }
    const identity = await (await fetch('/api/desktop-runtime')).json()
    const detail = (await read('')).run
    const goal = (await read('/goal')).goal
    const board = await (await fetch('/api/rooms/' + workspace + '/briefing')).json()
    return {journalId:identity.journal_id,bootId:identity.boot_id,workspaceId:detail.workspace_id,runId:detail.id,
      task:detail.task,goalId:goal.run_id,questions:(await read('/inbox')).requests,directions:(await read('/directions')).directions,
      boardCandidates:board.candidates.filter(row => row.run_id === run),candidates:goal.candidates,checks:goal.checks,
      events:goal.events,state:goal.state,meeting:detail.meeting,review:detail.review_output}
  },workspace,runId)
}
async function renderResult(label,candidate) {
  setStep(label + ' candidate and conversation')
  await page.size(390)
  let candidateAction = '#brief-review [data-action="candidate"][data-run="' + runId + '"]'
  // The newest candidate is visible. Older candidates are tucked into an accessible details element.
  if (await page.run(s => Boolean(document.querySelector(s)?.closest('details:not([open])')),candidateAction)) {
    await page.activate('#brief-review .brief-more > summary')
  }
  await page.activate(candidateAction)
  const fold = '[data-goal-attempt="' + candidate.attempt + '"][data-digest="' + candidate.digest + '"][open]'
  await until(() => page.run(s => document.activeElement === document.querySelector(s + ' [data-goal-diff]'),fold),'focused candidate diff')
  const width = await page.run(() => ({width:innerWidth,scroll:document.documentElement.scrollWidth}))
  assert.ok(width.width <= 430 && width.scroll <= width.width,JSON.stringify(width))
  await page.key('Enter')
  await until(() => page.run(s => [...document.querySelectorAll(s + ' > pre')].some(el=>el.getClientRects().length && el.textContent.includes('settings.py')),fold),'visible candidate patch')
  await page.screenshot(join(evidence,label + '-candidate.png'))
  await page.size(1100)
  await page.activate('.seat[data-person="developer"]')
  await page.activate('#conversation [data-view="technical"]')
  const panel = '#evidence-developer'
  if (!await page.run(s => document.querySelector(s)?.open,panel)) await page.activate(panel + ' > summary')
  await until(() => page.run(s => [...document.querySelectorAll(s + ' .office-markdown h1,' + s + ' .office-markdown h2,' + s + ' .office-markdown h3,' + s + ' .office-markdown h4')].some(h => h.textContent === '변경 근거'),panel),'rendered Markdown heading')
  assert.equal(await page.run(s => document.querySelector(s + ' li code')?.textContent,panel),'theme')
  await page.screenshot(join(evidence,label + '-markdown.png'))
  await page.activate('#conversation [data-view="simple"]')
  await page.activate('#conversation [data-jump="meeting"]')
  const revision = '#conversation details[data-revision="1"]'
  if (!await page.run(s => document.querySelector(s)?.open,revision)) await page.activate(revision + ' > summary')
  const flow = revision + ' .plan-flow'
  if (!await page.run(s => document.querySelector(s + ' svg'),flow)) await page.activate(flow + ' button')
  await page.ready(flow + ' svg[role="img"]')
  await page.screenshot(join(evidence,label + '-mermaid.png'))
  await page.activate('#conversation .close-button')
  return width
}
async function journey(first,second) {
  await open(first)
  if (!workspace) await register()
  const {task,question} = await assign(first)
  const before = await readRecords()
  assert.equal(before.workspaceId,workspace)
  assert.equal(before.runId,runId)
  await close()
  assert.equal((await studio.running()).bootId,runtime.bootId)
  await open(second)
  await selectRun()
  const resumed = await readRecords()
  assert.deepEqual(resumed,before,'Handoff changed the pending run')
  setStep(second + ' follow-up and product decision')
  await page.activate('.seat[data-person="developer"]')
  const followup = '기본값 테스트도 결과에 남겨 주세요.'
  await hangul('#message',followup)
  await page.activate('#send')
  const queued = await until(async () => (await directions()).find(row => row.message === followup && row.delivery === 'queued'),'queued follow-up')
  await page.activate('#conversation .close-button')
  const answer = '#thread-answer-' + runId + '-' + question.id
  await page.activate('#brief-questions [data-action="answer"][data-request="' + question.id + '"]')
  await until(() => page.run(s => document.querySelector(s) === document.activeElement,answer),'product answer focus')
  await hangul(answer,'밝게')
  await page.activate('#office-thread [data-request="' + runId + ':' + question.id + '"] [data-control="answer"]')
  await until(async () => (await json(path('/goal'))).goal.recovery?.disposition === 'closed','finished goal',150000)
  const finished = await readRecords()
  assert.equal(finished.state,'READY_FOR_HUMAN_REVIEW')
  assert.equal(finished.goalId,runId)
  assert.equal(finished.task,task)
  assert.equal(finished.questions.length,1)
  assert.equal(finished.questions[0].id,question.id)
  assert.equal(finished.questions[0].answer,'밝게')
  assert.ok(finished.questions[0].delivered_at)
  assert.equal(finished.directions.length,1)
  assert.equal(finished.directions[0].id,queued.id)
  assert.equal(finished.directions[0].delivery,'delivered')
  assert.ok(finished.meeting.revisions.some(row => row.direction_id === queued.id))
  assert.equal(finished.candidates.length,1)
  const candidate = finished.candidates[0]
  assert.equal(candidate.review_status,'approved')
  assert.ok(candidate.files.includes('settings.py'))
  assert.ok(finished.checks.some(c => c.attempt === 0 && c.status === 'failed' &&
    c.failed_ids.some(id => id.includes('test_reopen_saved_theme'))))
  assert.ok(finished.checks.some(c => c.attempt === candidate.attempt && c.stage === 'full' && c.status === 'passed' &&
    c.exit_code === 0 && c.counts.passed === 2 && c.fixed_ids.some(id => id.includes('test_reopen_saved_theme'))))
  assert.equal(finished.boardCandidates[0]?.digest,candidate.digest)
  assert.match(finished.review,/VERDICT: APPROVED/)
  const width = await renderResult(first + '-to-' + second,candidate)
  await close()
  await open(first)
  await selectRun()
  const reopened = await readRecords()
  assert.deepEqual(reopened,finished,'Candidate, decision, verification or journal changed across clients')
  assert.match(await readFile(join(repo,'settings.py'),'utf8'),/return "light"/)
  await close()
  return {first,second,workspace,runId,questionId:question.id,directionId:queued.id,narrowViewport:width,
    checks:'native shell keyboard traversal, scripted DOM composition lifecycle, Hangul persisted, Markdown and Mermaid rendered',
    pending:before,completed:finished}
}
try {
  for (const [first,second] of [['electron','rust'],['rust','electron']]) {
    results.push(await journey(first,second))
    await writeFile(join(evidence,first + '-to-' + second + '.json'),JSON.stringify(results.at(-1),null,2))
  }
  const ids = (await json('/api/rooms/' + workspace)).runs.map(run=>run.id).sort()
  assert.deepEqual(ids,results.map(result=>result.runId).sort())
  console.log('Both handoffs passed on',process.platform,'·',evidence)
} catch (error) {
  failure = error
  const diagnostics = {step,runId,root,error:String(error),cause:String(error.cause ?? '')}
  if (runId) {
    diagnostics.detail = await detail().catch(() => null)
    diagnostics.goal = await json(path('/goal')).catch(() => null)
  }
  if (page) {
    diagnostics.screen = await page.run(() => ({url:location.href,text:document.body.innerText.slice(-5000)})).catch(() => null)
    await page.screenshot(join(evidence,'failure.png')).catch(() => {})
  }
  await writeFile(join(evidence,'failure.json'),JSON.stringify(diagnostics,null,2))
  console.error('Handoff failure during',step,'·',evidence)
} finally {
  await finishVerification(failure,close,() => studio.stop(runtime.bootId))
}
