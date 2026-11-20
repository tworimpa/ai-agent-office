import test from 'node:test';
import assert from 'node:assert/strict';
import {base, mount, room, run, runPath, until, workspace} from './workspace-fixture.mjs';

const policy = {
  active_seconds: 28800,
  max_attempts: 6,
  checks: [
    {
      stage: 'quick',
      host: 'local',
      argv: ['python', '-m', 'pytest', '-q', '-x'],
      timeout_seconds: 1800,
      pass_exit_code: 0
    },
    {stage: 'full', host: 'local', argv: ['python', '-m', 'pytest', '-q'], timeout_seconds: 7200, pass_exit_code: 0}
  ]
};
const check = {
  attempt: 0,
  stage: 'quick',
  argv: policy.checks[0].argv,
  host: 'fixture',
  cwd: '/test',
  started_at: '2026-11-21T00:00:00Z',
  elapsed_seconds: 2,
  exit_code: 1,
  status: 'failed',
  counts: {failed: 1},
  scope_changes: [],
  summary: 'test_restore: light != dark'
};
const candidate = {
  attempt: 1,
  status: 'passed',
  eligible: true,
  review_status: 'approved',
  digest: 'a'.repeat(64),
  hypothesis: '기본값 고정',
  change: '설정 읽기',
  files: ['settings.py']
};
const goal = {
  run_id: run.id,
  id: run.id,
  workspace_id: workspace.id,
  state: 'READY_FOR_HUMAN_REVIEW',
  request: '설정 복원',
  scope: ['settings.py'],
  criteria: ['다시 열 수 있음'],
  policy,
  checks: [check],
  candidates: [candidate],
  best_attempt: 1,
  adopted_attempt: null,
  next_action: '후보를 확인하세요.',
  manual_checks: ['Windows에서 직접 열기'],
  events: [],
  merge_status: 'not_recorded',
  deployment_status: 'not_recorded'
};
function fixture(route) {
  return (path, body) => {
    const value = route?.(path, body);
    if (value !== undefined) return value;
    if (path === base) return {...room([{...run, state: goal.state}]), goal_supported: true};
    if (path === runPath) return {run: {...run, state: goal.state}};
    if (path === runPath + '/goal') return {goal};
    if (path === base + '/goal-policy') return {policy};
    if (path === '/api/skills') return {skills: []};
  };
}

test('overnight settings save the user-selected scope to the workspace, separately from ordinary assignments', async (t) => {
  let saved, assignment;
  const {get, person, window, submit} = mount(
    t,
    fixture((path, body) => {
      if (path === base + '/goal-policy' && body) {
        saved = body;
        return {policy: body};
      }
      if (path === base + '/assignments' && body) {
        assignment = body;
        return {run: {...run, id: body.id}};
      }
    })
  );
  await until(() => !get('settings').disabled);
  get('settings').click();
  await until(() => !get('goal-policy-form').hidden);
  const argv = get('goal-check-fields').querySelector('[name="argv"]');
  argv.value = 'python\n-m\npytest\n-q\ntests/test_settings.py';
  submit('goal-policy-form');
  await until(() => Boolean(saved));
  assert.deepEqual(saved.checks[0].argv, ['python', '-m', 'pytest', '-q', 'tests/test_settings.py']);
  assert.equal(saved.active_seconds, 28800);
  get('room-settings').close();
  person('coordinator').click();
  assert.equal(get('goal-option').hidden, true);
  get('allow-edits').click();
  get('overnight-goal').click();
  get('goal-criteria').value = '저장한 설정 복원\n빈 설정 파일 처리';
  get('message').value = 'Windows 설정을 복원해 주세요.';
  submit('chat-form');
  await until(() => Boolean(assignment));
  assert.equal(assignment.goal, true);
  assert.equal(assignment.allow_edits, true);
  assert.equal(assignment.message, 'Windows 설정을 복원해 주세요.');
  assert.deepEqual(assignment.goal_criteria, ['저장한 설정 복원', '빈 설정 파일 처리']);
  assert.equal(window.document.querySelectorAll('#goal-policy-form form').length, 0);
});

test('reopened goal exposes baseline, candidate diff, manual evidence and guarded adoption', async (t) => {
  let adoption;
  const {get} = mount(
    t,
    fixture((path, body) => {
      if (path === runPath + '/goal/candidates/1') return {attempt: 1, diff: '<img src=x>\\n+restored'};
      if (path === runPath + '/goal/adopt') {
        adoption = body;
        return {run_id: run.id, ...body};
      }
    }),
    {query: '?run=' + run.id + '&workspace=' + workspace.id}
  );
  await until(() => !get('goal-report').hidden);
  const root = get('goal-record');
  assert.match(root.querySelector('[data-goal-section="baseline"]').textContent, /test_restore/);
  assert.match(root.querySelector('[data-goal-section="manual"]').textContent, /Windows에서 직접 열기/);
  const content = root.querySelector('[data-goal-attempt="1"]');
  assert.ok(content);
  assert.equal(content.querySelector('[data-goal-adopt]').hidden, true);
  content.querySelector('[data-goal-diff]').click();
  await until(() => !content.querySelector('[data-goal-adopt]').hidden);
  assert.match(content.querySelector('pre').textContent, /<img src=x>/);
  assert.equal(content.querySelector('img'), null);
  content.querySelector('[data-goal-adopt]').click();
  await until(() => Boolean(adoption));
  assert.equal(adoption.digest, candidate.digest);
  assert.equal(adoption.previous_digest, null);
  assert.match(adoption.id, /^[a-f0-9]{32}$/);
});

test('old host shows only supported controls and sends no goal request', async (t) => {
  const {calls, get, person} = mount(t, (path) => {
    if (path === base) return room();
    if (path === runPath) return {run};
  });
  await until(() => !get('settings').disabled);
  person('coordinator').click();
  get('allow-edits').click();
  assert.equal(get('goal-option').hidden, true);
  assert.equal(
    calls.some((call) => call.path.includes('/goal')),
    false
  );
});

test('workspace goal default can be opted out of on the individual edit assignment', async (t) => {
  let submission;
  const {get, person, submit} = mount(
    t,
    fixture((path, body) => {
      if (path === base) return {...room(), goal_supported: true, goal_default: true};
      if (path === base + '/assignments' && body) {
        submission = body;
        return {run: {...run, id: body.id}};
      }
    })
  );
  await until(() => !get('settings').disabled);
  person('coordinator').click();
  get('allow-edits').click();
  assert.equal(get('overnight-goal').checked, true);
  get('overnight-goal').click();
  get('message').value = '한 번만 실행해 주세요.';
  submit('chat-form');
  await until(() => Boolean(submission));
  assert.equal(submission.allow_edits, true);
  assert.equal(Object.hasOwn(submission, 'goal'), false);
});

test('attempt comparison shows the actual patch fallback and missing checks without enabling adoption', async (t) => {
  const second = {
    ...candidate,
    attempt: 2,
    digest: 'b'.repeat(64),
    status: 'scope_reduced',
    eligible: false,
    scope_changes: ['누락: tests/test_defaults.py']
  };
  const {get} = mount(
    t,
    fixture((path) => {
      if (path === runPath + '/goal') return {goal: {...goal, candidates: [candidate, second]}};
      if (path === runPath + '/goal/compare/1/2')
        return {
          run_id: run.id,
          from_attempt: 1,
          to_attempt: 2,
          diff: '-test_defaults',
          format: 'patches',
          truncated: true
        };
    })
  );
  await until(() => !get('goal-report').hidden);
  const row = get('goal-record').querySelector('[data-goal-attempt="2"]');
  row.querySelector('[data-goal-compare]').click();
  await until(() => row.textContent.includes('-test_defaults'));
  assert.match(row.textContent, /누락: tests\/test_defaults.py/);
  assert.match(row.textContent, /각 후보의 패치 비교 · 출력 일부만 표시/);
  assert.equal(row.querySelector('[data-goal-adopt]'), null);
});
