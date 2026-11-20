// Worker messages and Git output are untrusted text. Only the elements constructed here are markup.
function element(doc, tag, text, className) {
  const node = doc.createElement(tag);
  if (text !== undefined) node.textContent = text;
  if (className) node.className = className;
  return node;
}

function disclosure(doc, label, key) {
  const details = element(doc, 'details', undefined, 'content-fold');
  details.dataset.disclosureKey = key;
  details.append(element(doc, 'summary', label));
  return details;
}

function copySelection(doc, box, text) {
  if (typeof doc.execCommand !== 'function') return false;
  const previous = doc.activeElement;
  const selection = doc.getSelection();
  const ranges = Array.from({length: selection.rangeCount}, (_, i) => selection.getRangeAt(i).cloneRange());
  const field = element(doc, 'textarea', undefined, 'content-copy-field');
  field.readOnly = true;
  // Keep the temporary selection inside the active modal. The rest of the document may be inert.
  box.append(field);
  field.value = text;
  field.focus({preventScroll: true});
  field.select();
  // textarea.value normalizes CRLF. Supply the original text during this copy gesture.
  field.addEventListener('copy', event => {
    if (event.clipboardData) {
      event.clipboardData.setData('text/plain', text);
      event.preventDefault();
    }
  }, {once: true});
  try {
    return doc.execCommand('copy');
  } catch {
    return false;
  } finally {
    field.remove();
    previous?.focus({preventScroll: true});
    selection.removeAllRanges();
    for (const range of ranges) selection.addRange(range);
  }
}

function rawView(doc, text, key, paint) {
  const box = element(doc, 'div', undefined, 'content-source');
  const tools = element(doc, 'div', undefined, 'source-tools');
  const wrap = element(doc, 'button', '줄바꿈');
  const copy = element(doc, 'button', '복사');
  const status = element(doc, 'span');
  const pre = element(doc, 'pre');
  wrap.type = copy.type = 'button';
  wrap.dataset.wrapKey = key;
  wrap.setAttribute('aria-pressed', 'false');
  wrap.addEventListener('click', () => {
    const enabled = pre.classList.toggle('is-wrapped');
    wrap.setAttribute('aria-pressed', String(enabled));
  });
  status.setAttribute('role', 'status');
  copy.addEventListener('click', async () => {
    try {
      await doc.defaultView.navigator.clipboard.writeText(text);
      status.textContent = '복사됨';
    } catch {
      if (copySelection(doc, box, text)) {
        status.textContent = '복사됨';
        return;
      }
      const range = doc.createRange();
      range.selectNodeContents(pre);
      const selection = doc.getSelection();
      selection.removeAllRanges();
      selection.addRange(range);
      status.textContent = '선택한 텍스트를 복사하세요.';
    }
  });
  if (paint) paint(pre);
  else pre.textContent = text;
  tools.append(status, wrap, copy);
  box.append(tools, pre);
  return box;
}

export function renderSource(doc, text, key) {
  return rawView(doc, text, key);
}

function foldText(doc, label, text, key) {
  const fold = disclosure(doc, label, key);
  fold.append(rawView(doc, text, key));
  return fold;
}

function inline(doc, parent, value) {
  // A deliberately small inline grammar. Unsupported syntax, URLs, and HTML remain readable.
  const tokens = /(`[^`\n]+`|\*\*[^*\n]+\*\*|__[^_\n]+__|\*[^*\n]+\*)/g;
  let end = 0;
  for (const match of value.matchAll(tokens)) {
    parent.append(value.slice(end, match.index));
    const part = match[0];
    const strong = part.startsWith('**') || part.startsWith('__');
    const tag = strong ? 'strong' : part.startsWith('`') ? 'code' : 'em';
    const width = strong ? 2 : 1;
    parent.append(element(doc, tag, part.slice(width, -width)));
    end = match.index + part.length;
  }
  parent.append(value.slice(end));
}

const fence = /^ {0,3}(`{3,}|~{3,})(.*)$/;
const heading = /^ {0,3}(#{1,6})\s+(.+)$/;
const listRow = /^\s*([-*+]|\d+[.)])\s+(.+)$/;
const tableCells = row => row.trim().replace(/^\||\|$/g, '').split('|').map(s => s.trim());
const gitStart = /^diff --git /;
const hunkHeader = /^@@ -\d+(?:,(\d+))? \+\d+(?:,(\d+))? @@/;
const unifiedStart = (row, next) => row.startsWith('--- ') && next?.startsWith('+++ ');
const patchMetadata = /^(?:index |old mode |new mode |new file mode |deleted file mode |similarity index |rename (?:from|to) |Binary files |GIT binary patch|\\ No newline at end of file)/;

function rawKind(row) {
  if (/^\s*(?:\[(?:INFO|DEBUG|WARN(?:ING)?|ERROR|TRACE|PASS|FAIL)\]|(?:INFO|DEBUG|WARN(?:ING)?|ERROR|TRACE|PASS|FAIL|FAILED|PASSED)\b|\[?\d{4}-\d\d-\d\d[T ]\d\d:|\d\d:\d\d:\d\d|Traceback \(most recent|File ".+", line \d+|\w+(?:Error|Exception):|at \S+ ?\(|tests\/\S+::|={3,}.*={3,}|collected \d+ items?|E   |platform (?:win32|linux|darwin) |rootdir:)/i.test(row)) return '로그';
  if (/^\s*(?:[$>] (?:git|npm|pnpm|node|python|pytest|cargo|go)\b|(?:git|npm|pnpm|python|pytest|cargo) [-\w]+)/.test(row)) return '코드';
  if (/^\s*(?:\{|\[)\s*["{[]/.test(row) && /[}\]],?\s*$/.test(row)) return '코드';
  if (/^(?:\t| {4})\S|^\s*(?:[{}()[\];]+[,;]?|(?:const|let|var|function|class|def|async|import|from|return|if|else|for|while|try|except|export)\b.*[;:{})\]"'\w]|[\w.]+\([^)]*\);?|\w+\s*(?:=|=>)\s*.+|["'][^"']+["']:\s*.+|[<$][^\n]+>)\s*$/.test(row)) return '코드';
  return '';
}

function rawBlock(lines, start) {
  const isPatch = gitStart.test(lines[start]) || unifiedStart(lines[start], lines[start + 1]) || hunkHeader.test(lines[start]);
  const kind = isPatch ? '변경 내용' : rawKind(lines[start]);
  if (!kind) return null;
  let end = start + 1;
  if (isPatch) {
    let oldLeft = 0, newLeft = 0;
    for (end = start; end < lines.length; end++) {
      const row = lines[end];
      const hunk = row.match(hunkHeader);
      if (hunk) {
        oldLeft = Number(hunk[1] ?? 1);
        newLeft = Number(hunk[2] ?? 1);
      } else if ((row.startsWith(' ') || row.startsWith('-')) && oldLeft) {
        oldLeft--;
        if (row.startsWith(' ') && newLeft) newLeft--;
      } else if (row.startsWith('+') && newLeft) newLeft--;
      else if (unifiedStart(row, lines[end + 1])) end++;
      else if (!gitStart.test(row) && !patchMetadata.test(row)) break;
    }
    return {kind, end};
  }
  const accepts = row => Boolean(rawKind(row) || /^\s+\S/.test(row));
  while (end < lines.length) {
    if (accepts(lines[end])) end++;
    else if (!lines[end].trim() && end + 1 < lines.length && accepts(lines[end + 1])) end++;
    else break;
  }
  return (end - start >= 3 || lines[start].length >= 240) ? {kind, end} : null;
}

function tableAt(lines, i) {
  if (!lines[i].includes('|') || !lines[i + 1]?.includes('|')) return false;
  const cells = tableCells(lines[i + 1]);
  return cells.length === tableCells(lines[i]).length && cells.every(cell => /^:?-{3,}:?$/.test(cell));
}

function renderTable(doc, lines) {
  const container = element(doc, 'div', undefined, 'md-table');
  const table = element(doc, 'table');
  const head = table.createTHead();
  const body = table.createTBody();
  const addRow = (section, line, tag) => {
    const row = section.insertRow();
    for (const value of tableCells(line)) {
      const cell = element(doc, tag);
      if (tag === 'th') cell.scope = 'col';
      inline(doc, cell, value);
      row.append(cell);
    }
  };
  addRow(head, lines[0], 'th');
  for (const row of lines.slice(2)) addRow(body, row, 'td');
  container.append(table);
  return container;
}

function* markdownBlocks(text) {
  const lines = text.replace(/\r\n?/g, '\n').split('\n');
  let paragraph = [];
  for (let i = 0; i < lines.length;) {
    const row = lines[i];
    const marker = row.match(fence);
    const title = row.match(heading);
    const block = rawBlock(lines, i);
    const item = row.match(listRow);
    if (row.trim() && !marker && !title && !block && !item && !/^ {0,3}>/.test(row) && !tableAt(lines, i)) {
      paragraph.push(row);
      i++;
      continue;
    }
    if (paragraph.length) {
      yield {tag: 'p', text: paragraph.join('\n')};
      paragraph = [];
    }
    const start = i;
    if (marker) {
      const close = new RegExp('^ {0,3}' + marker[1][0] + '{' + marker[1].length + ',}\\s*$');
      i++;
      while (i < lines.length && !close.test(lines[i])) i++;
      const label = ['코드', marker[2].trim(), (i - start - 1) + '줄'].filter(Boolean).join(' · ');
      yield {tag: 'details', start, label, text: lines.slice(start + 1, i).join('\n')};
      i++;
    } else if (block) {
      i = block.end;
      yield {tag: 'details', start, label: block.kind + ' · ' + (i - start) + '줄', text: lines.slice(start, i).join('\n')};
    } else if (title) {
      yield {tag: 'h' + title[1].length, text: title[2]};
      i++;
    } else if (item) {
      const ordered = /^\d/.test(item[1]);
      const items = [];
      let entry = item;
      while (entry && ordered === /^\d/.test(entry[1])) {
        items.push(entry[2]);
        entry = lines[++i]?.match(listRow);
      }
      yield {tag: ordered ? 'ol' : 'ul', items, number: parseInt(item[1], 10)};
    } else if (/^ {0,3}>/.test(row)) {
      const quote = [];
      while (i < lines.length && /^ {0,3}>/.test(lines[i])) quote.push(lines[i++].replace(/^ {0,3}> ?/, ''));
      yield {tag: 'blockquote', text: quote.join('\n')};
    } else if (tableAt(lines, i)) {
      i += 2;
      while (i < lines.length && lines[i].includes('|')) i++;
      yield {tag: 'table', rows: lines.slice(start, i)};
    } else i++;
  }
  if (paragraph.length) yield {tag: 'p', text: paragraph.join('\n')};
}

export function firstProseParagraph(text) {
  let fallback = '';
  for (const block of markdownBlocks(text)) {
    if (!fallback) {
      const candidate = block.items?.[0] || (block.tag === 'blockquote' ? block.text.split('\n')[0] : '');
      if (candidate && !rawKind(candidate)) fallback = candidate;
    }
    if (block.tag !== 'p') continue;
    const prose = [];
    for (const line of block.text.split('\n')) {
      if (!rawKind(line)) prose.push(line.trim());
      else if (prose.length) break;
    }
    const value = prose.join(' ').trim();
    if (!value) continue;
    const chars = Array.from(value);
    return chars.length > 320 ? chars.slice(0, 319).join('') + '…' : value;
  }
  return Array.from(fallback).slice(0, 320).join('');
}

export function renderMarkdown(doc, text, {key = 'message', source = false} = {}) {
  const root = element(doc, 'div', undefined, 'office-markdown');
  for (const block of markdownBlocks(text)) {
    if (block.tag === 'details') {
      root.append(foldText(doc, block.label, block.text, key + ':line:' + block.start));
    } else if (block.tag === 'table') {
      root.append(renderTable(doc, block.rows));
    } else if (block.items) {
      const list = element(doc, block.tag);
      if (block.tag === 'ol') list.start = block.number;
      for (const item of block.items) {
        const li = element(doc, 'li');
        inline(doc, li, item);
        list.append(li);
      }
      root.append(list);
    } else {
      const node = element(doc, block.tag);
      inline(doc, node, block.text);
      root.append(node);
    }
  }
  if (source) root.append(foldText(doc, '원문 보기', text, key + ':source'));
  return root;
}

// Git's default quotePath represents non-ASCII UTF-8 bytes as octal inside quoted paths.
function gitName(name) {
  if (!name.startsWith('"') || !name.endsWith('"')) return name;
  const escapes = {'"': '"', '\\': '\\', t: '\t', r: '\r', n: '\n', b: '\b', f: '\f', v: '\v', a: '\x07'};
  return name.slice(1, -1).replace(/(?:\\[0-7]{3})+|\\(["\\trnbfva])/g, (match, escaped) => {
    if (escaped) return escapes[escaped];
    return new TextDecoder().decode(Uint8Array.from(match.match(/[0-7]{3}/g), byte => parseInt(byte, 8)));
  });
}

const shortPath = header => gitName(header.split('\t')[0]).replace(/^[ab]\//, '');
const clipped = text => text.includes('...[truncated]') || text.includes('… [truncated]');
const omittedNew = '일부 새 파일은 변경 파일 목록과 작업 폴더에서 확인하세요.';

function gitHeaderPath(row) {
  const header = row.slice('diff --git '.length);
  // Git leaves spaces unquoted here. The same path on both sides disambiguates " b/" in a name.
  for (const match of header.matchAll(/ b\//g)) {
    const left = header.slice(2, match.index);
    if (header.startsWith('a/') && left === header.slice(match.index + 3)) return left;
  }
  const second = header.match(/\s("b\/(?:[^"\\]|\\.)*"|b\/.*)$/);
  return second ? shortPath(second[1]) : '';
}

function patchFiles(diff) {
  const lines = diff.match(/[^\n]*\n|[^\n]+$/g) ?? [];
  const rows = lines.map(line => line.replace(/\r?\n$/, ''));
  const files = [];
  let part, oldLeft = 0, newLeft = 0;
  function begin(path = '', git = false) {
    part = {path, git, headers: false, raw: [], classes: [], added: 0, deleted: 0, hunks: 0, partial: false, binary: false};
    files.push(part);
    oldLeft = newLeft = 0;
  }
  function finishHunk() {
    if (oldLeft || newLeft) part.partial = true;
    oldLeft = newLeft = 0;
  }
  for (let i = 0; i < rows.length; i++) {
    const row = rows[i];
    let cls = '';
    if (gitStart.test(row)) {
      finishHunk();
      begin(gitHeaderPath(row), true);
    }
    // Within a hunk, "--- x" and "+++ y" can be deleted/added lines of source.
    if (!oldLeft && !newLeft && unifiedStart(row, rows[i + 1])) {
      const oldPath = shortPath(row.slice(4));
      const newPath = shortPath(rows[i + 1].slice(4));
      const path = newPath === '/dev/null' ? oldPath : newPath;
      if (!part?.git || part.headers || part.hunks) begin(path);
      part.path = path;
      part.headers = true;
      part.raw.push(lines[i], lines[i + 1]);
      part.classes.push('diff-meta', 'diff-meta');
      i++;
      continue;
    }
    if (!part) begin();
    const hunk = row.match(hunkHeader);
    if (hunk) {
      finishHunk();
      oldLeft = Number(hunk[1] ?? 1);
      newLeft = Number(hunk[2] ?? 1);
      part.hunks++;
      cls = 'diff-hunk';
    } else if (row.startsWith('\\ No newline at end of file')) {
      cls = 'diff-meta';
    } else if (oldLeft || newLeft) {
      if (row.startsWith('+') && newLeft) {
        newLeft--;
        part.added++;
        cls = 'diff-added';
      } else if (row.startsWith('-') && oldLeft) {
        oldLeft--;
        part.deleted++;
        cls = 'diff-deleted';
      } else if (row.startsWith(' ') && oldLeft && newLeft) {
        oldLeft--;
        newLeft--;
      } else finishHunk();
    } else if (row.startsWith('rename to ') || row.startsWith('copy to ')) {
      part.path = gitName(row.slice(row.indexOf(' to ') + 4));
    } else if (row.startsWith('Binary files ') || row === 'GIT binary patch') {
      part.binary = true;
    } else if (/^[+-]/.test(row) && part.hunks) {
      part.partial = true;
    }
    if (clipped(row)) part.partial = true;
    part.raw.push(lines[i]);
    part.classes.push(cls);
  }
  finishHunk();
  return files;
}

function statusFiles(text) {
  const files = new Map(), unknown = [];
  for (const row of text.split(/\r?\n/)) {
    if (!row) continue;
    const entry = row.match(/^([ MADRCUT?!]{2}|[MADRCUT])\s(.+)$/);
    if (!entry || clipped(row)) {
      unknown.push(row);
      continue;
    }
    const [, status, names] = entry;
    const rename = /[RC]/.test(status) && names.match(/^(?:"(?:\\.|[^"\\])*"|.*?) -> (.+)$/);
    const name = rename ? rename[1] : names;
    files.set(gitName(name), {status: status.trim(), row});
  }
  return {files, unknown};
}

const metrics = (added, deleted) => ' · +' + added + ' −' + deleted;
const hasMetrics = file => file.hunks > 0 && !file.partial && !file.binary;

export function renderDiff(doc, {changes = '', diff = '', key = 'diff'} = {}) {
  const root = element(doc, 'div', undefined, 'office-diff');
  const status = statusFiles(changes);
  const files = patchFiles(diff);
  const paths = new Set(files.map(file => file.path).filter(Boolean));
  for (const path of status.files.keys()) {
    if (!paths.has(path)) files.push({path});
    paths.add(path);
  }
  const summary = element(doc, 'p', paths.size ? '변경 파일 ' + paths.size + '개' : '변경 내용');
  const partial = clipped(changes) || clipped(diff) || diff.includes(omittedNew) || status.unknown.length > 0;
  if (files.length && !partial && files.every(file => file.path && hasMetrics(file))) {
    summary.append(metrics(files.reduce((n, f) => n + f.added, 0), files.reduce((n, f) => n + f.deleted, 0)));
  }
  if (partial) summary.append(' · 일부 기록');
  root.append(summary);
  const occurrences = new Map();
  for (const file of files) {
    const name = file.path || '파일 미확인';
    const sequence = (occurrences.get(file.path) ?? 0) + 1;
    occurrences.set(file.path, sequence);
    const blockKey = key + ':file:' + JSON.stringify([file.path, sequence]);
    const info = status.files.get(file.path);
    const section = disclosure(doc, name, blockKey);
    const label = section.firstChild;
    if (info) label.append(element(doc, 'span', ' · ' + info.status, 'diff-status'));
    if (hasMetrics(file)) label.append(metrics(file.added, file.deleted));
    if (file.binary) label.append(' · 바이너리');
    if (file.partial) label.append(' · 일부 기록');
    if (file.raw) {
      if (info && /[RC]/.test(info.status)) section.append(element(doc, 'p', info.row));
      section.append(rawView(doc, file.raw.join(''), blockKey, pre => {
        file.raw.forEach((line, i) => pre.append(element(doc, 'span', line, file.classes[i])));
      }));
    } else {
      section.append(element(doc, 'p', '파일 상태만 기록되었습니다.'), rawView(doc, info.row, blockKey));
    }
    root.append(section);
  }
  if (status.unknown.length) root.append(foldText(doc, '변경 파일 목록 원문', changes, key + ':status'));
  if (!files.length && !status.unknown.length) root.append(element(doc, 'p', '기록된 변경 내용이 없습니다.'));
  return root;
}
