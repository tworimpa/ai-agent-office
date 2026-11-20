import assert from 'node:assert/strict';
import {test} from 'node:test';
import {JSDOM} from 'jsdom';
import {planFlow} from '../../src/agent_office/web/office-flow.js';

const tasks = [
  {id: 'T1', owner: 'developer', description: '첫 번째 수정', depends_on: [], folders: ['src']},
  {id: 'T2', owner: 'developer', description: '독립 작업', depends_on: []},
  {id: 'T3', owner: 'reviewer', description: '<img src=x onerror=alert(1)>', depends_on: ['T1', 'T2']},
];

function show(section) {
  section.querySelector('button').click();
  const chart = section.querySelector('.plan-flow-chart');
  if (!chart.hasAttribute('aria-busy')) return Promise.resolve(chart);
  return new Promise(resolve => {
    const observer = new section.ownerDocument.defaultView.MutationObserver(() => {
      if (chart.hasAttribute('aria-busy')) return;
      observer.disconnect();
      resolve(chart);
    });
    observer.observe(chart, {attributes: true});
  });
}

test('plan flows load on request, preserve actual parallel dependencies and scope their markers', async () => {
  const doc = new JSDOM().window.document;
  const current = planFlow(doc, {tasks}, 3);
  const older = planFlow(doc, {tasks: tasks.slice(0, 1)}, 2);
  doc.body.append(current, older);
  assert.equal(doc.querySelector('svg'), null);
  assert.equal(current.querySelector('button').getAttribute('aria-expanded'), 'false');
  assert.equal(current.querySelectorAll('ol li').length, 3);
  assert.match(current.querySelector('ol').textContent, /선행 작업 T1, T2/);
  assert.match(current.querySelector('ol').textContent, /작업 폴더 src/);
  const chart = await show(current);
  assert.equal(chart.hidden, false);
  assert.deepEqual(Array.from(chart.querySelectorAll('text'), n => n.textContent),
    ['T1 · 개발자', 'T2 · 개발자', 'T3 · 리뷰어']);
  assert.equal(chart.querySelectorAll('polyline').length, 2);
  assert.equal(chart.querySelector('svg').getAttribute('aria-label'), '계획 3 · 작업 흐름');
  assert.equal(older.querySelector('svg'), null);
  await show(older);
  const ids = Array.from(doc.querySelectorAll('[id]'), n => n.id);
  assert.equal(new Set(ids).size, ids.length);
  assert.equal(doc.querySelector('svg style, svg [style], svg[style], script, img, foreignObject'), null);
  const svg = chart.querySelector('svg');
  await show(current);
  assert.equal(chart.hidden, true);
  await show(current);
  assert.equal(chart.querySelector('svg'), svg);
});

test('unsafe identifiers and unresolved dependencies retain a readable list and produce no diagram', async () => {
  for (const change of [{id: 'X]\nn9[Injected'}, {depends_on: ['T99']}, {owner: '<script>'}]) {
    const doc = new JSDOM().window.document;
    const section = planFlow(doc, {tasks: [{...tasks[2], ...change}]}, 'v<unsafe>');
    doc.body.append(section);
    const chart = await show(section);
    assert.equal(chart.querySelector('svg'), null);
    assert.ok(chart.querySelector('[role=status]'));
    assert.match(section.querySelector('ol').textContent, /<img/);
    assert.equal(section.querySelector('img, script'), null);
  }
});

test('plans with no tasks show their empty state without a diagram action', () => {
  const section = planFlow(new JSDOM().window.document, {}, undefined);
  assert.match(section.textContent, /등록된 작업이 없습니다/);
  assert.equal(section.querySelector('button'), null);
});
