import test from 'node:test';
import assert from 'node:assert/strict';
import {mergeEvents, stateLabel} from '../../src/agent_office/web/state.js';

const empty = {runId: null, events: [], cursor: 0};
const event = (seq, run_id = 'r') => ({seq, run_id, event_type: 'STAGE_STARTED'});

test('deduplicates out-of-order pages without mutating inputs', () => {
  const current = {runId: 'r', events: [event(2), event(1)], cursor: 2};
  const next = mergeEvents(current, {events: [event(3), event(2)]}, 'r');
  assert.deepEqual(next.events.map(e => e.seq), [1, 2, 3]);
  assert.equal(next.cursor, 3);
  assert.deepEqual(current.events.map(e => e.seq), [2, 1]);
});
test('new run resets prior cursor and events', () => {
  const next = mergeEvents({runId: 'r', events: [event(200)], cursor: 200}, {events: [event(1, 'new')]}, 'new');
  assert.equal(next.cursor, 1);
  assert.equal(next.events.length, 1);
  assert.equal(next.events[0].run_id, 'new');
});
test('empty page preserves cursor', () => {
  assert.equal(mergeEvents({runId: 'r', events: [event(3)], cursor: 3}, {events: []}, 'r').cursor, 3);
});
test('display is bounded while cursor keeps full progress', () => {
  const result = mergeEvents(empty, {events: Array.from({length: 1200}, (_, i) => event(i+1))}, 'r');
  assert.equal(result.events.length, 1000);
  assert.equal(result.events[0].seq, 201);
  assert.equal(result.cursor, 1200);
});
test('unsafe sequences and mixed runs never merge', () => {
  assert.throws(() => mergeEvents(empty, {events: [event(2**54)]}, 'r'));
  assert.throws(() => mergeEvents(empty, {events: [event(1, 'other')]}, 'r'));
});
test('planning-only and reviewed completion are visibly distinct', () => {
  assert.equal(stateLabel('PLAN_ONLY'), '계획만 작성');
  assert.equal(stateLabel('READY_FOR_HUMAN_REVIEW'), '검토 준비');
  assert.equal(stateLabel('RECOVERY_REQUIRED'), '복구 확인 필요');
});
