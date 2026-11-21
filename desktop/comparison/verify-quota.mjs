import assert from 'node:assert/strict'
import {randomUUID} from 'node:crypto'
import {mkdir, readFile, writeFile} from 'node:fs/promises'
import {join} from 'node:path'
import {launch, until} from './drivers.mjs'
import {prepare} from './handoff-fixture.mjs'
import {finishVerification} from '../verify-lifecycle.mjs'

export async function verifyQuota(electron,rust,evidence) {
  await mkdir(evidence,{recursive:true})
  const {root,repo,studio} = await prepare(electron)
  const control = join(root,'bin')
  await writeFile(join(control,'quota-closed'),'test-only')
  const id = () => randomUUID().replaceAll('-','')
  let runtime, page, error
  async function api(path, data) {
    const response = await fetch(runtime.origin+path, data === undefined ? {} : {method:'POST',headers:{
      Origin:runtime.origin,'Content-Type':'application/json','X-Agent-Office':'studio'
    },body:JSON.stringify(data)})
    const result = await response.json()
    assert.ok(response.ok, path+' '+JSON.stringify(result))
    return result
  }
  async function close() {
    if (page) { const previous=page; page=null; await previous.close() }
  }
  async function open(kind,ws,run) {
    const directory=join(root,kind)
    await mkdir(directory,{recursive:true})
    const route='/?workspace='+ws+'&run='+run
    // Both fixtures already selected this exact journal. A Studio restart may assign a new port.
    const binding={origin:runtime.origin,journalId:runtime.journalId}
    await writeFile(join(directory,'connection.json'),JSON.stringify(kind==='rust' ? {...binding,page:route} :
      {connection:{mode:'attached',...binding},recent:{[runtime.origin]:route},remotes:[]}))
    page=await launch(kind,kind==='rust'?rust:electron,directory)
    await page.selectWindow(url=>url.startsWith(runtime.origin+'/'))
    if (await page.run(() => new URL(location.href).searchParams.get('workspace')) !== ws)
      await page.activate('#workspaces a[href*="'+ws+'"]')
    if (await page.run(() => new URL(location.href).searchParams.get('run')) !== run)
      await page.activate('#run-list a[href*="'+run+'"]')
    await page.ready('#work-usage > summary')
    await page.activate('#work-usage > summary')
    await page.ready('#usage-record h3')
  }
  try {
    runtime=await studio.start()
    const initial={...runtime}
    const ws=(await api('/api/workspaces',{name:'부재중 한도 검증',paths:[repo]})).workspace.id
    const room='/api/rooms/'+ws
    await until(async()=> (await api('/api/engines')).engines.filter(e=>['codex','antigravity'].includes(e.engine)).every(e=>e.probe_status==='ok'),
      'test provider metadata')
    const job={id:id(),allow_edits:true,delegation:false,goal:true,goal_criteria:['설정 복원'],
      role_engines:{developer:'codex',reviewer:'codex'},work_profile:{mode:'balanced',token_budget:500,
        roles:{reviewer:{model:'fixture-codex',effort:'high'}}},message:'설정 복원 오류를 재현하고 수정해 주세요.'}
    await api(room+'/assignments',job)
    const report=()=>api(room+'/usage/'+job.id)
    const goal=async rid=>(await api('/api/runs/'+rid+'/goal')).goal
    await until(async()=> (await goal(job.id)).recovery?.disposition==='waiting_quota','safe quota checkpoint',90000)
    const baseline=(await goal(job.id)).checks.filter(c=>c.attempt===0)
    assert.ok(baseline.some(c=>c.status==='failed'))
    const parked=await report()
    assert.equal(parked.usage.hold.kind,'quota_wait')
    await open('electron',ws,job.id)
    await page.screenshot(join(evidence,'waiting-electron.png'))
    await close()
    await open('electron',ws,job.id)
    assert.equal((await report()).usage.hold.kind,'quota_wait')
    await close()
    const independent=id()
    await api(room+'/assignments',{id:independent,message:'독립 상태 확인',allow_edits:false,delegation:false,
      role_engines:{developer:'antigravity'}})
    await until(async()=> (await api('/api/runs/'+independent)).run.state==='PLAN_ONLY','independent read completed')
    assert.equal((await report()).usage.hold.kind,'quota_wait')
    await studio.stop(runtime.bootId)
    runtime=await studio.start([],initial.journalId)
    assert.notEqual(runtime.bootId,initial.bootId)
    assert.equal(runtime.journalId,initial.journalId)
    await open('rust',ws,job.id)
    assert.equal((await report()).usage.hold.kind,'quota_wait')
    await writeFile(join(control,'quota-grant'),'provider confirms ordinary usage')
    await until(async()=> (await goal(job.id)).state==='READY_FOR_HUMAN_REVIEW','only the parked provider stage resumes',160000)
    const resumed=await report(), finished=await goal(job.id)
    assert.deepEqual(finished.checks.filter(c=>c.attempt===0),baseline)
    assert.equal(resumed.usage.decisions.filter(x=>x.action==='resumed').length,1)
    assert.equal((await readFile(join(control,'quota-edits'),'utf8')).trim().split(/\r?\n/).length,1)
    assert.equal(resumed.usage.total_tokens,200)
    assert.ok(resumed.usage.calls.some(c=>c.role==='reviewer' && c.requested_effort==='high' && c.actual_effort==='high'))
    await page.screenshot(join(evidence,'resumed-rust.png'))
    const capped={...job,id:id(),work_profile:{token_budget:50},message:'설정 복원 예산 검증'}
    await api(room+'/assignments',capped)
    await until(async()=> (await api(room+'/usage/'+capped.id)).usage.hold?.kind==='budget_stop','only one goal stops at its token budget',90000)
    await writeFile(join(evidence,'receipt.json'),JSON.stringify({workspace:ws,journal:runtime.journalId,bootBefore:initial.bootId,
      bootAfter:runtime.bootId,independent,run:job.id,parked,resumed,checks:finished.checks,capped:await api(room+'/usage/'+capped.id)},null,2))
  } catch (cause) { error=cause }
  finally { await finishVerification(error,close,async()=>{if(runtime) await studio.stop(runtime.bootId)}) }
}
