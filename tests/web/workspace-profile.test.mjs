import assert from 'node:assert/strict';
import test from 'node:test';
import {base, mount, room, run, runPath, until, workspace} from './workspace-fixture.mjs';

const journal = '9'.repeat(32);
const policy = {mode:'auto', roles:{developer:{model:null, effort:null}, reviewer:{}}, token_budget:null, on_limit:'wait', fallbacks:{developer:[], reviewer:[]}};
const catalog = {workspace_id:workspace.id, catalog:{engine:'codex', status:'available', models:[
  {id:'host-model', label:'호스트 모델', efforts:['low','medium','high'], is_default:true, default_effort:'medium'}],
  refreshed_at:'2026-11-20T08:00:00Z'}, capabilities:{
  codex:{model:'supported', effort:'supported', usage:'supported', limits:'supported'},
  antigravity:{model:'supported', effort:'supported', usage:'unknown', limits:'unknown'},
  cline:{model:'unsupported', effort:'unsupported', usage:'unknown', limits:'unknown'}}};
function fixture(override) {
  return (path, body) => {
    const result = override?.(path, body);
    if (result !== undefined) return result;
    if (path === base) return {...room(), journal_id:journal, work_profiles_supported:true, goal_supported:true};
    if (path === runPath) return {run};
    if (path === runPath + '/goal') return {goal:null};
    if (path === base + '/models') return catalog;
    if (path === base + '/work-policy') return {workspace_id:workspace.id, policy:body || policy};
    if (path === base + '/goal-policy') return {policy:{max_attempts:3, active_seconds:600, checks:[]}};
    if (path.endsWith('/skills')) return {skills:[]};
    if (path === base + '/assignments') return {run:{...run, id:body.id}};
  };
}

test('goal intake carries only explicit overrides, supports local model capability and keeps goal limits outside read-only jobs', async t => {
  const {get, person, window, calls, submit} = mount(t, fixture());
  await until(() => !get('settings').disabled);
  person('coordinator').click();
  await until(() => get('assignment-mode').options[0].textContent.includes('자동') && !get('assignment-developer-model').disabled);
  assert.equal(get('assignment-mode').value, '');
  assert.equal(get('assignment-reviewer-effort').disabled, false);
  assert.ok(get('assignment-budget').closest('fieldset').hidden);
  get('assignment-mode').value = 'deep';
  get('assignment-developer-model').value = 'host-model';
  get('assignment-developer-model').dispatchEvent(new window.Event('input'));
  get('assignment-developer-effort').value = 'high';
  get('allow-edits').click(); get('overnight-goal').click();
  get('assignment-budget').value = '3200';
  get('assignment-on-limit').value = 'stop';
  get('assignment-developer-override').value = 'custom';
  get('assignment-developer-override').dispatchEvent(new window.Event('change'));
  get('assignment-developer-override').parentElement.querySelector('button').click();
  const engine = get('assignment-developer-override').parentElement.querySelector('ol select');
  engine.value = 'cline'; engine.dispatchEvent(new window.Event('change'));
  assert.equal(engine.parentElement.querySelector('input[list]').disabled, true);
  get('message').value = '저장 워크플로 확인';
  submit('chat-form');
  await until(() => calls.some(call => call.path.endsWith('/assignments')));
  const body = calls.find(call => call.path.endsWith('/assignments')).body;
  assert.equal(body.goal, true);
  assert.deepEqual(body.work_profile, {mode:'deep', roles:{developer:{model:'host-model', effort:'high'}},
    token_budget:3200, on_limit:'stop', fallbacks:{developer:[{engine:'cline'}]}});
  assert.equal(calls.some(call => call.path.endsWith('/models') && call.body), false);
});

test('ordinary intake inherits automatically and a legacy room never reads or submits profile extensions', async t => {
  for (const legacy of [false,true]) {
    await t.test(legacy ? 'legacy' : 'supported', async t => {
      const {get, person, calls, submit} = mount(t, fixture(path => legacy && path === base ? room() : undefined));
      await until(() => !get('settings').disabled);
      person('coordinator').click();
      assert.equal(get('assignment-work-mode').hidden, legacy);
      get('message').value = '원인 확인';
      submit('chat-form');
      await until(() => calls.some(call => call.path.endsWith('/assignments')));
      const body = calls.find(call => call.path.endsWith('/assignments')).body;
      assert.equal(Object.hasOwn(body, 'work_profile'), false);
      if (legacy) assert.equal(calls.some(call => /\/(?:models|work-policy|characters|usage)(?:\/|$)/.test(call.path)), false);
    });
  }
});

test('meeting selection respects automatic host routing, older profile hosts and explicit plan review', async t => {
  for (const [native, confirm, expected] of [[true,false,'auto'],[false,false,false],[true,true,true]]) {
    await t.test(String(expected), async t => {
      const {get,person,calls,submit}=mount(t,fixture(path=>path===base ? {
        ...room(), journal_id:journal, work_profiles_supported:true,meeting_supported:true,quick_dispatch_supported:native
      } : undefined));
      await until(()=>!get('settings').disabled);
      person('coordinator').click();
      await until(()=>get('assignment-mode').options[0].textContent.includes('자동'));
      if (confirm) get('review-plan').click();
      get('message').value='읽기 전용 확인';
      submit('chat-form');
      await until(()=>calls.some(call=>call.path.endsWith('/assignments')));
      const body=calls.find(call=>call.path.endsWith('/assignments')).body;
      assert.equal(body.meeting,expected);
      assert.equal(Boolean(body.review_plan),confirm);
    });
  }
});

test('workspace policy persists model and fallback settings separately, and model refresh requires a click', async t => {
  const {get, calls, submit, window} = mount(t, fixture());
  await until(() => !get('settings').disabled);
  get('settings').click();
  await until(() => !get('policy-save').disabled && !get('policy-developer-model').disabled);
  get('work-policy').open = true;
  get('policy-mode').value = 'balanced';
  get('policy-reviewer-effort').value = 'high';
  get('policy-reviewer-alias').value = 'work-review';
  get('policy-budget').value = '4500';
  const refresh = [...get('policy-fields').querySelectorAll('button')].find(btn => btn.textContent.includes('목록 갱신'));
  refresh.click();
  await until(() => calls.some(call => call.path.endsWith('/models') && call.body));
  submit('work-policy-form');
  await until(() => get('policy-note').textContent.includes('저장했습니다'));
  const body = calls.find(call => call.path.endsWith('/work-policy') && call.body).body;
  assert.equal(body.mode, 'balanced');
  assert.deepEqual(body.roles.reviewer, {effort:'high', account_alias:'work-review'});
  assert.equal(body.token_budget, 4500);
  assert.equal(window.document.querySelectorAll('form form').length, 0);
});

test('portrait opens conversation first, six axes expose sample counts, unknown axes and a targeted evidence link', async t => {
  const directions = [{id:'a'.repeat(32), role:'developer', message:'검사 범위 추가', attempt:1, delivery:'delivered'}];
  const axes = ['분석','구현','검증','검토','프로젝트 이해','협업 인계'].map((label,i) => ({
    label, level:i === 1 ? 3 : null, samples:i === 1 ? 4 : 0,
    evidence:i === 1 ? [{run_id:run.id, section:'directions', direction_id:directions[0].id, label:'전달된 지시'}] : []}));
  const {get, person, window, calls} = mount(t, fixture(path => {
    if (path.endsWith('/characters/developer')) return {workspace_id:workspace.id, character:{role:'developer',name:'서진',specialty:'구현',days:90,axes,comparisons:[]}};
    if (path === runPath + '/directions') return {directions};
  }));
  await until(() => !get('settings').disabled);
  person('developer').click();
  assert.ok(get('conversation').open);
  assert.equal(get('character-sheet').open, false);
  assert.equal(calls.some(call => call.path.includes('/characters/')), false);
  get('character-sheet').open = true;
  await until(() => get('character-record').querySelector('svg'));
  assert.match(get('character-record').textContent, /최근 90일/);
  assert.match(get('character-record').textContent, /구현 · 3\/5표본 4건/);
  assert.equal(get('character-record').querySelectorAll('.character-axes li').length, 6);
  assert.equal(get('character-record').querySelectorAll('.radar-value').length, 1);
  get('character-record').querySelector('button').click();
  await until(() => !get('conversation').open && window.document.activeElement.dataset.receipt === directions[0].id);
  assert.match(window.document.activeElement.textContent, /검사 범위 추가/);
});

test('run reports four independent usage kinds with source, time and explicit zero versus unknown', async t => {
  const {get} = mount(t, fixture(path => {
    if (path === base + '/usage/' + run.id) return {workspace_id:workspace.id, run_id:run.id, profile:{mode:'deep', reason:'실패 재현'},
      budget:{active_used:0, active_seconds:1800, token_budget:6000, attempts_used:1, max_attempts:3},
      usage:{calls:[{role:'developer', engine:'codex', actual_model:'observed-model', output_tokens:0, context_used:null, context_window:4096,
        source:'turn.completed', observed_at:'2026-11-20T02:00:00Z'}], total_tokens:null, unknown_calls:1,
      rate_limits:[{engine:'codex', account_alias:'work', used_percent:0, source:'account/rateLimits/read', window_minutes:300}],
      decisions:[], hold:{reason:'재설정 확인 대기'}}};
  }));
  await until(() => !get('work-usage').hidden);
  get('work-usage').open = true;
  await until(() => get('usage-record').textContent.includes('observed-model'));
  const record = get('usage-record');
  assert.match(record.textContent, /출력 0.*turn.completed/s);
  assert.match(record.textContent, /점유 미확인 \/ 창 크기 4,096/);
  assert.match(record.textContent, /사용 0%.*재설정 미확인.*account\/rateLimits\/read/s);
  assert.match(record.textContent, /실제 작업 0초 \/ 1,800초.*토큰 한도 6,000/s);
  assert.ok(record.textContent.includes('재설정 확인 대기'));
});

test('return briefing pins a workspace and journal watermark across polls and links a new hold to usage evidence', async t => {
  const key = ['office-last-visit:local', journal, workspace.id].join(':');
  const snapshot = {workspace_id:workspace.id, observed_at:'2026-11-20T09:00:00Z', overlaps:[], decisions:[], candidates:[],
    work:[{run_id:run.id,task:run.task,state:'RUNNING',stage:'implement',priority:0,progress:'이전 진행',
      last_progress_at:'2026-11-20T06:00:00Z', usage_hold:{reason:'한도 확인 대기', at:'2026-11-20T08:00:00Z'}}]};
  const {get, calls, window} = mount(t, fixture(path => {
    if (path === base) return {...room(), journal_id:journal, work_profiles_supported:true, briefing_supported:true};
    if (path.endsWith('/briefing')) return snapshot;
    if (path === base + '/usage/' + run.id) return {workspace_id:workspace.id, run_id:run.id, budget:{},usage:{}};
  }), {preferences:{[key]:'2026-11-20T07:00:00Z'}});
  await until(() => get('brief-visit-rows').querySelector('button'));
  assert.equal(get('brief-visit-count').textContent, '1건');
  assert.doesNotMatch(get('brief-visit-rows').textContent, /이전 진행/);
  assert.equal(window.localStorage.getItem(key), snapshot.observed_at);
  get('brief-visit').open = true;
  const jump = get('brief-visit-rows').querySelector('button');
  jump.focus();
  get('refresh').click();
  await until(() => calls.filter(call => call.path.endsWith('/briefing')).length > 1);
  assert.equal(window.document.activeElement, jump);
  jump.click();
  await until(() => get('work-usage').open && window.document.activeElement === get('work-usage').querySelector('summary'));
});

test('shared visit uses a single entry receipt across polls and retries a lost response with its original id', async t => {
  const key = ['office-last-visit:local', journal, workspace.id].join(':');
  let writes = 0;
  const snapshot = {workspace_id:workspace.id, observed_at:'2026-11-20T09:00:00Z', overlaps:[], decisions:[], candidates:[],
    work:[{run_id:run.id,task:run.task,state:'RUNNING',stage:'implement',priority:0,progress:'호스트에서 남긴 검증',
      last_progress_at:'2026-11-20T08:15:00Z'}]};
  const {get, calls, window} = mount(t, fixture((path, body) => {
    if (path === base) return {...room(), journal_id:journal, work_profiles_supported:true, briefing_supported:true, shared_visits_supported:true};
    if (path.endsWith('/briefing')) return snapshot;
    if (path.endsWith('/visit')) {
      if (++writes === 1) throw Error('lost receipt');
      return {workspace_id:workspace.id,id:body.id,visited_at:snapshot.observed_at, previous_visit:'2026-11-20T08:00:00Z'};
    }
  }), {preferences:{[key]:'2026-11-20T08:45:00Z'}});
  await until(() => !get('brief-visit-retry').hidden && writes > 0);
  const id = calls.find(call => call.path === base + '/visit').body.id;
  assert.equal(window.sessionStorage.getItem(key + ':pending'), id);
  get('refresh').click();
  await until(() => calls.filter(call => call.path.endsWith('/briefing')).length > 1);
  assert.equal(writes, 1);
  get('brief-visit-retry').click();
  await until(() => get('brief-visit-rows').textContent.includes('호스트에서 남긴 검증'));
  assert.deepEqual(calls.filter(call => call.path.endsWith('/visit')).map(call => call.body.id), [id,id]);
  assert.equal(window.sessionStorage.getItem(key + ':pending'), null);
});
