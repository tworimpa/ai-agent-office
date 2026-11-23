import assert from 'node:assert/strict';
import test from 'node:test';
import {base, mount, room, run, runId, runPath, until} from './workspace-fixture.mjs';

function pollingClock() {
  const timers = new Map();
  let next = -1;
  return {
    timers,
    setupWindow(win) {
      const set = win.setTimeout.bind(win), clear = win.clearTimeout.bind(win);
      win.setTimeout = (callback, delay, ...args) => {
        if (![3000, 20000].includes(delay)) return set(callback, delay, ...args);
        timers.set(next, {delay, callback});
        return next--;
      };
      win.clearTimeout = id => { if (!timers.delete(id)) clear(id); };
    },
    tick() {
      assert.equal(timers.size, 1);
      const [[id, {callback}]] = timers;
      timers.delete(id);
      return callback();
    },
  };
}

for (const supported of [false, true]) {
  test('visible active refresh uses ' + (supported ? 'one bundled request' : 'compatible individual reads'), async t => {
    const clock = pollingClock();
    let row = {...run};
    const snapshot = () => ({...room([row]), bundle_supported: supported, goal_supported: true});
    const {window, calls, get} = mount(t, path => {
      if (path === base) return snapshot();
      if (path === runPath) return {run: row};
      if (path === runPath + '/goal') return {goal: null};
      if (path.startsWith(base + '/bundle/')) return {room: snapshot(), run: {run: row}, goal: {goal: null},
        inbox: {requests: []}, directions: {directions: []}};
    }, {setupWindow: clock.setupWindow});
    await until(() => clock.timers.size === 1);
    calls.length = 0;
    await clock.tick();
    assert.equal(calls.length, supported ? 1 : 5);
    if (supported) assert.equal(calls[0].path, base + '/bundle/' + runId + '/live');
    const visibility = hidden => {
      Object.defineProperty(window.document, 'hidden', {value: hidden, configurable: true});
      window.document.dispatchEvent(new window.Event('visibilitychange'));
    };
    visibility(true);
    calls.length = 0;
    window.dispatchEvent(new window.Event('online'));
    assert.equal(clock.timers.size, 0);
    assert.equal(calls.length, 0);
    row = {...row, state: 'PLAN_ONLY'};
    visibility(false);
    await until(() => clock.timers.size === 1);
    assert.equal([...clock.timers.values()][0].delay, 20000);
    assert.equal(get('room').hidden, false);
  });
}

test('active team children have individual, keyboard-accessible workspace links', async t => {
  const selected = {...run, team_parent_id: 'f'.repeat(32), visiting_roles: [{role: 'verifier', engine: 'claude'}]};
  const sibling = {...selected, id: '9'.repeat(32), task: '형제 작업', visiting_roles: []};
  const {window} = mount(t, path => path === base ? room([selected, sibling]) : path === runPath ? {run: selected} : undefined);
  await until(() => window.document.querySelectorAll('a[data-child]').length === 2);
  const link = window.document.querySelector('[data-child="' + sibling.id + '"]');
  assert.match(link.textContent, /형제 작업/);
  assert.equal(new URL(link.href).searchParams.get('run'), sibling.id);
  assert.match(window.document.querySelector('[data-visitor="verifier"]').textContent, /독립 검증자 · Claude Code/);
});
