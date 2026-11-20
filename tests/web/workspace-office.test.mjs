import assert from 'node:assert/strict';
import test from 'node:test';
import {base, hosts, mount, remote, room, run, runId, runPath, settings, skillId, until, workspace} from './workspace-fixture.mjs';

test('pasted code stays out of room, portrait and dialog titles while surrounding prose remains available', async t => {
  const task = '설정을 확인하세요.\n\n```js\nconst saved = option;\n```\n\n재접속도 확인하세요.';
  const row = {...run, task};
  const {get, person} = mount(t, path => {
    if (path === base) return room([row]);
    if (path === runPath) return {run: row};
  }, {query: '?workspace=' + workspace.id + '&run=' + runId});
  await until(() => get('ledger-heading').textContent === '설정을 확인하세요.');
  person('developer').click();
  assert.equal(get('chat-context').textContent, '설정을 확인하세요.');
  assert.doesNotMatch(person('developer').title, /const saved/);
  assert.match(get('chat-log').textContent, /재접속도 확인하세요/);
  assert.equal(get('chat-log').querySelector('details').open, false);
});

test('recovery status shows the interrupted stage diagnostic before a previous successful answer', async t => {
  const row = {...run, state:'RECOVERY_REQUIRED', outcome:'INTERRUPTED', developer_output:'수정 완료',
    test_output:'Test command timed out after 300 seconds'};
  const {get, person} = mount(t, path => {
    if (path === base) return room([row]);
    if (path === runPath) return {run: row};
  }, {query:'?workspace='+workspace.id+'&run='+runId});
  await until(() => get('ledger-label').textContent === '복구 필요');
  person('developer').click();
  await until(() => get('chat-log').textContent.includes('timed out'));
  assert.doesNotMatch(get('chat-log').textContent, /수정 완료/);
});

test('coordinator assignment uses room defaults and sends a linked follow-up without creating a separate run', async t => {
  let assigned = null, direction = null, mode = 'RUNNING';
  const message = '<img src=x onerror=alert(1)> 로그인 검사';
  const {get, person, calls, submit, window} = mount(t, (path, body) => {
    if (path === base) return room(assigned ? [{...assigned, state: mode}] : []);
    if (path === base + '/assignments') {
      assert.match(body.id, /^[a-f0-9]{32}$/);
      assert.deepEqual(body, {id: body.id, message, meeting: true, allow_edits: true, delegation: null, indices: [0]});
      assigned = {...run, id: body.id, task: body.message};
      return {run: assigned};
    }
    if (assigned && path === '/api/runs/' + assigned.id) return {run: {...assigned, state: mode, developer_output: mode === 'PLAN_ONLY' ? '조회 결과' : ''}};
    if (assigned && path === '/api/runs/' + assigned.id + '/directions') {
      if (body) {
        assert.deepEqual(body, {id: body.id, role: 'developer', attempt: 1, message: 'Windows에서도 확인해 주세요.'});
        assert.match(body.id, /^[a-f0-9]{32}$/);
        direction = {...body, created_at: run.created_at, delivery: 'queued'};
        return {direction};
      }
      return {directions: direction ? [direction] : []};
    }
  });
  await until(() => get('room-title').textContent === workspace.name);
  person('coordinator').click();
  assert.equal(get('conversation').open, true);
  assert.match(get('run-delegation').options[0].textContent, /조정자가 결정/);
  get('message').value = message;
  get('allow-edits').checked = true;
  get('assignment-folders').querySelector('input').checked = true;
  submit('chat-form');
  await until(() => get('chat-log').textContent.includes('서진에게 작업을 맡겼습니다.'));
  assert.ok(get('chat-log').textContent.includes(message));
  assert.equal(get('chat-log').querySelector('img'), null);
  assert.equal(new URLSearchParams(window.location.search).get('run'), assigned.id);
  assert.equal(get('chat-form').hidden, true);
  assert.equal(get('assignment-receipt').hidden, false);
  assert.equal(get('receipt-task').textContent, message);
  assert.equal(window.document.activeElement, get('receipt-owner'));
  assert.doesNotMatch(get('chat-log').textContent, /검토 미확인|테스트 미확인/);
  submit('chat-form');
  get('receipt-owner').click();
  await until(() => !get('send').disabled);
  assert.equal(get('chat-context').textContent, message);
  get('message').value = 'Windows에서도 확인해 주세요.';
  submit('chat-form');
  await until(() => get('chat-log').textContent.includes('전달 대기'));
  direction = {...direction, delivery: 'delivered'};
  mode = 'PLAN_ONLY';
  get('refresh').click();
  await until(() => get('chat-log').textContent.includes('전달됨') && get('ledger-label').textContent === '조회 완료');
  assert.match(get('chat-log').textContent, /조회 결과/);
  assert.equal(get('ledger-result').hidden, true);
  assert.equal(calls.filter(call => call.path === base + '/assignments').length, 1);
});

test('a lost assignment receipt preserves the exact request ID, choices and message across reload', async t => {
  let posted, attempts = 0, storedRun;
  const attemptsRoute = (path, body) => {
    if (path === base) return room(storedRun ? [storedRun] : []);
    if (path === base + '/assignments') {
      if (++attempts === 1) {
        posted = body;
        assert.equal(body.review_plan, true);
        assert.deepEqual(body.role_engines, {reviewer: 'gemini'});
        assert.deepEqual(body.test_files, ['tests/test_review_regressions.py']);
        storedRun = {...run, id: body.id, task: body.message, allow_edits: body.allow_edits,
          assignment: {...run.assignment, test_files: body.test_files}};
        throw new Error('lost after journal write');
      }
      assert.deepEqual(body, posted);
      if (attempts === 2) return Response.json({error: {code: 'NOT_FOUND', message: '등록된 경로가 없습니다.'}}, {status: 404});
      return {run: storedRun};
    }
    if (path === '/api/runs/' + storedRun?.id) return {run: storedRun};
  };
  const first = mount(t, attemptsRoute);
  await until(() => first.get('room-title').textContent === workspace.name);
  first.person('coordinator').click();
  first.get('message').value = '저널 경로 설명을 보완해줘';
  first.get('allow-edits').click();
  first.get('review-plan').click();
  first.get('assignment-reviewer-engine').value = 'gemini';
  assert.equal(first.get('assignment-test-scope').hidden, false);
  first.get('test-files').value = 'tests/test_review_regressions.py';
  first.get('run-delegation').value = 'false';
  first.submit('chat-form');
  await until(() => first.get('chat-note').textContent.includes('같은 요청을 다시 보내세요'));
  const storage = Object.fromEntries(Object.keys(first.window.sessionStorage).map(key => [key, first.window.sessionStorage.getItem(key)]));
  first.close();

  const second = mount(t, attemptsRoute, {storage});
  await until(() => second.get('room-title').textContent === workspace.name);
  second.person('coordinator').click();
  assert.equal(second.get('message').value, posted.message);
  assert.equal(second.get('message').readOnly, true);
  assert.equal(second.get('run-delegation').value, 'false');
  assert.equal(second.get('review-plan').checked, true);
  assert.equal(second.get('review-plan').disabled, true);
  assert.equal(second.get('assignment-reviewer-engine').value, 'gemini');
  assert.equal(second.get('assignment-reviewer-engine').disabled, true);
  assert.equal(second.get('test-files').value, posted.test_files[0]);
  assert.equal(second.get('test-files').disabled, true);
  assert.equal(second.get('send').textContent, '같은 요청 다시 보내기');
  second.submit('chat-form');
  await until(() => second.get('chat-note').textContent.includes('같은 요청을 다시 보내세요'));
  assert.equal(second.get('message').readOnly, true);
  second.submit('chat-form');
  await until(() => second.get('chat-note').textContent === '작업을 배정했습니다.');
  assert.equal(second.get('message').readOnly, false);
  assert.equal(second.window.sessionStorage.length, 0);
  second.get('chat-view-controls').querySelector('[data-jump="tests"]').click();
  await until(() => second.get('evidence-tests')?.textContent.includes(posted.test_files[0]));
  assert.equal(attempts, 3);
  second.get('receipt-new').click();
  assert.equal(second.get('assignment-receipt').hidden, true);
  assert.equal(second.get('chat-form').hidden, false);
  assert.equal(second.get('message').value, '');
  assert.doesNotMatch(second.get('chat-log').textContent, /저널 경로 설명/);
  assert.equal(second.get('chat-evidence').hidden, true);
});

test('receipt follows the accepted queued run even when the developer portrait is busy on another run', async t => {
  let reserved, started = false;
  const accepted = () => ({...run, id: reserved, task:'두 번째 업무', state: started ? 'RUNNING' : 'QUEUED',
    attempts:started ? 1 : 0});
  const {get, person, submit, window} = mount(t, (path, body) => {
    if (path === base) return room(reserved ? [run, accepted()] : [run]);
    if (path === runPath) return {run};
    if (path === base + '/assignments') { reserved = body.id; return {run:accepted()}; }
    if (path === '/api/runs/' + reserved) return {run:accepted()};
  });
  await until(() => get('ledger-label').textContent === '작업 중');
  person('coordinator').click();
  get('message').value = '두 번째 업무';
  submit('chat-form');
  await until(() => !get('assignment-receipt').hidden);
  get('receipt-owner').click();
  assert.equal(get('chat-context').textContent, '두 번째 업무');
  assert.equal(new URL(window.location.href).searchParams.get('run'), reserved);
  assert.equal(get('chat-form').hidden, true);
  started = true;
  get('refresh').click();
  await until(() => !get('chat-form').hidden && !get('send').disabled);
  assert.equal(get('chat-note').textContent, '');
});

test('meeting review retains a draft through polling and sends the product choice and edited plan to the selected remote run', async t => {
  const plan = {
    outcome: '원인을 고칩니다.', scope: ['기존 경로\n유지'], rationale: ['기존 설정  '],
    tasks: [{id: 't1', owner: 'developer', description: '수정', depends_on: [], folders: ['src']},
      {id: 't2', owner: 'reviewer', description: '독립 검토', depends_on: ['t1']}],
    criteria: [{criterion: '재현 검증', verification: '테스트를 실행합니다.', test_files: ['tests/test_main.py']}],
    questions: [{question: '기본 동작을 고르세요.', options: ['자동', '수동']}],
    evidence: {revision: '4'.repeat(40), paths: ['src/main.py']},
  };
  const version = {revision: 1, plan, source: 'coordinator', reason: '첫 회의', verification: [], answers: {}};
  let meeting = {run_id: runId, status: 'planning', revision: 0, contributions: [
    {role: 'developer', engine: 'codex', text: '<img src=x> 경로 확인\n\n~~~py\nprint(1)\n~~~', created_at: run.created_at}],
    revisions: [], choices: [], answers: {}, plan: null, verification: []};
  const prefix = '/api/hosts/' + remote;
  let posted, detailReads = 0;
  const lines = {developer: '파일 경로를 확인했습니다.', coordinator: '계획 의견을 모으고 있습니다.', reviewer: '검토 중'};
  const view = () => ({...run, lines, meeting});
  const {get, person, submit, window, calls} = mount(t, (path, body) => {
    if (path === prefix + '/rooms/' + workspace.id) return room([{...run, lines}]);
    if (path === prefix + '/runs/' + runId) { detailReads++; return {run: view()}; }
    if (path === prefix + '/runs/' + runId + '/meeting/decision') {
      posted = body;
      meeting = {...meeting, status: 'approved', revision: 2, plan: body.plan, answers: body.answers,
        revisions: [version, {...version, revision: 2, source: 'operator', plan: body.plan, approved_by: 'operator'}],
        decision: {id: body.id, revision: body.revision}, work: [
          {revision: 2, task_id: 't1', engine: 'codex', result: '별도 작업 트리에서 병렬 실행을 시작했습니다.', created_at: run.created_at},
          {revision: 2, task_id: 't1', engine: 'codex', result: '주 작업 트리에 반영: src/main.py', created_at: run.created_at},
        ]};
      return {meeting};
    }
  }, {query: '?host=' + remote + '&workspace=' + workspace.id + '&run=' + runId});
  await until(() => get('room-view-controls').querySelector('[data-jump="meeting"]'));
  assert.equal(get('meeting').hidden, true);
  get('room-view-controls').querySelector('[data-jump="meeting"]').click();
  assert.match(get('meeting-phase').textContent, /논의 중/);
  assert.equal(get('meeting-decision').hidden, true);
  assert.match(get('meeting-record').textContent, /<img src=x>/);
  assert.equal(get('meeting-record').querySelector('img'), null);
  assert.equal(person('coordinator').querySelector('.seat-line').textContent, lines.coordinator);
  assert.equal(person('developer').querySelector('.seat-line').textContent, lines.developer);
  person('developer').click();
  assert.ok(get('conversation').contains(get('meeting-record')));
  get('chat-view-controls').querySelector('[data-view="simple"]').click();
  get('chat-view-controls').querySelector('[data-jump="meeting"]').click();
  assert.equal(window.document.activeElement, get('meeting-heading'));
  const contribution = () => get('meeting-record').querySelector('.contribution details');
  const wrap = () => contribution().querySelector('[data-wrap-key]');
  contribution().open = true;
  wrap().click();
  wrap().focus();
  meeting = {...meeting, contributions:[...meeting.contributions, {role:'reviewer', engine:'codex', text:'검토 의견'}],
    status: 'awaiting_review', revision: 1, revisions: [version], plan,
    choices: [{id: 'q1', ...plan.questions[0]}], verification: [{criterion: '재현 검증', status: 'not_verified', evidence: ''}]};
  get('refresh').click();
  await until(() => !get('meeting-decision').hidden && !get('approve-plan').disabled);
  assert.equal(contribution().open, true);
  assert.equal(wrap().getAttribute('aria-pressed'), 'true');
  assert.equal(window.document.activeElement, wrap());
  submit('meeting-decision');
  assert.equal(posted, undefined);
  const editor = get('meeting-outcome');
  editor.closest('details').open = true;
  editor.value = '수정된 결과 <script>문자열</script>';
  editor.focus();
  const seen = detailReads;
  get('refresh').click();
  await until(() => detailReads > seen);
  assert.equal(window.document.activeElement, editor);
  assert.equal(get('meeting-outcome').value, editor.value);
  get('meeting-choice-0-1').click();
  submit('meeting-decision');
  await until(() => get('meeting-phase').textContent.includes('계획 확정'));
  assert.deepEqual(posted, {id: posted.id, revision: 1, plan: {...plan, outcome: editor.value}, answers: {q1: '수동'}});
  assert.match(posted.id, /^[a-f0-9]{32}$/);
  assert.equal(get('meeting-decision').hidden, true);
  assert.ok(get('meeting-record').textContent.includes('사용자 선택 · 수동'));
  assert.ok(get('meeting-record').textContent.includes('계획 2판'));
  assert.ok(get('meeting-record').textContent.includes('tests/test_main.py'));
  assert.match(get('meeting-record').textContent, /병렬 작업 실행 기록.*t1.*별도 작업 트리.*주 작업 트리에 반영: src\/main.py/);
  assert.equal(get('meeting-record').querySelector('script'), null);
  assert.equal(calls.filter(call => call.body).length, 1);
  assert.equal(new URLSearchParams(window.location.search).get('run'), runId);
});

test('legacy remote execution links find the workspace and retain the last progress during a disconnect', async t => {
  let disconnected = false;
  const target = '/api/hosts/' + remote;
  const {get, calls, person, window} = mount(t, path => {
    if (path === target + '/runs/' + runId) return {run};
    if (path === target + '/rooms/' + workspace.id) {
      return disconnected ? Response.json({}, {status: 503}) : room();
    }
  }, {query: '?host=' + remote + '&run=' + runId + '#detail'});
  await until(() => person('developer').querySelector('.seat-line').textContent === run.lines.developer);
  assert.equal(get('host').value, remote);
  assert.equal(get('versions-link').search, '?host=' + remote);
  assert.equal(new URLSearchParams(window.location.search).get('workspace'), workspace.id);
  assert.equal(get('detail-link').pathname, '/studio');
  assert.equal(get('detail-link').search, '?host=' + remote + '&run=' + runId);
  assert.ok(calls.every(call => call.path === '/api/hosts' || call.path.startsWith(target)));
  disconnected = true;
  get('refresh').click();
  await until(() => get('connection').textContent.includes('마지막 기록'));
  assert.equal(person('developer').querySelector('.seat-line').textContent, run.lines.developer);
  assert.equal(get('settings').disabled, true);
  disconnected = false;
  get('refresh').click();
  await until(() => get('connection').textContent === '원격 연구실 연결됨');
});

test('remote assignment uses the planning protocol only when the room advertises it', async t => {
  const prefix = '/api/hosts/' + remote;
  for (const supported of [false, true]) {
    let posted;
    const {get, person, submit} = mount(t, (path, body) => {
      if (path === prefix + '/rooms/' + workspace.id) return supported ? {...room([]), meeting_supported: true} : room([]);
      if (path === prefix + '/rooms/' + workspace.id + '/assignments') {
        posted = body;
        return {run: {...run, id: body.id}};
      }
      if (path === prefix + '/runs/' + posted?.id) return {run: {...run, id: posted.id}};
    }, {query: '?host=' + remote + '&workspace=' + workspace.id});
    await until(() => get('room-title').textContent === workspace.name);
    person('coordinator').click();
    assert.equal(get('review-option').hidden, !supported);
    assert.equal(get('assignment-engines').hidden, !supported);
    get('message').value = '수정 범위를 조사해 주세요.';
    submit('chat-form');
    await until(() => posted);
    assert.deepEqual(posted, {id: posted.id, message: '수정 범위를 조사해 주세요.', allow_edits: false, delegation: null,
      ...(supported ? {meeting: true} : {})});
  }
});

test('returning to a workspace prioritizes a waiting meeting and reconciles an unacknowledged decision by ID', async t => {
  const plan = {outcome: '확인', scope: ['범위'], tasks: [{id: 'T1', owner: 'developer', description: '진단', depends_on: []}],
    criteria: [{criterion: '판정', verification: '코드 확인'}], rationale: ['기존 기준'], questions: [], evidence: {revision: '1'.repeat(40), paths: []}};
  const gateId = '2'.repeat(32);
  const waitingRun = {...run, id: gateId, task: '응답할 작업'};
  let meeting = {run_id: gateId, status: 'awaiting_review', revision: 1, plan, contributions: [], choices: [],
    answers: {}, verification: [], revisions: []};
  let disconnected = false;
  const path = '/api/runs/' + gateId;
  const route = (url, body) => {
    if (url === base) return room([run, {...waitingRun, meeting_status: meeting.status, pending_count: Number(meeting.status === 'awaiting_review')}]);
    if (url === runPath) return {run};
    if (url === path) return disconnected ? Response.json({}, {status: 503}) : {run: {...waitingRun, meeting}};
    if (url === path + '/meeting/decision') {
      assert.deepEqual(body.plan, plan);
      assert.deepEqual(body.answers, {});
      meeting = {...meeting, status: 'approved', decision: {id: body.id, revision: 1}};
      disconnected = true;
      throw new Error('lost after journal write');
    }
  };
  const first = mount(t, route);
  await until(() => !first.get('meeting-decision').hidden && !first.get('approve-plan').disabled);
  assert.equal(first.get('ledger-heading').textContent, waitingRun.task);
  assert.match(first.get('run-list').textContent, /계획 확인 대기/);
  first.get('run-list').querySelector('a').click();
  await until(() => first.get('ledger-heading').textContent === run.task);
  first.get('inbox').click();
  await until(() => first.get('request-log').textContent.includes(waitingRun.task));
  first.get('request-log').querySelector('button').click();
  await until(() => first.window.document.activeElement === first.get('meeting-heading'));
  first.submit('meeting-decision');
  await until(() => first.get('detail-status').textContent.includes('최신 상태를 확인할 수 없습니다.'));
  const query = first.window.location.search;
  const storage = Object.fromEntries(Object.entries(first.window.sessionStorage));
  assert.equal(Object.keys(storage).length, 1);
  first.close();
  disconnected = false;
  const second = mount(t, route, {query, storage});
  await until(() => second.get('meeting-note').textContent === '계획 결정을 기록했습니다.');
  assert.equal(second.get('ledger-heading').textContent, waitingRun.task);
  assert.equal(second.get('meeting-decision').hidden, true);
  assert.equal(second.window.sessionStorage.length, 0);
  assert.equal(second.calls.some(call => call.body), false);
});

test('changed plan semantics clear old folder and test links unless explicitly retained or replaced', async t => {
  const task = {owner: 'developer', description: '기존 모듈 수정', folders: ['src/original'], depends_on: []};
  const criterion = {criterion: '기존 동작', verification: '기존 검사', test_files: ['tests/test_old.py']};
  const plan = {outcome: '동작 확인', scope: ['src'], rationale: ['호환성'], questions: [],
    tasks: [{...task, id:'A'}, {...task, id:'B'}], criteria: [criterion, {...criterion, criterion:'추가 동작'}],
    evidence: {revision: '7'.repeat(40), paths: []}};
  let meeting = {run_id: runId, status: 'awaiting_review', revision: 1, plan, contributions: [], choices: [], answers: {}, verification: [], revisions: []};
  let posted;
  const {get, submit, window} = mount(t, (path, body) => {
    if (path === base) return room();
    if (path === runPath) return {run: {...run, meeting}};
    if (path === runPath + '/meeting/decision') {
      posted = body.plan;
      meeting = {...meeting, plan: posted, status: 'approved', decision:{revision:1,id:body.id}};
      return {meeting};
    }
  });
  await until(() => !get('meeting-decision').hidden && !get('approve-plan').disabled);
  function fill(name, value) {
    const control = get('meeting-' + name);
    control.value = value;
    control.dispatchEvent(new window.Event('input', {bubbles:true}));
  }
  fill('scope', '새 범위');
  const keep = get('meeting-decision').elements.namedItem('retain-folders-1');
  assert.equal(keep.parentElement.hidden, false);
  keep.click();
  fill('criterion-0', '변경한 완료 기준');
  fill('verification-1', '새 파일로 검증');
  fill('tests-1', 'tests/test_new.py');
  submit('meeting-decision');
  await until(() => posted);
  assert.deepEqual(posted.tasks.map(row => row.folders), [[], ['src/original']]);
  assert.deepEqual(posted.criteria.map(row => row.test_files), [[], ['tests/test_new.py']]);
});

test('inbox distinguishes user-facing decisions, technical questions and tool approvals', async t => {
  const request = (kind, index) => ({
    id: String(index).repeat(32), kind, run_id: runId, role: 'developer', engine: 'codex',
    prompt: kind === 'product_decision' ? '<svg onload=alert(1)> 동작을 바꿀까요?' : '요청 내용', status: 'pending',
  });
  const requests = [request('question', 1), request('product_decision', 2), request('permission', 3)];
  const {get} = mount(t, (path, body) => {
    if (path === base) return room([{...run, pending_count: requests.filter(row => row.status === 'pending').length}]);
    if (path === runPath) return {run};
    if (path === base + '/inbox') return {requests: requests.filter(row => row.status === 'pending')};
    if (path === runPath + '/inbox') return {requests};
    if (path === runPath + '/inbox/' + requests[1].id + '/decision') {
      assert.deepEqual(body, {action: 'answer', answer: '기존 동작 유지'});
      requests[1] = {...requests[1], status: 'decided', decision: 'answer', answer: body.answer};
      return {request: requests[1]};
    }
  });
  await until(() => get('inbox-count').textContent === '3');
  get('inbox').click();
  await until(() => get('request-log').querySelectorAll('article').length === 3);
  const rows = Array.from(get('request-log').querySelectorAll('article'));
  assert.deepEqual(rows.map(row => row.querySelector('h3').textContent), ['기술 질문', '제품 동작 결정', '권한 승인 요청']);
  assert.equal(rows[1].querySelector('svg'), null);
  assert.equal(rows[2].querySelector('textarea'), null);
  assert.equal(rows[2].querySelector('button').textContent, '이번 요청만 승인');
  rows[1].querySelector('textarea').value = '기존 동작 유지';
  rows[1].querySelector('button').click();
  await until(() => get('request-log').textContent.includes('사용자 결정'));
  assert.ok(get('request-log').textContent.includes('기존 동작 유지'));
});

test('workspace settings persist role names, engines, skills and delegation independently of a running assignment', async t => {
  let current = settings;
  const {get, person, submit} = mount(t, (path, body) => {
    if (path === '/api/skills') return {skills: [{id: skillId, name: '테스트 규칙', version: 2}]};
    if (path === base && body) {
      current = body;
      return {settings: current};
    }
    if (path === base) return room([run], current);
    if (path === runPath) return {run};
  });
  await until(() => !get('settings').disabled);
  get('settings').click();
  await until(() => get('developer-skills').textContent.includes('테스트 규칙'));
  get('developer-name').value = '민수';
  get('developer-engine').value = 'gemini';
  get('room-delegation').checked = false;
  submit('settings-form');
  await until(() => !get('room-settings').open);
  assert.deepEqual(current, {
    delegation: false,
    developer: {name: '민수', engine: 'gemini', skills: [skillId]},
    reviewer: settings.reviewer,
  });
  assert.equal(person('developer').querySelector('.seat-name').textContent, '서진');
  person('coordinator').click();
  assert.match(get('run-delegation').options[0].textContent, /나에게 질문/);
});

test('developer chat preserves the draft and focus, then records the answer in the same conversation', async t => {
  let question = {id: '1'.repeat(32), kind: 'question', run_id: runId, role: 'developer', engine: 'codex',
    prompt: '어떤 테스트를 먼저 실행할까요?', status: 'pending'};
  let directions = [];
  const permission = {id: '2'.repeat(32), kind: 'permission', run_id: runId, role: 'developer', engine: 'codex',
    prompt: '이 폴더의 작업을 허용할까요?', status: 'pending'};
  const answer = '핵심 검증부터 실행해 주세요.';
  const {get, person, window} = mount(t, (path, body) => {
    if (path === base) return room([{...run, pending_count: 2}]);
    if (path === runPath) return {run};
    if (path === runPath + '/directions') return {directions};
    if (path === runPath + '/inbox') return {requests: [question, permission]};
    if (path === base + '/inbox') return {requests: [question, permission].filter(row => row.status === 'pending')};
    if (path === runPath + '/inbox/' + question.id + '/decision') {
      assert.deepEqual(body, {action: 'answer', answer});
      question = {...question, decision: 'answer', answer, status: 'decided'};
      return {request: question};
    }
  });
  await until(() => get('room-title').textContent === workspace.name);
  person('developer').click();
  const draft = () => get('chat-log').querySelector('[data-control="draft"]');
  await until(() => draft() && !draft().disabled);
  draft().value = answer;
  draft().dispatchEvent(new window.Event('input', {bubbles: true}));
  draft().focus();
  draft().setSelectionRange(2, 4);
  directions = [{id: '3'.repeat(32), role: 'developer', message: 'Windows 검증 추가', delivery: 'delivered'}];
  get('refresh').click();
  await until(() => get('chat-log').textContent.includes('Windows 검증 추가'));
  assert.equal(draft().value, answer);
  assert.equal(window.document.activeElement, draft());
  assert.equal(draft().selectionStart, 2);
  assert.equal(draft().selectionEnd, 4);
  get('chat-log').querySelector('[data-control="answer"]').click();
  await until(() => get('chat-request-note').textContent === '결정을 기록했습니다.' &&
    get('chat-log').textContent.includes(answer) && !draft());
  assert.equal(get('conversation').open, true);
  assert.equal(get('requests').open, false);
  const permissionAction = get('chat-log').querySelector('[data-control="inbox"]');
  assert.ok(permissionAction);
  assert.equal(get('chat-log').querySelector('[data-control="approve"]'), null);
  permissionAction.click();
  await until(() => get('request-log').querySelector('[data-control="approve"]'));
  assert.equal(window.document.activeElement.dataset.request, runId + ':' + permission.id);
  assert.equal(get('conversation').open, false);
  assert.equal(get('requests').open, true);
});

test('developer chat reads the latest decision state before sending an explicit answer', async t => {
  const question = {id: '1'.repeat(32), kind: 'product_decision', run_id: runId, role: 'developer', engine: 'codex',
    prompt: '기존 저장 흐름을 바꿀까요?', status: 'pending'};
  let changed = false;
  const {get, person, calls} = mount(t, path => {
    if (path === base) return room([{...run, pending_count: 1}]);
    if (path === runPath) return {run};
    if (path === runPath + '/inbox') return {requests: [changed ? {...question, prompt: '기존 저장 형식을 바꿀까요?'} : question]};
  });
  await until(() => get('room-title').textContent === workspace.name);
  person('developer').click();
  const button = () => get('chat-log').querySelector('[data-control="answer"]');
  await until(() => button() && !button().disabled);
  get('chat-log').querySelector('textarea').value = '현재 동작 유지';
  changed = true;
  button().click();
  await until(() => get('chat-request-note').textContent.includes('요청이 변경되었습니다.'));
  assert.equal(calls.some(call => call.path.endsWith('/decision')), false);
});

test('stopping an active remote run shows a stop request until termination is confirmed', async t => {
  let state = 'RUNNING';
  const prefix = '/api/hosts/' + remote;
  const {get, calls} = mount(t, (path, body) => {
    if (path === prefix + '/rooms/' + workspace.id) return room([{...run, state}]);
    if (path === prefix + '/runs/' + runId) return {run: {...run, state}};
    if (path === prefix + '/runs/' + runId + '/stop') {
      assert.deepEqual(body, {});
      state = 'STOP_REQUESTED';
      return {run: {...run, state}};
    }
  }, {query: '?host=' + remote + '&workspace=' + workspace.id + '&run=' + runId});
  await until(() => !get('stop-run').hidden && !get('stop-run').disabled);
  get('stop-run').click();
  await until(() => get('stop-note').textContent === '중지 요청을 접수했습니다.');
  assert.equal(get('stop-run').hidden, true);
  assert.equal(get('ledger-label').textContent, '중지 요청 중');
  state = 'CANCELED';
  get('refresh').click();
  await until(() => get('ledger-label').textContent === '중지됨');
  assert.equal(calls.filter(call => call.path.endsWith('/stop')).length, 1);
});

test('portrait chat exposes the evidence and delivery state of completed decisions', async t => {
  const receipt = (kind, id, status, fields = {}) => ({
    id: String(id).repeat(32), kind, run_id: runId, role: 'developer', engine: 'codex',
    prompt: '요청 ' + id, status, ...fields,
  });
  const revision = 'd'.repeat(40);
  const rows = [
    receipt('question', 1, 'delivered', {decision: 'answer', answer: '테스트 규칙을 사용하세요.', source: 'delegate',
      delegation_details: {specialist_engine: 'codex', verifier_engine: 'antigravity',
        evidence: {path: 'docs/test.md', line: 7, quote: '테스트 규칙은 <b>원문</b>', revision}}}),
    receipt('permission', 2, 'decided', {decision: 'reject'}),
    receipt('product_decision', 3, 'expired'),
  ];
  const {get, person} = mount(t, path => {
    if (path === base) return room();
    if (path === runPath) return {run};
    if (path === runPath + '/inbox') return {requests: rows};
  });
  await until(() => get('room-title').textContent === workspace.name);
  person('developer').click();
  get('chat-view-controls').querySelector('[data-view="collaboration"]').click();
  await until(() => get('chat-log').textContent.includes('대리 결정 근거'));
  const log = get('chat-log');
  assert.match(log.textContent, /조정자 대리 결정/);
  assert.match(log.textContent, /에이전트에게 전달됨/);
  assert.match(log.textContent, /거절 · 결정 접수됨/);
  assert.match(log.textContent, /만료됨/);
  assert.ok(log.textContent.includes('docs/test.md 7행 · 기준 커밋 ' + revision));
  assert.ok(log.textContent.includes('테스트 규칙은 <b>원문</b>'));
  assert.match(log.textContent, /전문가 · Codex \/ 독립 검증자 · Antigravity/);
  assert.equal(log.querySelector('b'), null);
});

test('a selected developer run cannot mask another run actively assigned to the reviewer', async t => {
  const reviewId = 'e'.repeat(32);
  const reviewing = {...run, id: reviewId, task: '별도 검토 작업', state: 'REVIEWING',
    lines: {...run.lines, reviewer: '변경 사항을 검토하고 있습니다.'}};
  const {get, person, window} = mount(t, path => {
    if (path === base) return room([run, reviewing]);
    if (path === runPath) return {run};
    if (path === '/api/runs/' + reviewId) return {run: reviewing};
  });
  await until(() => get('room-title').textContent === workspace.name);
  assert.match(person('developer').textContent, /원인 확인/);
  assert.match(person('reviewer').textContent, /별도 검토 작업/);
  person('reviewer').click();
  assert.equal(get('chat-context').textContent, '별도 검토 작업');
  assert.equal(new URLSearchParams(window.location.search).get('run'), reviewId);
});

test('a deep link cannot read another workspace’s directions or issue inbox decisions', async t => {
  const outside = {...run, workspace_id: 'e'.repeat(32)};
  const ownRun = {...run, id: 'f'.repeat(32), pending_count: 1};
  const pending = {run_id: runId, id: '1'.repeat(32), role: 'developer', kind: 'permission', status: 'pending', prompt: '다른 작업실의 요청'};
  const ownPending = {...pending, run_id: ownRun.id, prompt: '이 작업실의 요청'};
  const {get, person, calls} = mount(t, path => {
    if (path === base) return room([ownRun]);
    if (path === runPath) return {run: outside};
    if (path === base + '/inbox') return {requests: [ownPending, pending]};
    if (path === '/api/inbox') return {requests: [pending]};
  }, {query: '?workspace=' + workspace.id + '&run=' + runId});
  await until(() => get('detail-status').textContent === '이 실행은 선택한 작업공간에 속하지 않습니다.');
  assert.equal(get('detail-link').hidden, true);
  assert.equal(get('stop-run').hidden, true);
  person('coordinator').click();
  assert.equal(get('conversation').open, true);
  assert.ok(!get('chat-log').textContent.includes(pending.prompt));
  get('conversation').close();
  get('inbox').click();
  await until(() => get('request-log').textContent.includes(ownPending.prompt));
  assert.ok(!get('request-log').textContent.includes(pending.prompt));
  assert.ok(!calls.some(call => call.path.startsWith(runPath + '/')));
  assert.ok(!calls.some(call => call.path === '/api/inbox'));
});

test('additional directions stay bound to the attempt visible when the portrait conversation opened', async t => {
  let attempt = 1;
  const {get, person, calls, submit} = mount(t, (path, body) => {
    if (path === base) return room([{...run, attempts: attempt}]);
    if (path === runPath) return {run: {...run, attempts: attempt}};
    if (path === runPath + '/directions' && body) {
      assert.equal(body.attempt, 2);
      return {direction: {...body, delivery: 'queued'}};
    }
  });
  await until(() => get('room-title').textContent === workspace.name);
  person('developer').click();
  await until(() => !get('send').disabled);
  get('message').value = '첫 시도용 지시';
  attempt = 2;
  get('refresh').click();
  await until(() => get('chat-note').textContent.includes('실행 시도가 바뀌었습니다.'));
  assert.equal(get('message').disabled, true);
  submit('chat-form');
  assert.ok(!calls.some(call => call.path.endsWith('/directions') && call.body));
  get('conversation').close();
  person('developer').click();
  await until(() => !get('send').disabled);
  assert.equal(get('message').value, '');
  get('message').value = '두 번째 시도에 확인할 지시';
  submit('chat-form');
  await until(() => calls.some(call => call.path.endsWith('/directions') && call.body?.attempt === 2));
});

test('a legacy run may show questions while withholding new additional directions', async t => {
  const legacy = {...run, assignment: null};
  const request = {id: '1'.repeat(32), run_id: runId, role: 'developer', engine: 'codex', kind: 'question',
    status: 'pending', prompt: '기존 실행에서 묻는 질문'};
  const {get, person} = mount(t, path => {
    if (path === base) return room([legacy]);
    if (path === runPath) return {run: legacy};
    if (path === runPath + '/inbox') return {requests: [request]};
  });
  await until(() => get('room-title').textContent === workspace.name);
  person('developer').click();
  await until(() => get('chat-log').querySelector('[data-control="answer"]')?.disabled === false);
  assert.equal(get('send').hidden, true);
  assert.match(get('chat-note').textContent, /작업실에서 새로 맡긴 작업/);
});

test('workspace history pages deduplicate against fresh polling and keep older links usable', async t => {
  const older = {...run, id: 'e'.repeat(32), task: '이전 작업', state: 'PLAN_ONLY'};
  const cursor = 'h-' + runId;
  const next = 'h-' + older.id;
  let latest = run;
  const {get, calls, window} = mount(t, path => {
    if (path === base) return {...room([latest]), next_cursor: cursor};
    if (path === base + '/history/' + cursor) {
      return {runs: [{...run, task: '겹치는 오래된 사본'}, older], next_cursor: next};
    }
    if (path === base + '/history/' + next) return {runs: [], next_cursor: null};
    if (path === runPath) return {run: latest};
    if (path === '/api/runs/' + older.id) return {run: older};
  });
  await until(() => !get('history-older').hidden && !get('history-older').disabled);
  get('history-older').click();
  await until(() => get('run-list').querySelectorAll('a').length === 2);
  assert.ok(!get('run-list').textContent.includes('겹치는 오래된 사본'));
  latest = {...latest, task: '최신 상태의 새 제목'};
  get('refresh').click();
  await until(() => get('run-list').textContent.includes(latest.task));
  assert.equal(get('run-list').querySelectorAll('a').length, 2);
  get('run-list').querySelectorAll('a')[1].click();
  await until(() => get('ledger-heading').textContent === older.task);
  assert.equal(new URLSearchParams(window.location.search).get('run'), older.id);
  get('history-older').click();
  await until(() => get('history-older').hidden);
  assert.deepEqual(calls.filter(call => call.path.includes('/history/')).map(call => call.path),
    [base + '/history/' + cursor, base + '/history/' + next]);
});

test('an in-flight history page never populates a different host’s workspace', async t => {
  const cursor = 'a-' + runId;
  const remoteRun = {...run, id: 'e'.repeat(32), task: '원격 작업'};
  const foreignPage = {...run, id: 'f'.repeat(32), task: '로컬 작업의 이전 기록'};
  const prefix = '/api/hosts/' + remote;
  let resolveHistory;
  const {get, window} = mount(t, path => {
    if (path === base) return {...room(), next_cursor: cursor};
    if (path === runPath) return {run};
    if (path === base + '/history/' + cursor) return new Promise(resolve => { resolveHistory = resolve; });
    if (path === prefix + '/rooms/' + workspace.id) return {...room([remoteRun]), next_cursor: null};
    if (path === prefix + '/runs/' + remoteRun.id) return {run: remoteRun};
  });
  await until(() => !get('history-older').hidden && !get('history-older').disabled);
  get('history-older').click();
  await until(() => resolveHistory);
  get('host').value = remote;
  get('host').dispatchEvent(new window.Event('change', {bubbles: true}));
  await until(() => get('run-list').textContent.includes(remoteRun.task));
  resolveHistory({runs: [foreignPage], next_cursor: null});
  await new Promise(resolve => setTimeout(resolve, 0));
  assert.equal(get('history-older').hidden, true);
  assert.equal(get('run-list').querySelectorAll('a').length, 1);
  assert.ok(!get('run-list').textContent.includes(foreignPage.task));
});

test('simple conversation orders directions, attributes the conclusion, and opens evidence without losing the reading position', async t => {
  let result = {...run, state: 'READY_FOR_HUMAN_REVIEW', outcome: 'APPROVED',
    developer_output: '담당자가 설정을 수정했습니다.\n\n## 상세 근거\n\n~~~py\nprint(3)\n~~~\n\n코드 뒤의 설명',
    test_output: '3 passed in 0.24s', review_output: 'VERDICT: APPROVED', changes: ' M src/main.py',
    diff: 'diff --git a/src/main.py b/src/main.py\n--- a/src/main.py\n+++ b/src/main.py\n@@ -1 +1 @@\n-old\n+new\n'};
  const directions = [
    {id: '2', role: 'developer', message: '두 번째 지시', created_at: '2026-11-19T03:00:00Z', delivery: 'queued'},
    {id: '1', role: 'developer', message: '첫 번째 지시', created_at: '2026-11-19T02:00:00Z', delivery: 'delivered'},
  ];
  const route = path => {
    if (path === base) return room([result]);
    if (path === runPath) return {run: result};
    if (path === runPath + '/directions') return {directions};
  };
  const {get, window, calls} = mount(t, route);
  const thread = get('office-thread'), picker = get('room-view-controls');
  await until(() => thread.textContent.includes('두 번째 지시'));
  assert.match(thread.textContent, /원인 확인.*첫 번째 지시.*두 번째 지시.*서진.*담당자가 설정을 수정했습니다\./s);
  assert.match(thread.textContent, /변경 파일 1개.*테스트 3개 통과.*최종 사용자 확인 필요/);
  assert.doesNotMatch(thread.textContent, /내부 출력|VERDICT|상세 근거/);
  assert.equal(get('ledger-result').hidden, true);
  const answer = [...thread.querySelectorAll('details')].find(item => item.firstElementChild.textContent === '답변 더 보기');
  answer.open = true;
  await until(() => answer.textContent.includes('코드 뒤의 설명'));
  const code = answer.querySelector('details');
  assert.equal(code.open, false);
  code.open = true;
  const source = () => picker.querySelector('[data-jump="changes"]');
  source().focus();
  thread.scrollTop = 31;
  source().click();
  const changes = get('evidence-changes');
  const firstFile = () => changes.querySelector('.office-diff details');
  assert.ok(changes.open && firstFile().open);
  firstFile().querySelector('[data-wrap-key]').click();
  firstFile().querySelector('summary').focus();
  result = {...result, diff: result.diff.replace('+new', '+adjusted')};
  get('refresh').click();
  await until(() => changes.textContent.includes('+adjusted'));
  assert.ok(firstFile().open);
  assert.equal(firstFile().querySelector('[data-wrap-key]').getAttribute('aria-pressed'), 'true');
  assert.equal(window.document.activeElement, firstFile().querySelector('summary'));
  picker.querySelector('[data-back]').click();
  assert.ok(thread.contains(answer) && answer.open && code.open);
  assert.equal(window.document.activeElement, source());
  assert.equal(thread.scrollTop, 31);
  picker.querySelector('[data-view="collaboration"]').click();
  const preferences = Object.fromEntries(Object.entries(window.localStorage));
  const reload = mount(t, route, {preferences});
  await until(() => reload.get('room-view-controls').querySelector('[data-view="collaboration"]')?.getAttribute('aria-pressed') === 'true');
  assert.equal(calls.concat(reload.calls).some(call => call.body), false);
});

test('each view exposes pending requests and permission scope, retaining a draft submitted from the room', async t => {
  const requests = ['permission', 'question', 'product_decision'].map((kind, i) => ({
    run_id: run.id, id: String(i + 1).repeat(32), kind, role: 'reviewer', engine: 'codex', status: 'pending',
    prompt: i === 0 ? '작업 폴더 C:/work 안 파일 수정' : '질문 ' + i}));
  const {get, window, person, calls} = mount(t, (path, body) => {
    if (path === base) return room();
    if (path === runPath) return {run};
    if (path === runPath + '/inbox') return {requests};
    if (path === base + '/inbox') return {requests};
    if (path === runPath + '/inbox/' + requests[1].id + '/decision') {
      assert.deepEqual(body, {action:'answer', answer:'요청함에서 수정'});
      requests[1] = {...requests[1], status:'decided', decision:'answer', answer:body.answer};
      return {request:requests[1]};
    }
  });
  const thread = get('office-thread'), picker = get('room-view-controls');
  const draft = () => thread.querySelector('textarea');
  await until(() => draft() && !draft().disabled);
  draft().value = '현재 설정 유지';
  draft().dispatchEvent(new window.Event('input', {bubbles:true}));
  for (const view of ['technical','collaboration','simple']) {
    picker.querySelector('[data-view="' + view + '"]').click();
    assert.match(thread.textContent, /권한 승인 요청.*C:\/work.*이번 요청 1회.*기술 질문.*제품 동작 결정/s);
    assert.equal(draft().value, '현재 설정 유지');
    assert.equal(draft().disabled, false);
    assert.ok(thread.querySelector('[data-control="inbox"]'));
  }
  person('developer').click();
  const chatDraft = get('chat-log').querySelector('textarea');
  assert.equal(chatDraft.value, '현재 설정 유지');
  chatDraft.value = '대화에서 수정';
  chatDraft.dispatchEvent(new window.Event('input', {bubbles:true}));
  get('conversation').close();
  assert.equal(draft().value, '대화에서 수정');
  get('inbox').click();
  await until(() => get('request-log').querySelector('textarea'));
  const inboxDraft = get('request-log').querySelector('textarea');
  assert.equal(inboxDraft.value, '대화에서 수정');
  inboxDraft.value = '요청함에서 수정';
  inboxDraft.dispatchEvent(new window.Event('input', {bubbles:true}));
  get('requests').close();
  assert.equal(draft().value, '요청함에서 수정');
  thread.querySelector('[data-control="answer"]').click();
  await until(() => thread.textContent.includes('결정 접수됨'));
  assert.equal(calls.filter(call => call.body).length, 1);
});

test('a late detail refresh cannot re-enable an offline question editor', async t => {
  let slow = false, resolveDetail;
  const row = {run_id:runId,id:'e'.repeat(32),kind:'question',role:'developer',status:'pending',prompt:'이 이름을 사용할까요?'};
  const {get} = mount(t, path => {
    if (path === base) return room();
    if (path === runPath) return slow ? new Promise(resolve => { resolveDetail = resolve; }) : {run};
    if (path === runPath + '/inbox') {
      if (slow) throw new Error('offline');
      return {requests:[row]};
    }
  });
  const editor = () => get('office-thread').querySelector('textarea');
  await until(() => editor() && !editor().disabled);
  slow = true;
  get('refresh').click();
  await until(() => resolveDetail && editor()?.disabled);
  const previous = editor();
  resolveDetail({run:{...run,diagnostic:'updated'}});
  await until(() => editor() && editor() !== previous);
  assert.ok(editor().disabled);
});

test('evidence facts distinguish failed, unverified, read-only and manual outcomes', async t => {
  let result = {...run, outcome: 'TEST_FAILED', state: 'FAILED', changes:'(Git preview unavailable)',
    test_output: 'FAILED tests/test_demo.py::example - AssertionError\n1 failed, 2 passed in 0.12s'};
  const {get} = mount(t, path => {
    if (path === base) return room([result]);
    if (path === runPath) return {run:result};
  });
  const thread = get('office-thread');
  await until(() => thread.textContent.includes('테스트 1개 실패, 2개 통과'));
  assert.match(thread.textContent, /AssertionError.*변경 파일 미확인.*검토 미실행/s);
  result = {...result, state:'PLAN_ONLY', outcome:'PLAN_ONLY', allow_edits:false, test_output:'', changes:'',
    meeting:{status:'approved', plan:{criteria:[{criterion:'화면 검토'}]}, revision:1, contributions:[],
      verification:[{criterion:'화면 검토',status:'not_verified',evidence:''}]}};
  get('refresh').click();
  await until(() => thread.textContent.includes('수동 확인 필요 1건'));
  assert.match(thread.textContent, /테스트 미실행.*검토 미실행/);
  result = {...run, state:'RECOVERY_REQUIRED', outcome:'INTERRUPTED', test_output:'개발자 설명: 75 passed in 0.1s', changes:' M x\n...[truncated]'};
  get('refresh').click();
  await until(() => thread.textContent.includes('복구 필요'));
  assert.match(thread.textContent, /변경 파일 미확인.*테스트 미확인.*검토 미확인/);
  assert.doesNotMatch(thread.textContent, /75개/);
  result = {...run, state:'FAILED', outcome:'CHANGES_REQUESTED', review_output:'VERDICT: CHANGES_REQUESTED\n\n다시 시작하면 데이터가 사라집니다.'};
  get('refresh').click();
  await until(() => thread.textContent.includes('데이터가 사라집니다.'));
  assert.doesNotMatch(thread.textContent, /VERDICT/);
  result = {...run, kind:'parallel', state:'FAILED', outcome:'DEVELOPER_FAILED', subtasks:[
    {state:'READY_FOR_HUMAN_REVIEW', outcome:'APPROVED'}, {state:'FAILED', outcome:'DEVELOPER_FAILED'}]};
  get('refresh').click();
  await until(() => thread.textContent.includes('하위 작업별 확인'));
  assert.match(thread.textContent, /하위 작업 검토 결과 확인 1건, 실패 1건/);
  assert.doesNotMatch(thread.textContent, /테스트 (통과|미실행)|검토 미실행/);
});
