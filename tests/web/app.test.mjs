import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {JSDOM} from 'jsdom';

const html = await readFile(new URL('../../src/agent_office/web/index.html', import.meta.url), 'utf8');
const a = 'a'.repeat(32), b = 'b'.repeat(32), r = '1'.repeat(32), s = '2'.repeat(32);
const task = (id, run, state='PLAN_ONLY') => ({task_id:id, current_run_id:run, state, request_snapshot:'<img src=x onerror=alert(1)>',
  project:'project', workspace:'path', branch:'ao/test', developer_engine:'codex',reviewer_engine:'codex',
  attempts:1,max_attempts:2,outcome:'PLAN_ONLY',created_at:'2026-11-06T00:00:00Z',updated_at:'2026-11-06T00:00:00Z'});
const event = (seq, run) => ({seq,run_id:run,event_type:'STAGE_STARTED',timestamp:'2026-11-06T00:00:00Z',payload:{phase:'developer',state:'RUNNING'}});

async function until(check) {
  for (let i=0; i<200; i++) {if(check())return; await new Promise(resolve=>setTimeout(resolve,5));}
  assert.ok(check(), 'UI did not reach expected state');
}

async function mount(t, suffix='') {
  const dom = new JSDOM(html,{url:`http://127.0.0.1:8765/${suffix}`,pretendToBeVisual:true});
  const originals=Object.fromEntries(['window','document','location','history','fetch'].map(key=>[key,globalThis[key]]));
  Object.assign(globalThis,{window:dom.window,document:dom.window.document,location:dom.window.location,history:dom.window.history});
  const server={tasks:[task(a,r),task(b,s)],events:{[r]:[event(1,r),event(2,r)],[s]:[event(1,s)]},calls:[],fail:false,missing:null,hold:null,release:null,failEventsRun:null,
    failEngines:false,engines:{checked_at:'2026-11-06T00:00:00Z',platform:'test',engines:[
      {engine:'codex',label:'Codex',default_role:'developer',installed:true,version:'1.2.3',probe_status:'ok',auth_status:'authenticated',headless_support:true,execution_status:'not_checked'},
      {engine:'antigravity',label:'Antigravity',default_role:'reviewer',installed:true,version:'1.4.2',probe_status:'ok',auth_status:'unknown',headless_support:true,execution_status:'not_checked'}]}};
  globalThis.fetch=async path=>{
    server.calls.push(String(path));
    if(server.fail)throw new Error('offline');
    const url=new URL(path,dom.window.location.href);
    if(url.pathname==='/api/meta')return Response.json({database:'test.sqlite3'});
    if(url.pathname==='/api/tasks')return Response.json({tasks:server.tasks});
    if(url.pathname==='/api/engines')return server.failEngines ? Response.json({error:{code:'STORE_IO'}},{status:503}) : Response.json(server.engines);
    const id=url.pathname.split('/')[3];
    const selected=server.tasks.find(item=>item.task_id===id);
    if(!selected || server.missing===id)return Response.json({error:{code:'NOT_FOUND'}},{status:404});
    if(!url.pathname.endsWith('/events')){
      if(server.hold===id)return new Promise(resolve=>{server.release=()=>resolve(Response.json({task:selected}));});
      return Response.json({task:selected});
    }
    const run=url.searchParams.get('run_id');
    if(server.failEventsRun===run)return Response.json({error:{code:'STORE_IO'}},{status:503});
    const cursor=Number(url.searchParams.get('after_seq'));
    const events=(server.events[run]??[]).filter(item=>item.seq>cursor);
    return Response.json({task_id:id,run_id:run,events,next_after_seq:events.at(-1)?.seq??cursor,has_more:false});
  };
  t.after(async()=>{
    Object.defineProperty(dom.window.document,'hidden',{value:true,configurable:true});
    dom.window.document.dispatchEvent(new dom.window.Event('visibilitychange'));
    if(server.release)server.release();
    await new Promise(resolve=>setTimeout(resolve,10));
    dom.window.close();
    Object.assign(globalThis,originals);
  });
  await import(`../../src/agent_office/web/app.js?test=${Math.random()}`);
  const get=id=>dom.window.document.getElementById(id);
  await until(()=>get('connection').textContent.includes('기록 연결됨'));
  return {dom,server,get,refresh:()=>get('refresh').click()};
}

test('initial render is text-only and refresh preserves the selected element focus',async t=>{
  const {dom,server,get,refresh}=await mount(t);
  assert.equal(get('request').textContent,'<img src=x onerror=alert(1)>');
  assert.equal(dom.window.document.querySelector('img'),null);
  assert.equal(get('detail-badge').textContent,'계획만 작성');
  assert.equal(get('timeline').children.length,2);
  const button=dom.window.document.querySelector(`[data-task-id="${b}"]`);
  button.focus();
  server.tasks.reverse();
  server.events[r].push(event(3,r));
  refresh();
  await until(()=>get('timeline').children.length===3);
  assert.equal(dom.window.document.activeElement,button);
  assert.equal(dom.window.document.querySelector(`[data-task-id="${b}"]`),button);
});

test('late response for prior selection cannot replace current detail',async t=>{
  const {dom,server,get}=await mount(t);
  server.hold=b;
  dom.window.document.querySelector(`[data-task-id="${b}"]`).click();
  await until(()=>server.release);
  dom.window.document.querySelector(`[data-task-id="${a}"]`).click();
  server.hold=null; server.release();
  await until(()=>get('detail-content').hidden===false);
  assert.equal(get('task-id').textContent,a);
  assert.equal(new URL(dom.window.location.href).searchParams.get('task'),a);
});

test('missing task followed by recovery restores the same event timeline',async t=>{
  const {server,get,refresh}=await mount(t);
  server.missing=a; refresh();
  await until(()=>!get('error').hidden);
  assert.equal(get('timeline').children.length,0);
  server.missing=null; refresh();
  await until(()=>get('error').hidden);
  assert.equal(get('timeline').children.length,2);
});

test('URL selection and hidden pause work without executing actions',async t=>{
  const {dom,server,get,refresh}=await mount(t,`?task=${b}`);
  assert.equal(get('task-id').textContent,b);
  Object.defineProperty(dom.window.document,'hidden',{value:true,configurable:true});
  dom.window.document.dispatchEvent(new dom.window.Event('visibilitychange'));
  const count=server.calls.length; refresh();
  await new Promise(resolve=>setTimeout(resolve,20));
  assert.equal(server.calls.length,count);
  Object.defineProperty(dom.window.document,'hidden',{value:false,configurable:true});
  dom.window.document.dispatchEvent(new dom.window.Event('visibilitychange'));
  await until(()=>server.calls.length>count);
  assert.ok(server.calls.every(path=>path.startsWith('/api/')));
});

test('run change resets event cursor and old run data',async t=>{
  const {server,get,refresh}=await mount(t);
  const newRun='3'.repeat(32); server.tasks[0]={...server.tasks[0],current_run_id:newRun};
  server.events[newRun]=[event(1,newRun)]; refresh();
  await until(()=>get('run-id').textContent===newRun && get('timeline').children.length===1);
  assert.ok(server.calls.some(path=>path.includes(`run_id=${newRun}&after_seq=0`)));
});

test('failed new-run events never show old-run events beside new detail',async t=>{
  const {server,get,refresh}=await mount(t);
  const newRun='4'.repeat(32);
  server.tasks[0]={...server.tasks[0],current_run_id:newRun};
  server.events[newRun]=[event(1,newRun)]; server.failEventsRun=newRun;
  refresh(); await until(()=>!get('error').hidden);
  assert.equal(get('run-id').textContent,newRun);
  assert.equal(get('timeline').children.length,0);
  assert.equal(get('event-count').textContent,'0개');
  server.failEventsRun=null; refresh();
  await until(()=>get('error').hidden);
  assert.equal(get('timeline').children.length,1);
});

test('selected status transitions and reconnect announce once, not every poll',async t=>{
  const {dom,server,get,refresh}=await mount(t);
  server.tasks[0]={...server.tasks[0],state:'RUNNING',outcome:null}; refresh();
  await until(()=>get('detail-badge').textContent==='개발 중');
  assert.ok(get('announcer').textContent.includes('개발 중'));
  server.tasks[0]={...server.tasks[0],state:'PLAN_ONLY',outcome:'PLAN_ONLY'}; refresh();
  await until(()=>get('detail-badge').textContent==='계획만 작성');
  assert.ok(get('announcer').textContent.includes('계획만 작성'));
  const mutations=[];
  const observer=new dom.window.MutationObserver(changes=>mutations.push(...changes));
  observer.observe(get('announcer'),{childList:true,subtree:true,characterData:true});
  const before=server.calls.length; refresh();
  await until(()=>server.calls.length>=before+4);
  await new Promise(resolve=>setTimeout(resolve,10));
  assert.equal(mutations.length,0); observer.disconnect();
  server.fail=true; refresh(); await until(()=>!get('error').hidden);
  server.fail=false; refresh(); await until(()=>get('error').hidden);
  assert.ok(get('announcer').textContent.includes('복구'));
});

test('two initial engine cards separate metadata auth and actual verification',async t=>{
  const {dom,server,get,refresh}=await mount(t);
  const cards=dom.window.document.querySelectorAll('#engine-cards article');
  assert.equal(cards.length,2);
  assert.ok(cards[0].textContent.includes('Codex') && cards[0].textContent.includes('로그인 확인'));
  assert.ok(cards[1].textContent.includes('Antigravity') && cards[1].textContent.includes('로그인 미확인'));
  assert.ok([...cards].every(card=>card.textContent.includes('실제 작업 미검증')));
  server.engines.engines[1]={...server.engines.engines[1],label:'<img src=x onerror=alert(1)>',auth_status:'not_authenticated'};
  refresh();await until(()=>get('engine-cards').textContent.includes('로그인 안 됨'));
  assert.equal(get('engine-cards').querySelector('img'),null);
  assert.ok(get('engine-cards').textContent.includes('<img src=x onerror=alert(1)>'));
});

test('engine-only failure does not hide tasks and unchanged snapshot recovery clears warning',async t=>{
  const {server,get,refresh}=await mount(t);
  server.failEngines=true; refresh();
  await until(()=>get('engine-time').textContent.includes('갱신하지 못'));
  assert.ok(get('announcer').textContent.includes('진단'));
  assert.equal(get('detail-content').hidden,false);
  assert.equal(get('error').hidden,true);
  server.failEngines=false; refresh();
  await until(()=>get('engine-time').textContent.startsWith('시작 시 진단'));
  assert.ok(get('announcer').textContent.includes('복구'));
});
