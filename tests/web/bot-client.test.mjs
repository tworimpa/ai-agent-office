import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { JSDOM } from 'jsdom';
import { mountBotClient } from '../../src/agent_office/web/bot-client.js';
import { pagedHistory } from '../../src/agent_office/web/bot-client-ui.js';

const html = await readFile(new URL('../../src/agent_office/web/bot-client.html', import.meta.url), 'utf8');
const workspace = 'a'.repeat(32),
  journal = 'b'.repeat(32),
  oldRun = 'c'.repeat(32);
const first = { id: 'research', name: '다온', role: 'UX 리서처', digest: 'v1', appearance: { preset: 1 } };
const second = { id: 'tester', name: '솔', role: '접근성 테스터', digest: 'v1', appearance: { preset: 11 } };
const turn = () => new Promise((resolve) => setTimeout(resolve, 15));

async function fixture(t, { query = '', respond, saved, currentJournal = journal } = {}) {
  const { window: win } = new JSDOM(html, { url: 'http://localhost/bots' + query, pretendToBeVisual: true });
  for (const [name, fn] of Object.entries({
    showModal() {
      this.open = true;
    },
    close() {
      this.open = false;
      this.dispatchEvent(new win.Event('close'));
    },
  }))
    win.HTMLDialogElement.prototype[name] = fn;
  if (saved) for (const [key, value] of Object.entries(saved)) win.sessionStorage.setItem(key, value);
  const models = {
    bots: [{ id: 'office-developer', builtin: true }, first, second],
    messages: [],
    orders: [],
    runs: [
      {
        id: oldRun,
        task: '사용자 의견 정리',
        state: 'WAITING_USER',
        pending_count: 1,
        assignment: { bots: { developer: first } },
      },
    ],
    files: {
      id: first.id,
      digest: 'v1',
      files: { 'SOUL.md': '일을 조사한다', 'notes/기록.md': '첫 기록' },
      folders: ['빈 폴더'],
    },
  };
  let handlers;
  const scene = {
    show(bots) {
      this.bots = bots;
    },
    pause() {},
    respond(values) {
      this.last = values;
    },
  };
  const calls = [];
  win.fetch = async (raw, options) => {
    const url = new URL(raw, win.location),
      path = url.pathname.replace('/api/rooms/' + workspace, '');
    const body = typeof options.body === 'string' ? JSON.parse(options.body) : options.body;
    calls.push({ path, body, query: url.search });
    const reply = (data, status = 200) => ({
      ok: status < 400,
      status,
      json: async () => structuredClone(data),
    });
    const custom = await respond?.({ path, body, reply, models, calls, options });
    if (custom) return custom;
    if (body) throw new Error('unexpected write ' + path);
    if (path === '/api/workspaces') return reply({ workspaces: [{ id: workspace, name: '테스트 작업실' }] });
    if (path === '')
      return reply({
        workspace: { id: workspace, name: '테스트 작업실', folders: ['one', 'two'] },
        journal_id: currentJournal,
        runs: models.runs,
      });
    assert.equal(url.searchParams.get('office_journal'), currentJournal);
    if (path === '/bots') return reply({ bots: models.bots, assignments: {} });
    if (path === '/bot-orders') return reply({ orders: models.orders });
    if (path.endsWith('/assets')) return reply(models.files);
    if (path.endsWith('/chat')) return reply({ messages: models.messages });
    throw Error('unexpected ' + path);
  };
  const app = await mountBotClient(win, (_el, options) => {
    handlers = options;
    return scene;
  });
  t.after(() => {
    app.dispose();
    win.close();
  });
  const get = (id) => win.document.getElementById(id);
  function input(id, value) {
    const node = get(id);
    if (node.type === 'checkbox') node.checked = value;
    else node.value = value;
    node.dispatchEvent(new win.Event('input', { bubbles: true }));
  }
  async function submit(id) {
    get(id).dispatchEvent(new win.Event('submit', { cancelable: true, bubbles: true }));
    await turn();
  }
  return { win, scene, handlers, models, get, input, submit, calls };
}

test('real roster, waiting run and read-only command retain the same ID after a lost reply', async (t) => {
  let attempts = 0;
  const { scene, handlers, get, input, submit, calls } = await fixture(t, {
    respond: ({ path, body, reply, models }) => {
      if (path !== '/bots/research/commands' || !body) return;
      if (++attempts === 1) throw Error('응답 끊김');
      models.messages = [
        { id: 'record', role: 'assistant', text: '실행기의 실제 결과', state: 'PLAN_ONLY', run_id: body.id },
      ];
      return reply({
        run: { id: body.id, workspace_id: workspace, assignment: { bots: { developer: first } } },
      });
    },
  });
  assert.deepEqual(
    scene.bots.map((b) => b.id),
    ['research', 'tester'],
  );
  get('history').querySelector('button').click();
  assert.ok(get('taskBody').textContent.includes('응답 대기 1건'));
  assert.equal(
    get('taskBody').querySelector('a').getAttribute('href'),
    '/?workspace=' + workspace + '&run=' + oldRun,
  );
  handlers.choose(first);
  get('tabs').querySelector('.group button').click();
  await turn();
  input('folder', '1');
  input('msg', '현황 확인');
  await submit('compose');
  assert.equal(get('msg').value, '현황 확인');
  await submit('compose');
  const payloads = calls.filter((c) => c.path.endsWith('/commands')).map((c) => c.body);
  assert.equal(payloads.length, 2);
  assert.deepEqual(payloads[0], payloads[1]);
  assert.deepEqual(payloads[0].indices, [1]);
  assert.equal(payloads[0].allow_edits, false);
  assert.equal(get('msg').value, '');
  assert.ok(get('thread').textContent.includes('실행기의 실제 결과'));
});

test('nested files open centrally, draft digest survives reload and conflicts require explicit comparison', async (t) => {
  const one = await fixture(t);
  one.handlers.choose(first);
  await turn();
  one.get('explorer').querySelector('[data-dir=notes]').click();
  one.get('explorer').querySelector('[title="notes/기록.md"]').click();
  one.input('text', '편집 초안');
  one.handlers.choose(second);
  one.handlers.choose(first);
  assert.equal(one.get('text').value, '편집 초안');
  const saved = { ...one.win.sessionStorage };
  let writes = 0;
  const two = await fixture(t, {
    query: '?workspace=' + workspace + '&bot=research',
    saved,
    respond: ({ path, body, models, reply }) => {
      if (path.endsWith('/assets')) {
        models.files.digest = 'v2';
        models.files.files['notes/기록.md'] = '다른 곳에서 저장한 기록';
        if (body) {
          writes++;
          assert.equal(body.digest, 'v2');
          assert.equal(body.content, '편집 초안');
          models.files.files[body.filename] = body.content;
          models.files.digest = 'v3';
          return reply(models.files);
        }
      }
    },
  });
  await turn();
  assert.equal(two.get('filename').textContent, 'notes/기록.md');
  assert.equal(two.get('save').disabled, true);
  two.get('resolveFile').click();
  assert.equal(two.get('serverText').value, '다른 곳에서 저장한 기록');
  two.get('rebaseDraft').click();
  two.get('save').click();
  await turn();
  assert.equal(writes, 1);
  assert.equal(two.get('save').disabled, true);
  const clean = await fixture(t, {
    query: '?workspace=' + workspace + '&bot=research',
    saved,
    currentJournal: 'd'.repeat(32),
  });
  assert.equal(clean.get('chat').hidden, false);
  assert.equal(clean.get('editor').hidden, true);
});

test('king keeps every recipient on retry and exposes partial admission without opening pending fake run', async (t) => {
  let attempts = 0;
  const { get, handlers, input, submit, scene, calls, win } = await fixture(t, {
    respond: ({ path, body, reply, models }) => {
      if (path !== '/bot-orders' || !body) return;
      const partial = ++attempts === 1;
      const order = {
        ...body,
        assignments: body.assignments.map((a, i) => ({
          ...a,
          run_id: String(i + 1).repeat(32),
          ...(partial && i === 1 ? { state: 'pending', error: '작업 슬롯 부족' } : { state: 'QUEUED' }),
        })),
      };
      models.orders = [order];
      return reply({ order });
    },
  });
  handlers.openKing();
  input('royal-command', '다온: 사용자 사례 조사');
  await submit('royal-form');
  const duties = get('royal-assignments').querySelectorAll('textarea');
  assert.equal(duties.length, 2);
  assert.equal(duties[0].value, '사용자 사례 조사');
  duties[1].value = '키보드 조작 확인';
  duties[1].dispatchEvent(new win.Event('input'));
  await submit('royal-form');
  assert.equal(get('royal-dialog').open, true);
  assert.equal(get('royal-history').querySelectorAll('a').length, 1);
  assert.deepEqual(
    scene.last.map((a) => a.bot),
    ['research'],
  );
  assert.ok(get('royal-status').textContent.includes('솔 · 작업 슬롯 부족'));
  assert.equal(get('royal-command').readOnly, true);
  await submit('royal-form');
  assert.equal(get('royal-dialog').open, false);
  const bodies = calls.filter((c) => c.path === '/bot-orders' && c.body).map((c) => c.body);
  assert.deepEqual(bodies[0], bodies[1]);
  assert.equal(bodies[1].allow_edits, false);
  assert.deepEqual(
    scene.last.map((a) => a.bot),
    ['tester'],
  );
});

test('character picker has twelve presets and creates a free-role bot from actual submitted values', async (t) => {
  const { get, submit, scene, calls } = await fixture(t, {
    respond: ({ path, body, models, reply }) => {
      if (path === '/bots' && body) {
        models.bots.push({ ...body, digest: 'new' });
        return reply({ bots: models.bots, assignments: {} });
      }
    },
  });
  get('new').click();
  assert.equal(get('choices').children.length, 12);
  get('choices').lastElementChild.click();
  get('name').value = '별';
  get('job').value = '천문 기록관';
  get('soul').value = '관측 일지를 검토한다.';
  await submit('botForm');
  const body = calls.find((c) => c.path === '/bots' && c.body).body;
  assert.equal(body.role, '천문 기록관');
  assert.deepEqual(body.appearance, { preset: 11 });
  assert.ok(scene.bots.some((bot) => bot.name === '별' && bot.face === 11));
});

test('global explorer creates a nested file on the server and opens it in the owner’s tab', async (t) => {
  const { get, submit, win } = await fixture(t, {
    respond: ({ path, body, models, reply }) => {
      if (path !== '/bots/research/assets' || !body) return;
      assert.deepEqual(body, {
        filename: '참고/경로/검토.md',
        digest: 'v1',
        content: '',
        kind: 'file',
        create: true,
      });
      models.files.files[body.filename] = '';
      models.files.digest = 'v2';
      return reply(models.files);
    },
  });
  const root = get('explorer').querySelector('details');
  root.open = true;
  root.dispatchEvent(new win.Event('toggle'));
  await turn();
  const create = [...get('explorer').querySelectorAll('button')].find(
    (node) => node.textContent === '새 파일',
  );
  create.click();
  get('entryName').value = '참고/경로/검토.md';
  await submit('entryForm');
  assert.equal(get('filename').textContent, '참고/경로/검토.md');
  assert.ok(get('fileowner').textContent.includes('다온'));
  assert.equal(get('text').value, '');
  assert.equal(get('entryDialog').open, false);
});

test('Git package requires a preview and installs only its pinned commit in the current workspace', async (t) => {
  const source = {
    url: 'https://example.org/bots.git', version: 'v1', path: 'sample',
    commit: '7'.repeat(40), digest: '8'.repeat(64),
  };
  const { get, input, submit, calls } = await fixture(t, {
    respond: ({ path, body, reply, models }) => {
      if (path === '/bots/package-preview') {
        if (body.version !== 'v1') return reply({ error: { message: '태그가 없습니다.' } }, 400);
        return reply({ name: '공유 봇', role: '조사', source, digest: source.digest, files: ['SOUL.md'] });
      }
      if (path === '/bots/package-install') {
        assert.deepEqual(body, source);
        models.bots.push({ id: 'shared', name: '공유 봇', role: '조사', digest: source.digest, package: source });
        return reply({ id: 'shared', source });
      }
    },
  });
  get('packageImport').click();
  assert.equal(get('packageInstall').disabled, true);
  input('packageUrl', source.url);
  input('packageVersion', 'no-such-tag');
  await submit('packageForm');
  assert.match(get('packageStatus').textContent, /태그가 없습니다/);
  input('packageVersion', 'v1');
  input('packagePath', 'sample');
  await submit('packageForm');
  assert.match(get('packagePreview').textContent, new RegExp(source.commit));
  input('packagePath', 'other');
  assert.equal(get('packageInstall').disabled, true);
  input('packagePath', 'sample');
  await submit('packageForm');
  get('packageInstall').click();
  get('packageInstall').click();
  await turn();
  assert.equal(calls.filter((call) => call.path === '/bots/package-install').length, 1);
  assert.equal(get('packageDialog').open, false);
  assert.match(get('chatTitle').textContent, /공유 봇/);
  assert.match(get('packageSource').textContent, new RegExp(source.commit));
  assert.equal(get('packageExport').download, 'shared.bot-package.zip');
  assert.equal(get('exportBot').download, 'shared.bot.json');
});

test('import preserves original bytes for server validation and export downloads the journal-bound manifest', async (t) => {
  let original;
  const { get, handlers, win } = await fixture(t, {
    respond: ({ path, body, options, reply }) => {
      if (path !== '/bots/import') return;
      assert.equal(body, original);
      assert.equal(options.headers['X-Agent-Office'], 'studio');
      assert.equal(options.mode, 'same-origin');
      return reply({ error: { message: '중복 키' } }, 400);
    },
  });
  handlers.choose(first);
  assert.equal(get('exportBot').download, 'research.bot.json');
  assert.equal(
    get('exportBot').getAttribute('href'),
    '/api/rooms/' + workspace + '/bots/research/manifest?office_journal=' + journal,
  );
  original = new win.File(['{"id":"one","id":"two"}'], 'bot.json', { type: 'application/json' });
  Object.defineProperty(get('manifestFile'), 'files', { value: [original] });
  await get('manifestFile').onchange();
  assert.equal(get('notice').textContent, '중복 키');
});

test('slow previous chat never replaces the new bot, and interrupted commands recover after reload', async (t) => {
  let release;
  const slow = new Promise((resolve) => {
    release = resolve;
  });
  const one = await fixture(t, {
    respond: async ({ path, body, reply }) => {
      if (path === '/bots/research/chat') {
        await slow;
        return reply({ messages: [{ id: 'old', role: 'assistant', text: '이전 봇의 답변' }] });
      }
      if (path === '/bots/tester/chat')
        return reply({ messages: [{ id: 'new', role: 'assistant', text: '선택한 봇의 답변' }] });
      if (path.endsWith('/commands') && body) throw Error('전송 후 연결 끊김');
    },
  });
  one.handlers.choose(first);
  one.handlers.choose(second);
  await turn();
  release();
  await turn();
  assert.ok(one.get('thread').textContent.includes('선택한 봇의 답변'));
  assert.ok(!one.get('thread').textContent.includes('이전 봇'));
  one.input('msg', '작업 확인');
  await one.submit('compose');
  const previous = one.calls.find((c) => c.path.endsWith('/commands')).body;
  const two = await fixture(t, {
    query: '?workspace=' + workspace + '&bot=tester',
    saved: { ...one.win.sessionStorage },
    respond: ({ path, body, reply }) => {
      if (path.endsWith('/commands') && body)
        return reply({
          run: { id: body.id, workspace_id: workspace, assignment: { bots: { developer: second } } },
        });
    },
  });
  assert.equal(two.get('msg').value, '작업 확인');
  await two.submit('compose');
  assert.deepEqual(two.calls.find((c) => c.path.endsWith('/commands')).body, previous);
});

test('choosing a preset for a bot with a server portrait clears its image without resending the URL', async (t) => {
  const custom = { ...first, appearance: { preset: 3, image: '/api/example/portrait' } };
  const page = await fixture(t, {
    respond: ({ path, body, reply, models }) => {
      if (path === '/bots' && !body) return reply({ bots: [custom] });
      if (path === '/bots/research/appearance') return reply(models.files);
    },
  });
  page.handlers.choose(custom);
  page.get('character').click();
  await page.submit('botForm');
  assert.equal(page.calls.filter((c) => c.path.endsWith('/appearance')).length, 0);
  page.get('character').click();
  page.get('choices').children[6].click();
  await page.submit('botForm');
  assert.deepEqual(page.calls.find((c) => c.path.endsWith('/appearance')).body, {
    appearance: { preset: 6 },
    digest: 'v1',
  });
});

test('a new browser can restore redacted original text without changing group ID or granting different permissions', async (t) => {
  const original = {
    id: 'f'.repeat(32),
    message: '  기록 [redacted]  ',
    allow_edits: false,
    indices: [1],
    replay_requires_original: true,
    assignments: [{ bot_id: first.id, duty: '  담당 [redacted]  ', state: 'pending', run_id: oldRun }],
  };
  const page = await fixture(t, {
    respond: ({ path, body, reply }) => {
      if (path !== '/bot-orders') return;
      if (!body) return reply({ orders: [original] });
      if (body.message !== '  기록 원문  ') return reply({ error: { message: '원문 불일치' } }, 409);
      return reply({
        order: { ...original, assignments: [{ ...original.assignments[0], state: 'QUEUED' }] },
      });
    },
  });
  page.handlers.openKing();
  page.get('royal-history').querySelector('button').click();
  assert.equal(page.get('royal-folder').disabled, true);
  assert.equal(page.get('royal-edits').disabled, true);
  page.input('royal-command', '다른 내용');
  await page.submit('royal-form');
  assert.equal(page.get('royal-command').readOnly, false);
  page.input('royal-command', '  기록 원문  ');
  const duty = page.get('royal-assignments').querySelector('textarea');
  duty.value = '  담당 원문  ';
  duty.dispatchEvent(new page.win.Event('input'));
  await page.submit('royal-form');
  const retries = page.calls.filter((c) => c.path === '/bot-orders' && c.body);
  assert.equal(retries.length, 2);
  for (const { body } of retries) {
    assert.equal(body.id, original.id);
    assert.deepEqual(body.indices, [1]);
    assert.equal(body.allow_edits, false);
    assert.deepEqual(
      body.assignments.map((a) => a.bot_id),
      [first.id],
    );
  }
  assert.equal(retries[1].body.assignments[0].duty, '  담당 원문  ');
});

test('older chat and order pages remain accessible alongside new server results', async (t) => {
  const chatCursor = '3'.repeat(32),
    orderCursor = '4'.repeat(32);
  const row = (id, text) => ({ id, role: 'user', text });
  const order = (id, message) => ({ id, message, assignments: [] });
  let newResult = false;
  const page = await fixture(t, {
    respond: ({ path, reply }) => {
      if (path === '/bots/research/chat')
        return reply({
          messages: [row('middle', '중간 대화'), ...(newResult ? [row('new', '새 대화')] : [])],
          next_cursor: chatCursor,
        });
      if (path === '/bots/research/chat/before/' + chatCursor)
        return reply({ messages: [row('old', '처음 대화')], next_cursor: null });
      if (path === '/bot-orders')
        return reply({
          orders: [...(newResult ? [order('new-order', '새 어명')] : []), order('middle-order', '중간 어명')],
          next_cursor: orderCursor,
        });
      if (path === '/bot-orders/before/' + orderCursor)
        return reply({ orders: [order('old-order', '첫 어명')], next_cursor: null });
    },
  });
  page.handlers.choose(first);
  await turn();
  page.get('thread').querySelector('.history-more').click();
  await turn();
  assert.ok(
    page.get('thread').textContent.indexOf('처음 대화') < page.get('thread').textContent.indexOf('중간 대화'),
  );
  page.handlers.openKing();
  page.get('royal-history').querySelector('.history-more').click();
  await turn();
  newResult = true;
  page.win.document.dispatchEvent(new page.win.Event('visibilitychange'));
  await turn();
  assert.deepEqual(
    [...page.get('thread').querySelectorAll('.bubble p')].map((p) => p.textContent),
    ['처음 대화', '중간 대화', '새 대화'],
  );
  assert.deepEqual(
    [...page.get('royal-history').querySelectorAll('summary')].map((p) => p.textContent),
    ['새 어명', '중간 어명', '첫 어명'],
  );
});

test('a gap after reconnect retains expanded ranges and can be filled through its own cursor', () => {
  const history = pagedHistory(),
    rows = (...ids) => ids.map((id) => ({ id }));
  history.latest(rows(3, 2), '2');
  history.older('2', rows(1), null);
  history.latest(rows(7, 6), '6');
  assert.deepEqual(
    history.groups.map((g) => g.next),
    ['6', null],
  );
  history.older('6', rows(5, 4, 3), '3');
  assert.deepEqual(
    history.groups.map((g) => g.rows.map((r) => r.id)),
    [[7, 6, 5, 4, 3, 2, 1]],
  );
  assert.equal(history.groups[0].next, null);
});

test('partial recipients can be parked, a selected subset dispatched, and the original request resumed', async (t) => {
  let attempt = 0;
  const page = await fixture(t, {
    respond: ({ path, body, models, reply }) => {
      if (path !== '/bot-orders' || !body) return;
      attempt++;
      const order = {
        ...body,
        assignments: body.assignments.map((a, i) => ({
          ...a,
          run_id: String(i + 1).repeat(32),
          state: attempt === 1 && i === 1 ? 'pending' : attempt === 2 ? 'FAILED' : 'QUEUED',
        })),
      };
      models.orders = [order, ...models.orders.filter((row) => row.id !== order.id)];
      return reply({ order });
    },
  });
  const { handlers, get, input, submit, scene, win } = page;
  handlers.openKing();
  input('royal-command', '첫 어명');
  await submit('royal-form');
  await submit('royal-form');
  assert.deepEqual(
    scene.last.map((a) => a.bot),
    [first.id],
  );
  get('royal-new').click();
  assert.equal(get('royal-command').value, '');
  input('royal-command', '두 번째 어명');
  await submit('royal-form');
  const check = get('royal-assignments').querySelector('input');
  check.checked = false;
  check.dispatchEvent(new win.Event('change'));
  await submit('royal-form');
  assert.deepEqual(
    scene.last.map((a) => a.bot),
    [first.id],
  );
  handlers.openKing();
  get('royal-history').querySelector('button').click();
  assert.equal(get('royal-command').value, '첫 어명');
  await submit('royal-form');
  const requests = page.calls.filter((c) => c.path === '/bot-orders' && c.body).map((c) => c.body);
  assert.notEqual(requests[0].id, requests[1].id);
  assert.deepEqual(
    requests[1].assignments.map((a) => a.bot_id),
    [second.id],
  );
  assert.deepEqual(requests[0], requests[2]);
});

test('an incomplete or mismatched order receipt cannot clear the original or animate a run', async (t) => {
  const variants = [
    (o) => ({ ...o, assignments: [] }),
    (o) => ({ ...o, allow_edits: !o.allow_edits }),
    (o) => ({ ...o, indices: [7] }),
    (o) => ({ ...o, assignments: o.assignments.toReversed() }),
    (o) => ({ ...o, message: '다른 지시' }),
    (o) => ({ ...o, assignments: o.assignments.map((a) => ({ ...a, run_id: oldRun })) }),
    (o) => ({ ...o, assignments: o.assignments.map((a) => ({ ...a, state: 'UNKNOWN' })) }),
  ];
  let index = 0;
  const page = await fixture(t, {
    respond: ({ path, body, reply }) => {
      if (path === '/bot-orders' && body)
        return reply({
          order: variants[index++]({
            ...body,
            assignments: body.assignments.map((a, i) => ({
              ...a,
              run_id: String(i + 1).repeat(32),
              state: 'QUEUED',
            })),
          }),
        });
    },
  });
  page.handlers.openKing();
  page.input('royal-command', '대조');
  await page.submit('royal-form');
  for (const _ of variants) {
    await page.submit('royal-form');
    assert.equal(page.get('royal-dialog').open, true);
    assert.equal(page.get('royal-command').readOnly, true);
    assert.equal(page.scene.last, undefined);
  }
  assert.equal(
    new Set(page.calls.filter((c) => c.path === '/bot-orders' && c.body).map((c) => c.body.id)).size,
    1,
  );
});

test('run navigation verifies the owner and workspace before exposing a review link, including restored tabs', async (t) => {
  const prior = '9'.repeat(32),
    foreign = '8'.repeat(32);
  const respond = ({ path, reply }) => {
    if (path === '/api/runs/' + prior || path === '/api/runs/' + foreign) {
      const id = path.split('/').at(-1);
      return reply({
        run: {
          id,
          workspace_id: id === prior ? workspace : 'd'.repeat(32),
          task: '지난 일',
          state: 'PLAN_ONLY',
          assignment: { bots: { developer: second } },
        },
      });
    }
  };
  const p = await fixture(t, { query: '?workspace=' + workspace + '&bot=research&run=' + prior, respond });
  assert.equal(p.get('taskOwner').textContent, second.name);
  assert.ok(p.calls.find((c) => c.path === '/api/runs/' + prior).query.includes('office_journal=' + journal));
  const saved = {
    ['office-bot-room/v1/' + journal + '/' + workspace]: JSON.stringify({
      bots: {
        'bot:research': {
          active: 'task:' + foreign,
          tabs: ['chat', 'task:' + foreign],
          files: {},
          message: '',
        },
      },
    }),
  };
  const denied = await fixture(t, { query: '?workspace=' + workspace + '&bot=research', saved, respond });
  assert.equal(denied.get('task').hidden, true);
  assert.equal(denied.get('taskBody').querySelector('a'), null);
  assert.ok(!denied.win.location.search.includes(foreign));
  assert.ok(!JSON.stringify({ ...denied.win.sessionStorage }).includes(foreign));
});

test('two browser tabs keep separate edit sessions and each restores its own draft', async (t) => {
  const one = await fixture(t),
    two = await fixture(t);
  one.handlers.choose(first);
  one.input('msg', '첫 창에서 작성');
  two.handlers.choose(first);
  two.input('msg', '둘째 창에서 작성');
  assert.equal(one.win.localStorage.length, 0);
  assert.equal(two.win.localStorage.length, 0);
  for (const [original, want] of [
    [one, '첫 창에서 작성'],
    [two, '둘째 창에서 작성'],
  ]) {
    const restored = await fixture(t, {
      query: '?bot=research&workspace=' + workspace,
      saved: { ...original.win.sessionStorage },
    });
    assert.equal(restored.get('msg').value, want);
  }
});
