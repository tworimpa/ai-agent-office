import assert from 'node:assert/strict'
import {execFileSync} from 'node:child_process'
import {mkdir, readFile, realpath, writeFile} from 'node:fs/promises'
import {join, resolve} from 'node:path'
import {launch, until} from './drivers.mjs'
import {prepare} from './handoff-fixture.mjs'
import {sha256} from './journal-receipt.mjs'
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
const composition = []
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
  await page.size(1366,768)
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
  await page.fill(selector,'')
  await page.run(s => {
    const target = document.querySelector(s)
    target.focus()
    const observed = []
    for (const type of ['compositionstart','compositionupdate','compositionend']) {
      target.addEventListener(type,e => {
        observed.push([e.type,e.isTrusted])
        target.dataset.handoffComposition = JSON.stringify(observed)
      },{once:true})
    }
  },selector)
  const ibus = !page.compose && process.env.OFFICE_TEST_IBUS === '1' && value === '밝게'
  if (ibus) {
    // The wrapper gives this test its own X display, D-Bus and keyboard engine.
    const ids = execFileSync('xdotool',['search','--onlyvisible','--name','AI Agent Office'],{encoding:'utf8'}).trim().split(/\s+/)
    assert.equal(ids.length,1,'Expected exactly one fixture window on the private display')
    execFileSync('xdotool',['windowfocus','--sync',ids[0]])
    execFileSync('ibus',['engine','hangul'])
    assert.equal(execFileSync('ibus',['engine'],{encoding:'utf8'}).trim(),'hangul')
    execFileSync('xdotool',['type','--clearmodifiers','--delay','80','qkfrrp']) // 밝게 on a two-set keyboard
    execFileSync('xdotool',['key','Right'])
    await until(() => page.run((s,text) => document.querySelector(s).value === text,selector,value),'IBus committed 밝게')
    execFileSync('ibus',['engine','xkb:us::eng'])
  } else if (page.compose) await page.compose(value)
  else {
    // Outside the native IBus check, cover the application's composition event handling.
    await page.run((s,text) => {
      const el = document.querySelector(s)
      for (const [type,data] of [['compositionstart',''],['compositionupdate',text]])
        el.dispatchEvent(new CompositionEvent(type,{bubbles:true,data}))
    },selector,value)
    await page.fill(selector,value)
    await page.run((s,text) => document.querySelector(s).dispatchEvent(
      new CompositionEvent('compositionend',{bubbles:true,data:text})),selector,value)
  }
  const {committed,observed} = await page.run(s => {
    const target = document.querySelector(s)
    return {committed:target.value,observed:JSON.parse(target.dataset.handoffComposition)}
  },selector)
  assert.equal(committed,value)
  if (ibus) {
    assert.ok(observed.some(([type]) => type === 'compositionend'),'IBus did not commit the Hangul answer')
  } else {
    assert.deepEqual(observed.map(([type])=>type),['compositionstart','compositionupdate','compositionend'])
    assert.ok(observed.slice(0,2).every(([,trusted])=>trusted === Boolean(page.compose)))
  }
  const method = ibus ? 'IBus Hangul two-set X11 keyboard + WebKitGTK' :
    page.compose ? 'Chromium IME engine' : 'scripted WebKit DOM events and text entry'
  composition.push({selector,method,observed})
}
async function selectRun() {
  if (await page.run(() => new URL(location.href).searchParams.get('run')) !== runId)
    await page.activate('#run-list a[href*="' + runId + '"]')
  await until(() => page.run(id => new URL(location.href).searchParams.get('run') === id,runId),'active run ' + runId)
}
async function briefingViews(tag,action) {
  const views = []
  for (const [width,height] of [[1366,768],[390,844]]) {
    await page.size(width,height)
    await page.reload()
    await page.ready('#brief-' + (action === 'answer' ? 'questions' : 'review') + ' [data-action="' + action + '"][data-run="' + runId + '"]')
    await page.run(() => window.scrollTo(0,0))
    const observation = await page.run(() => {
      const position = selector => [...document.querySelectorAll(selector)].map(el => {
        const {x,y,width,height} = el.getBoundingClientRect()
        const center = document.elementFromPoint(x + width / 2,y + height / 2)
        return {run:el.dataset.run,action:el.dataset.action,x,y,width,height,
          insideViewport:width > 0 && height > 0 && x >= 0 && y >= 0 && x + width <= innerWidth && y + height <= innerHeight,
          hitTarget:el.contains(center)}
      })
      return {width:innerWidth,height:innerHeight,scrollWidth:document.documentElement.scrollWidth,
        questions:position('#brief-questions [data-action="answer"]'),candidates:position('#brief-review [data-action="candidate"]')}
    })
    assert.equal(observation.width,width)
    assert.equal(observation.height,height)
    assert.ok(observation.scrollWidth <= width)
    const group = action === 'answer' ? observation.questions : observation.candidates
    assert.ok(group.some(item=>item.run === runId && item.insideViewport && item.hitTarget),'Active briefing action is outside the first viewport')
    await page.screenshot(join(evidence,tag + '-briefing-' + width + 'x' + height + '.png'))
    views.push(observation)
  }
  return views
}
async function assign(kind, representative) {
  const task = representative ? 'Windows에서 재실행하면 설정이 사라지는 문제를 재현하고 수정해줘. 관련 테스트와 독립 리뷰를 거쳐 아침에 검토할 후보를 남겨줘.' :
    kind + '에서 저장한 테마와 첫 실행 기본값을 확인해 주세요.'
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
  if (!representative) {
    const review = '#brief-questions [data-action="plan"][data-run="' + runId + '"]'
    await page.activate(review)
    await page.check('#meeting-inputs input[type="radio"]')
    await page.activate('#approve-plan')
  } else {
    assert.equal(run.assignment.work_profile.mode,'auto')
    assert.equal(run.assignment.work_profile.roles.developer.model,'fixture-codex')
    assert.equal(run.assignment.planning_mode,'direct')
  }
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
async function renderResult(label,candidate,representative) {
  setStep(label + ' candidate and conversation')
  const briefing = await briefingViews(label + '-ready','candidate')
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
  await page.size(1366,768)
  await page.activate('.seat[data-person="developer"]')
  await page.activate('#conversation [data-view="technical"]')
  const panel = '#evidence-developer'
  if (!await page.run(s => document.querySelector(s)?.open,panel)) await page.activate(panel + ' > summary')
  await until(() => page.run(s => [...document.querySelectorAll(s + ' .office-markdown h1,' + s + ' .office-markdown h2,' + s + ' .office-markdown h3,' + s + ' .office-markdown h4')].some(h => h.textContent === '변경 근거'),panel),'rendered Markdown heading')
  assert.equal(await page.run(s => document.querySelector(s + ' li code')?.textContent,panel),'theme')
  await page.screenshot(join(evidence,label + '-markdown.png'))
  if (representative) {
    await page.activate('#character-sheet > summary')
    await until(() => page.run(() => document.querySelectorAll('#character-record .character-axes li').length === 6),'six evidence axes')
    await page.screenshot(join(evidence,label + '-character.png'))
    await page.activate('#conversation .close-button')
    for (const [w,h] of [[1366,768],[390,844]]) {
      await page.size(w,h)
      await page.run(() => document.querySelector('#work-usage > summary').focus())
      if (!await page.run(() => document.querySelector('#work-usage').open)) await page.key('Enter')
      await until(() => page.run(() => document.querySelector('#usage-record').textContent.includes('fixture-codex')),'usage receipt')
      await page.screenshot(join(evidence,label + '-usage-' + w + '.png'))
    }
    return {width,briefing}
  }
  await page.activate('#conversation [data-view="simple"]')
  await page.activate('#conversation [data-jump="meeting"]')
  const revision = '#conversation details[data-revision="1"]'
  if (!await page.run(s => document.querySelector(s)?.open,revision)) await page.activate(revision + ' > summary')
  const flow = revision + ' .plan-flow'
  if (!await page.run(s => document.querySelector(s + ' svg'),flow)) await page.activate(flow + ' button')
  await page.ready(flow + ' svg[role="img"]')
  await page.screenshot(join(evidence,label + '-mermaid.png'))
  await page.activate('#conversation .close-button')
  return {width,briefing}
}
async function journey(first,second,representative=false) {
  composition.length = 0
  await open(first)
  const userAgents = {[first]: await page.run(() => navigator.userAgent)}
  if (!workspace) await register()
  const {task,question} = await assign(first,representative)
  const before = await readRecords()
  assert.equal(before.workspaceId,workspace)
  assert.equal(before.runId,runId)
  assert.ok(['RUNNING','WAITING_USER'].includes(before.state),before.state)
  await close()
  assert.equal((await studio.running()).bootId,runtime.bootId)
  await open(second)
  userAgents[second] = await page.run(() => navigator.userAgent)
  await selectRun()
  const pendingViews = await briefingViews(first + '-to-' + second + '-pending','answer')
  await page.size(1366,768)
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
  const sendAnswer = '#office-thread [data-request="' + runId + ':' + question.id + '"] [data-control="answer"]'
  await page.key('Tab')
  await until(() => page.run(s => document.querySelector(s) === document.activeElement,sendAnswer),'Tab from the answer to its submit button')
  await page.key('Enter')
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
  if (!representative) assert.ok(finished.meeting.revisions.some(row => row.direction_id === queued.id))
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
  const rendered = await renderResult(representative ? 'morning' : first + '-to-' + second,candidate,representative)
  const metrics = () => page.run(async (ws,run) => (await fetch('/api/rooms/'+ws+'/usage/'+run)).json(),workspace,runId)
  const usage = representative ? await metrics() : undefined
  if (representative) {
    assert.equal(usage.profile.mode,'auto')
    assert.ok(usage.usage.calls.some(call => call.role==='developer' && call.requested_model==='fixture-codex' && call.actual_model==='fixture-codex-routed'))
    assert.ok(usage.usage.calls.some(call => call.role==='reviewer' && call.engine==='antigravity'))
    assert.ok(usage.usage.total_tokens>0)
    assert.ok(usage.usage.rate_limits.some(limit => limit.used_percent===18))
  }
  await close()
  await open(first)
  await selectRun()
  const reopened = await readRecords()
  if (representative) assert.deepEqual(await metrics(), usage, 'Desktop journal usage changed across clients')
  assert.deepEqual(reopened,finished,'Candidate, decision, verification or journal changed across clients')
  assert.match(await readFile(join(repo,'settings.py'),'utf8'),/return "light"/)
  await close()
  return {platform:process.platform,first,second,userAgents,workspace,runId,questionId:question.id,directionId:queued.id,narrowViewport:rendered.width,
    pendingViews,candidateViews:rendered.briefing,
    composition:[...composition], checks:'native shell keyboard traversal, Hangul persisted, Markdown and Mermaid rendered',
    pending:before,completed:finished,usage}
}
try {
  for (const [first,second] of [['electron','rust'],['rust','electron']]) {
    results.push(await journey(first,second))
    await writeFile(join(evidence,first + '-to-' + second + '.json'),JSON.stringify(results.at(-1),null,2))
  }
  results.push(await journey('electron','rust',true))
  await writeFile(join(evidence,'morning-work-profile.json'),JSON.stringify(results.at(-1),null,2))
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
const journalFiles = []
for (const name of ['tasks.sqlite3','tasks.sqlite3.studio.sqlite3']) {
  const file = join(studio.directory,name)
  journalFiles.push({file,sha256:await sha256(file)})
}
await writeFile(join(evidence,'journal-receipt.json'),JSON.stringify({
  journal:runtime.journalId, runs:results.map(r => r.runId), files:journalFiles
},null,2))
