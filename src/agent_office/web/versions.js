const events = {initial: '초기 버전', promotion: '승격', rollback: '롤백'};
const statuses = {added: '추가', deleted: '삭제', modified: '수정'};
const validHost = id => id === 'local' || (typeof id === 'string' && /^[a-f0-9]{32}$/.test(id));
const validSeq = n => Number.isSafeInteger(n) && n >= 0;
const validSha = sha => typeof sha === 'string' && /^(?:[a-f0-9]{40}|[a-f0-9]{64})$/.test(sha);
const sameVersion = (a, b) => a?.seq === b?.seq && a?.sha === b?.sha;
const stamp = time => Number.isFinite(Date.parse(time)) ? new Date(time).toLocaleString('ko-KR') : '시각 미확인';

function addressHost(win) {
  const ids = new URL(win.location.href).searchParams.getAll('host');
  if (!ids.length) return 'local';
  return ids.length === 1 && validHost(ids[0]) ? ids[0] : null;
}

function validEntry(row) {
  return row && validSeq(row.seq) && row.seq > 0 && Object.hasOwn(events, row.kind) && validSha(row.sha) &&
    (row.previous === null || validSha(row.previous)) && (row.target_seq === null || validSeq(row.target_seq));
}

function checkPage(page, offset) {
  if (page?.available === false) return page;
  if (page?.available !== true) throw new Error('게시된 안정 버전 자료를 확인할 수 없습니다.');
  if (!Array.isArray(page.items) || !page.items.every(validEntry) ||
      !(page.current === null || validEntry(page.current)) ||
      (page.next_offset !== null && (!validSeq(page.next_offset) || page.next_offset <= offset)) ||
      (!offset && !sameVersion(page.current, page.items[0])) ||
      (page.items.length && !page.current)) throw new Error('버전 이력 응답을 확인할 수 없습니다.');
  return page;
}

function checkComparison(result, from, to) {
  if (result?.from_seq !== from.seq || result.from_sha !== from.sha ||
      result.to_seq !== to.seq || result.to_sha !== to.sha) throw new Error('선택한 버전과 게시된 비교 자료가 다릅니다.');
  if (!Array.isArray(result.files) || !validSeq(result.total_files) || result.files.length > result.total_files ||
      !result.files.every(file => file && typeof file.path === 'string' && Object.hasOwn(statuses, file.status)) ||
      typeof result.truncated !== 'boolean' || !(result.diff == null || typeof result.diff === 'string')) {
    throw new Error('변경 파일과 본문을 확인할 수 없습니다.');
  }
}

export function mountVersions(win) {
  const doc = win.document;
  const get = id => doc.getElementById(id);
  const element = (tag, text) => {
    const result = doc.createElement(tag);
    if (text != null) result.textContent = text;
    return result;
  };
  const say = (id, text) => { if (get(id).textContent !== text) get(id).textContent = text; };
  const controllers = new Set();
  let hostId, prefix, page, offset, from, to, manual, online, hostName;
  let pageSignature, hostsSignature, comparisonSignature, options;
  let comparisonStale = false;
  let generation = 0, compareSequence = 0, timer, disposed = false;

  function resetHost() {
    hostId = addressHost(win);
    prefix = hostId === 'local' ? '/api/versions' : '/api/hosts/' + hostId + '/versions';
    const query = hostId ? '?host=' + hostId : '';
    for (const [id, path] of [['studio-link', '/'], ['office-link', '/office'], ['improvements-link', '/improvements']]) {
      get(id).href = path + query;
    }
    page = from = to = pageSignature = hostsSignature = hostName = null;
    offset = 0;
    online = manual = false;
    options = new Map();
    ++generation;
    for (const controller of controllers) controller.abort();
    for (const id of ['history', 'hosts', 'from-version', 'to-version']) get(id).replaceChildren();
    clearComparison();
    say('current-version', '확인 중');
    say('current-sha', '');
    say('published', '');
    say('connection', '연결 확인 중');
    delete get('connection').dataset.online;
    say('page-note', '');
    controls();
  }
  function controls() {
    for (const id of ['from-version', 'to-version']) get(id).disabled = !online || !options.size;
    get('older').disabled = !online || page?.next_offset == null;
    get('newer').disabled = !online || !offset;
    get('older').parentElement.hidden = !offset && page?.next_offset == null;
  }
  function unpublished() {
    page = from = to = pageSignature = null;
    offset = 0;
    manual = false;
    options.clear();
    for (const id of ['history', 'from-version', 'to-version']) get(id).replaceChildren();
    clearComparison();
    say('current-version', '게시된 버전 자료 없음');
    say('current-sha', '');
    say('published', '');
    say('compare-note', '이 호스트의 버전 이력이 게시되면 비교할 수 있습니다.');
  }
  function clearComparison() {
    ++compareSequence;
    comparisonSignature = undefined;
    get('comparison').hidden = true;
    get('files').replaceChildren();
    say('diff', '');
    say('file-count', '');
    say('compare-note', '');
  }
  const live = () => !disposed && addressHost(win) === hostId;
  async function json(path) {
    const controller = new win.AbortController();
    const timeout = win.setTimeout(() => controller.abort(), 15000);
    controllers.add(controller);
    try {
      const response = await win.fetch(path, {signal: controller.signal, mode: 'same-origin', redirect: 'error', cache: 'no-store'});
      if (!response.ok) {
        const error = new Error('호스트의 게시 자료를 불러올 수 없습니다.');
        error.status = response.status;
        throw error;
      }
      return await response.json();
    } finally {
      win.clearTimeout(timeout);
      controllers.delete(controller);
    }
  }
  function showHosts(hosts) {
    if (!Array.isArray(hosts) || !hosts.every(h => h && validHost(h.id) && typeof h.name === 'string')) {
      throw new Error('호스트 목록을 확인할 수 없습니다.');
    }
    const signature = JSON.stringify(hosts);
    if (hostsSignature !== signature) {
      get('hosts').replaceChildren(...hosts.map(h => {
        const li = element('li'), link = element('a', h.name);
        link.href = '/versions?host=' + h.id;
        if (h.id === hostId) link.setAttribute('aria-current', 'page');
        li.append(link);
        return li;
      }));
      hostsSignature = signature;
    }
    const host = hosts.find(h => h.id === hostId);
    if (!host) {
      unpublished();
      hostName = null;
      throw new Error('선택한 호스트를 찾을 수 없습니다.');
    }
    hostName = host.name;
  }
  function showPage(next, requested) {
    const currentChanged = !sameVersion(page?.current, next.current);
    page = next;
    offset = requested;
    const signature = JSON.stringify([page, offset]);
    if (signature === pageSignature) return;
    pageSignature = signature;
    const current = page.current;
    say('current-version', current ? '#' + current.seq + ' · ' + events[current.kind] : '게시된 안정 버전 없음');
    say('current-sha', current?.sha || '');
    say('published', '마지막 게시 · ' + stamp(page.updated_at));
    say('page-number', Math.floor(offset / 24) + 1 + '페이지');
    get('history').replaceChildren(...page.items.map(row => {
      const li = element('li'), time = element('time', stamp(row.time));
      if (row.time) time.dateTime = row.time;
      const kind = row.kind === 'rollback' ? '롤백 · #' + row.target_seq + ' 복원' : events[row.kind];
      const route = (row.previous ? row.previous.slice(0, 10) + ' → ' : '') + row.sha.slice(0, 10);
      li.append(element('strong', '#' + row.seq + ' · ' + kind), time, element('small', route));
      return li;
    }));
    const rows = [current, ...page.items].filter(Boolean);
    const replaced = selected => selected && (!current || selected.seq > current.seq ||
      rows.some(row => row.seq === selected.seq && !sameVersion(row, selected)));
    if (!current || replaced(from) || replaced(to) || (currentChanged && !manual)) {
      manual = false;
      from = to = null;
    }
    if (!manual && offset === 0) {
      from = page.items[1] || current;
      to = current;
    }
    options = new Map([from, to, ...rows].filter(Boolean).map(row => [row.seq, row]));
    const ordered = [...options.values()].sort((a, b) => b.seq - a.seq);
    for (const [id, selected] of [['from-version', from], ['to-version', to]]) {
      const select = get(id);
      select.replaceChildren(...ordered.map(row => {
        const option = element('option', '#' + row.seq + ' · ' + events[row.kind] + ' · ' + row.sha.slice(0, 10));
        option.title = row.sha;
        option.value = String(row.seq);
        return option;
      }));
      if (selected) select.value = String(selected.seq);
    }
  }
  async function compare(reuse = false) {
    const sequence = ++compareSequence;
    const active = () => live() && sequence === compareSequence;
    if (!from || !to || !online) return;
    const direction = '#' + from.seq + ' → #' + to.seq;
    const key = [from.seq, from.sha, to.seq, to.sha].join('/');
    if (reuse && !comparisonStale && comparisonSignature === key &&
        [from, to].every(v => [page.current, ...page.items].some(r => sameVersion(r, v)))) return;
    if (comparisonSignature !== key) {
      comparisonSignature = undefined;
      get('comparison').hidden = true;
      say('compare-note', direction + ' 비교 자료를 불러오는 중입니다.');
    }
    try {
      const result = await json(prefix + '/compare/' + from.seq + '/' + to.seq);
      if (!active()) return;
      try {
        checkComparison(result, from, to);
      } catch (err) {
        clearComparison();
        say('compare-note', direction + ' · ' + err.message);
        return;
      }
      comparisonSignature = key;
      comparisonStale = false;
      say('file-count', '변경 파일 ' + result.total_files + '개');
      get('files').replaceChildren(...result.files.map(file => {
        const mode = file.before_mode && file.after_mode && file.before_mode !== file.after_mode ?
          ' (' + file.before_mode + ' → ' + file.after_mode + ')' : '';
        return element('li', statuses[file.status] + ' · ' + file.path + mode);
      }));
      const partial = result.truncated || result.files.length < result.total_files;
      say('diff', result.diff || (result.total_files === 0 ? '변경된 파일이 없습니다.' : '게시된 diff 본문이 없습니다.'));
      say('compare-note', direction + (partial ? ' · 일부 자료만 게시되었습니다. 전체 변경은 운영자 터미널에서 확인하세요.' : ''));
      get('comparison').hidden = false;
    } catch (err) {
      if (!active()) return;
      if (err.status === 404) clearComparison();
      comparisonStale = true;
      say('compare-note', direction + ' 비교 자료를 확인할 수 없습니다.' +
        (comparisonSignature === key ? ' 마지막으로 받은 결과를 표시합니다.' : ' 새로고침으로 다시 확인하세요.'));
    }
  }
  async function refresh(requested = offset, reuseComparison = false) {
    win.clearTimeout(timer);
    if (!live() || doc.hidden) return;
    const sequence = ++generation;
    const active = () => live() && sequence === generation;
    try {
      const {hosts} = await json('/api/hosts');
      if (!active()) return;
      showHosts(hosts);
      let next = checkPage(await json(prefix + '/page/' + requested), requested);
      if (!active()) return;
      if (next.available && requested && !sameVersion(next.current, page?.current)) {
        // New transitions shift offset-based pages, including a rollback to a previously seen SHA.
        requested = 0;
        next = checkPage(await json(prefix + '/page/0'), 0);
        if (!active()) return;
      }
      online = true;
      say('connection', hostName + ' 연결됨');
      get('connection').dataset.online = 'true';
      say('page-note', '');
      if (!next.available) {
        unpublished();
        controls();
        return;
      }
      showPage(next, requested);
      controls();
      if (page.current) await compare(reuseComparison);
      else { clearComparison(); say('compare-note', '비교할 안정 버전이 없습니다.'); }
    } catch (err) {
      if (!active()) return;
      ++compareSequence;
      if (err.status === 404) unpublished();
      online = false;
      controls();
      say('connection', (hostName || '호스트') + ' 조회 실패');
      get('connection').dataset.online = 'false';
      say('page-note', (err?.message || '자료를 불러올 수 없습니다.') +
        (page ? ' 이 화면은 마지막으로 받은 기록입니다.' : ' 새로고침으로 다시 확인하세요.'));
      if (!page) say('current-version', '확인할 수 없음');
    } finally {
      if (active() && !doc.hidden) timer = win.setTimeout(() => refresh(offset, true), 6000);
    }
  }
  function selectPair() {
    if (!live() || !online) return;
    manual = true;
    from = options.get(Number(get('from-version').value));
    to = options.get(Number(get('to-version').value));
    clearComparison();
    compare();
  }
  function locationChanged() {
    if (hostId !== addressHost(win)) resetHost();
    refresh();
  }
  const listeners = [
    [get('refresh'), 'click', () => refresh()],
    [get('newer'), 'click', () => refresh(Math.max(0, offset - 24))],
    [get('older'), 'click', () => { if (page?.next_offset != null) refresh(page.next_offset); }],
    [get('from-version'), 'change', selectPair], [get('to-version'), 'change', selectPair],
    [win, 'popstate', locationChanged], [doc, 'visibilitychange', locationChanged],
  ];
  for (const [target, event, action] of listeners) target.addEventListener(event, action);
  resetHost();
  refresh();
  return () => {
    disposed = true;
    win.clearTimeout(timer);
    for (const controller of controllers) controller.abort();
    for (const [target, event, action] of listeners) target.removeEventListener(event, action);
  };
}

if (typeof window !== 'undefined') mountVersions(window);
