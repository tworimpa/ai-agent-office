import assert from 'node:assert/strict';
import test from 'node:test';
import {JSDOM} from 'jsdom';
import {firstProseParagraph, renderMarkdown, renderDiff} from '../../src/agent_office/web/office-content.js';

const doc = new JSDOM('', {url: 'http://localhost/'}).window.document;
const patch = [
  'diff --git a/main.py b/main.py', '--- a/main.py', '+++ b/main.py',
  '@@ -1,2 +1,2 @@', ' keep', '--- literal', '+++ literal',
  '--- /dev/null', '+++ b/새 파일.txt', '@@ -0,0 +1,2 @@', '+첫 줄', '+마지막 줄', '',
].join('\n');

test('Markdown preserves semantic structure and keeps untrusted markup inert', () => {
  const input = '# 결과\n\n- **완료**\n- `pytest`\n\n> 확인\n\n| 파일 | 결과 |\n|---|---|\n| a | 통과 |\n\n' +
    '<img src=x onerror=alert(1)> ![track](https://example.org/x) [open](javascript:alert(1))';
  const body = renderMarkdown(doc, input, {source: true, key: 'reply'});
  assert.equal(body.querySelector('h1').textContent, '결과');
  assert.equal(body.querySelector('ul strong').textContent, '완료');
  assert.equal(body.querySelector('li code').textContent, 'pytest');
  assert.equal(body.querySelector('blockquote').textContent, '확인');
  assert.equal(body.querySelectorAll('th').length, 2);
  assert.equal(body.querySelectorAll('td').length, 2);
  assert.equal(body.querySelector('img, a[href^="javascript:"], [onerror], script, iframe'), null);
  assert.ok(body.textContent.includes('<img src=x'));
  assert.equal(body.querySelector('[data-disclosure-key="reply:source"] pre').textContent, input);
});

test('short fences and pasted code or logs fold separately from surrounding paragraphs', () => {
  for (const middle of ['```js\nlet x = 1;\n```', 'const x = 1;\nconst y = 2;\nconsole.log(x + y);',
    'INFO init\nWARN retry\nERROR stop', patch.trimEnd()]) {
    const body = renderMarkdown(doc, '앞 설명\n' + middle + '\n뒤 설명', {key: 'r'});
    assert.deepEqual([...body.querySelectorAll('p')].map(p => p.textContent), ['앞 설명', '뒤 설명']);
    assert.equal(body.querySelectorAll('details').length, 1);
    assert.equal(body.querySelector('details').open, false);
    assert.ok(body.querySelector('details').dataset.disclosureKey.startsWith('r:'));
    assert.ok(body.querySelector('details pre').textContent.includes(middle.split('\n')[1]));
  }
});

test('operator can copy exact original including malformed syntax and CRLF, and toggle wrapping', async () => {
  const raw = '앞\r\n````sh\r\n```\r\n<script>x</script>\r\n';
  const node = renderMarkdown(doc, raw, {source: true});
  let copied;
  Object.defineProperty(doc.defaultView.navigator, 'clipboard', {configurable: true, value: {writeText: async text => { copied = text; }}});
  const original = node.querySelector('[data-disclosure-key="message:source"]');
  assert.equal(original.open, false);
  assert.equal(original.querySelector('pre').textContent, raw);
  original.querySelector('[data-wrap-key]').click();
  assert.equal(original.querySelector('[data-wrap-key]').getAttribute('aria-pressed'), 'true');
  assert.ok(original.querySelector('pre').classList.contains('is-wrapped'));
  [...original.querySelectorAll('button')].find(b => b.textContent === '복사').click();
  await Promise.resolve();
  assert.equal(copied, raw);
});

test('file sections reconcile porcelain rows with Git and bare unified patches and count actual hunk lines', () => {
  const node = renderDiff(doc, {changes: ' M main.py\n?? 새 파일.txt\n', diff: patch, key: 'run7'});
  const files = [...node.querySelectorAll('details')];
  assert.equal(files.length, 2);
  assert.ok(files.every(file => !file.open && file.dataset.disclosureKey.startsWith('run7:')));
  assert.match(files[0].querySelector('summary').textContent, /main\.py.*\+1.*−1/);
  assert.match(files[1].querySelector('summary').textContent, /새 파일\.txt.*\+2.*−0/);
  assert.match(node.firstElementChild.textContent, /2개.*\+3.*−1/);
  assert.equal(files[0].querySelector('.diff-added').textContent.trim(), '+++ literal');
  assert.equal(files[0].querySelector('.diff-deleted').textContent.trim(), '--- literal');
  assert.ok(files[1].querySelector('pre').textContent.endsWith('+마지막 줄\n'));
});

test('copy falls back inside the panel when the async clipboard is denied and restores focus', async () => {
  const win = new JSDOM('').window;
  const doc = win.document;
  const raw = '첫 줄\r\n둘째 줄';
  Object.defineProperty(win.navigator, 'clipboard', {value: {async writeText() { throw Error('denied'); }}});
  const node = renderMarkdown(doc, raw, {source: true});
  doc.body.append(node);
  const copy = [...node.querySelectorAll('button')].find(b => b.textContent === '복사');
  let copied;
  doc.execCommand = command => {
    assert.equal(command, 'copy');
    assert.equal(doc.activeElement.tagName, 'TEXTAREA');
    const event = new win.Event('copy', {cancelable: true});
    Object.defineProperty(event, 'clipboardData', {value: {setData(type, text) {
      assert.equal(type, 'text/plain');
      copied = text;
    }}});
    doc.activeElement.dispatchEvent(event);
    return event.defaultPrevented;
  };
  copy.focus();
  copy.click();
  await new Promise(resolve => setTimeout(resolve, 0));
  assert.equal(copied, raw);
  assert.equal(doc.activeElement, copy);
  assert.equal(node.querySelector('textarea'), null);
  assert.equal(node.querySelector('[role=status]').textContent, '복사됨');
  win.close();
});

test('a prose list after a pasted diff stays visible and a single long JSON line folds', () => {
  const node = renderMarkdown(doc, patch + '\n- 다음 단계');
  assert.equal(node.querySelector('li').textContent, '다음 단계');
  assert.ok(!node.querySelector('pre').textContent.includes('다음 단계'));
  const json = JSON.stringify({value: 'x'.repeat(240)});
  assert.equal(renderMarkdown(doc, json).querySelector('details pre').textContent, json);
});

test('the simple summary uses the first prose paragraph after technical blocks', () => {
  const technical = '# 제목\r\n\r\n~~~\r\n여기부터 코드\r\n~~~\r\n\r\n|제목|값|\r\n|---|---|\r\n|x|y|\r\n' +
    'INFO 시작\r\nWARN 재시도\r\nERROR 종료\r\n' + patch + '\r\n- 체크 목록\r\n';
  assert.equal(firstProseParagraph(technical), '');
  assert.equal(firstProseParagraph('const answer = 42;'), '');
  assert.equal(firstProseParagraph(technical + '\r\n**작업 결과**입니다.\r\n둘째 줄입니다.\r\n\r\n다음 단락'),
    '**작업 결과**입니다. 둘째 줄입니다.');
  assert.equal(firstProseParagraph('🐱'.repeat(321)), '🐱'.repeat(319) + '…');
});

test('binary, status-only, malformed, and clipped patches keep evidence without invented totals', () => {
  const node = renderDiff(doc, {changes: ' M a.bin\n?? new.bin\n M x.py', diff:
    'diff --git a/a.bin b/a.bin\nBinary files a/a.bin and b/a.bin differ\n' +
    'diff --git a/x.py b/x.py\n@@ -1 +1,3 @@\n-old\n+partial...[truncated]'});
  assert.equal(node.querySelectorAll('details').length, 3);
  assert.match(node.textContent, /바이너리/);
  assert.match(node.textContent, /일부/);
  assert.match(node.textContent, /new.bin/);
  assert.doesNotMatch(node.firstElementChild.textContent, /\+\d/);
  const odd = renderDiff(doc, {diff: 'unstructured <script>x</script>'});
  assert.match(odd.querySelector('pre').textContent, /unstructured <script>/);
  assert.equal(odd.querySelector('script'), null);
  assert.doesNotMatch(odd.firstElementChild.textContent, /파일 1개/);
});

test('Git-quoted Korean paths and renames share exactly one file section with porcelain status', () => {
  const quoted = '\\355\\225\\234\\352\\270\\200.py';
  const changes = 'R  "old.py" -> "' + quoted + '"';
  const diff = 'diff --git a/old.py "b/' + quoted + '"\nsimilarity index 100%\nrename from old.py\nrename to "' + quoted + '"\n';
  const node = renderDiff(doc, {changes, diff});
  assert.equal(node.querySelectorAll('details').length, 1);
  assert.match(node.querySelector('summary').textContent, /한글\.py/);
  assert.ok(node.textContent.includes(changes));
  assert.equal(node.querySelector('pre').textContent, diff);
  const nested = renderDiff(doc, {changes: ' M docs b/note.bin', diff:
    'diff --git a/docs b/note.bin b/docs b/note.bin\nBinary files a/docs b/note.bin and b/docs b/note.bin differ\n'});
  assert.equal(nested.querySelectorAll('details').length, 1);
  assert.match(nested.querySelector('summary').textContent, /^docs b\/note\.bin/);
});
