const isId = value => typeof value === 'string' && /^[0-9a-f]{32}$/.test(value);
const isRevision = value => typeof value === 'string' && /^(?:[0-9a-f]{40}|[0-9a-f]{64})$/.test(value);
const engines = {codex: 'Codex', antigravity: 'Antigravity', claude: 'Claude Code', gemini: 'Gemini CLI', cline: 'Cline CLI'};
const labels = {
  generating: '생성 중', ready: '제안 작성 완료', no_proposal: '제안 없음', failed: '실패', interrupted: '생성 중단',
  submitted: '후보 접수', passed: '통과', running: '검증 중', approved: '승인', rejected: '거절',
  validation_started: '검증 시작', validation_finished: '검증 종료',
  PLAN_ONLY: '계획 완료', APPROVED: '리뷰 통과', DEVELOPER_FAILED: '개발 실패',
  TEST_FAILED: '테스트 실패', REVIEW_FAILED: '리뷰 실패', CHANGES_REQUESTED: '수정 요청',
  REVIEW_UNDECIDED: '리뷰 미결정', CANCELED: '중지됨', INTERRUPTED: '실행 중단',
};
const label = value => Object.hasOwn(labels, value) ? labels[value] : value;
const dateLabel = stamp => Number.isFinite(Date.parse(stamp)) ? new Date(stamp).toLocaleString('ko-KR') : '시각 미확인';
const decisionStatus = (row, stable) => label(row.decision) ||
  (row.stale_base || (stable && row.base !== stable) ? '기준 변경 · 재제안 필요' : '결정 대기');
const categories = ['runs', 'jobs', 'candidates'];
const emptyMessage = {runs: '선택할 수 있는 종료 실행이 없습니다.', jobs: '아직 생성 내역이 없습니다.', candidates: '아직 게시된 후보가 없습니다.'};

function addressHost(win) {
  const values = new URL(win.location.href).searchParams.getAll('host');
  if (!values.length) return 'local';
  return values.length === 1 && (values[0] === 'local' || isId(values[0])) ? values[0] : null;
}

function validPayload(body) {
  return isId(body?.id) && isRevision(body.base) && Object.hasOwn(engines, body.engine) &&
    Array.isArray(body.run_ids) && body.run_ids.length >= 1 && body.run_ids.length <= 8 &&
    body.run_ids.every(isId) && new Set(body.run_ids).size === body.run_ids.length;
}

function matches(body, job) {
  return job?.id === body.id && job.engine === body.engine && job.base === body.base &&
    Array.isArray(job.run_ids) && job.run_ids.length === body.run_ids.length && job.run_ids.every((id, i) => body.run_ids[i] === id);
}

export function mountImprovements(win) {
  const doc = win.document;
  const get = id => doc.getElementById(id);
  const node = (tag, value) => {
    const element = doc.createElement(tag);
    if (value != null) element.textContent = value;
    return element;
  };
  const say = (id, value) => { if (get(id).textContent !== value) get(id).textContent = value; };
  const hostId = addressHost(win);
  const prefix = hostId === 'local' ? '/api' : '/api/hosts/' + hostId;
  const storageKey = 'agent-office.improvements.pending.' + hostId;
  const current = () => !disposed && addressHost(win) === hostId;
  const pages = Object.fromEntries(categories.map(kind => [kind, {offset: 0, back: [], seq: 0, rows: [], next: null}]));
  const checks = new Set();
  let disposed = false, timer, host, available = false, config, pending, sending = false;
  let cycle = 0, detailSeq = 0, activeDetail, detailSignature, hostSignature;
  const controllers = new Set();

  function route(path, params = {}, selectedHost = hostId) {
    const query = new URLSearchParams();
    if (selectedHost && selectedHost !== 'local') query.set('host', selectedHost);
    for (const [key, value] of Object.entries(params)) query.set(key, value);
    return path + (query.size ? '?' + query : '');
  }
  if (hostId) {
    get('versions-link').href = '/versions?host=' + hostId;
    get('studio-link').href = route('/');
    get('office-link').href = route('/office', hostId === 'local' ? {host: 'local'} : {});
  }
  const query = new URL(win.location.href).searchParams;
  for (const kind of ['job', 'candidate']) {
    if (isId(query.get(kind))) activeDetail = {kind, id: query.get(kind)};
  }
  try {
    const saved = JSON.parse(win.sessionStorage.getItem(storageKey));
    if (validPayload(saved)) {
      pending = saved;
      saved.run_ids.forEach(id => checks.add(id));
      get('engine').value = saved.engine;
      say('submit-note', '이전 요청의 접수 여부를 확인하고 있습니다.');
    }
  } catch { /* No saved request. */ }

  async function request(path, body) {
    if (!current()) throw new Error('실행 호스트가 변경되었습니다.');
    const controller = new win.AbortController();
    controllers.add(controller);
    const timeout = win.setTimeout(() => controller.abort(), 15000);
    try {
      const options = {signal: controller.signal, mode: 'same-origin', redirect: 'error', cache: 'no-store'};
      if (body) Object.assign(options, {method: 'POST', headers: {'Content-Type': 'application/json', 'X-Agent-Office': 'studio'}, body: JSON.stringify(body)});
      const response = await win.fetch(path, options);
      const data = await response.json();
      if (!response.ok) {
        const error = new Error(data.error?.message || '요청을 처리할 수 없습니다.');
        error.status = response.status;
        error.code = data.error?.code;
        throw error;
      }
      return data;
    } finally {
      win.clearTimeout(timeout);
      controllers.delete(controller);
    }
  }
  const api = (suffix, body) => request(prefix + '/improvements' + suffix, body);

  function controls() {
    const allowed = current() && available && config?.source_enabled === true && config.view?.available === true && isRevision(config.view.stable);
    get('evidence').disabled = !allowed || !!pending || sending;
    get('engine').disabled = get('evidence').disabled;
    get('generate').disabled = !allowed || sending || (!pending && (!checks.size || checks.size > 8 || pages.runs.stale));
    say('generate', sending ? '접수 확인 중…' : pending ? '같은 요청으로 다시 확인' : '개선안 생성');
    for (const box of get('runs').querySelectorAll('input')) {
      box.checked = checks.has(box.value);
      box.disabled = !box.checked && checks.size >= 8;
    }
    say('selection-count', checks.size + '개 선택 · 최대 8개');
  }

  function detailURL(kind, id) { return route('/improvements', {[kind]: id}) + '#detail'; }
  function linkToDetail(kind, id, title) {
    const link = node('a', title);
    link.href = detailURL(kind, id);
    link.dataset.kind = kind;
    link.dataset.id = id;
    link.addEventListener('click', event => {
      if (event.ctrlKey || event.metaKey || event.shiftKey || event.altKey || event.button !== 0) return;
      event.preventDefault();
      selectDetail(kind, id, true);
    });
    return link;
  }
  function markSelection() {
    for (const link of doc.querySelectorAll('.record-list a')) {
      if (link.dataset.id === activeDetail?.id && link.dataset.kind === activeDetail.kind) link.setAttribute('aria-current', 'true');
      else link.removeAttribute('aria-current');
    }
  }
  function selectDetail(kind, id, focus) {
    if (!current() || !isId(id)) return;
    activeDetail = {kind, id};
    detailSignature = undefined;
    get('detail-body').replaceChildren();
    get('detail-body').hidden = true;
    say('detail-title', kind === 'job' ? '생성 상세' : '후보 상세');
    say('detail-note', '상세를 불러오는 중입니다.');
    win.history.replaceState(null, '', detailURL(kind, id));
    markSelection();
    if (focus) get('detail-title').focus();
    loadDetail();
  }

  function runLinks(body, ids, studioIds) {
    const list = node('ul');
    for (const id of ids.filter(isId)) {
      const item = node('li');
      if (studioIds.includes(id)) {
        const link = node('a', id);
        link.href = route('/', {run: id}) + '#detail';
        item.append(link);
      } else item.textContent = id + ' · CLI 실행';
      list.append(item);
    }
    body.append(node('h4', '근거 실행'), list);
  }
  function fields(rows) {
    const dl = node('dl');
    for (const [name, value] of rows) dl.append(node('dt', name), node('dd', value ?? '기록 없음'));
    return dl;
  }
  function section(body, heading, value, tag = 'p') {
    body.append(node('h4', heading), node(tag, value || '기록 없음'));
  }
  function renderJob(body, data) {
    const {job, candidate_id: candidate, proposal} = data;
    body.append(node('h3', job.title || '개선안 생성 요청'), fields([
      ['진행 상태', label(job.state)], ['요청 ID', job.id], ['제안 엔진', engines[job.engine]], ['기준 SHA', job.base],
    ]));
    if (job.message || data.error) section(body, '처리 결과', [job.message, data.error].filter(Boolean).join('\n'));
    if (proposal) {
      section(body, '제안 근거', proposal.rationale);
      section(body, '기대 효과', proposal.expected);
      section(body, '엔진이 제안한 패치', proposal.patch, 'pre');
    }
    runLinks(body, job.run_ids, data.studio_run_ids);
    if (data.publication_available === false) {
      body.append(node('p', '후보 게시 자료를 읽을 수 없어 최신 접수 상태를 확인하지 못했습니다.'));
      if (activeDetail.lastCandidate) body.append(linkToDetail('candidate', activeDetail.lastCandidate, '마지막으로 확인한 후보 보기'));
    } else {
      activeDetail.lastCandidate = candidate;
      if (isId(candidate)) body.append(linkToDetail('candidate', candidate, '접수된 후보의 변경·검증 결과 보기'));
      else if (job.state === 'ready' && !data.error) body.append(node('p', '운영자가 접수한 후보를 게시하면 이곳에 표시됩니다.'));
    }
  }
  function renderCandidate(body, c, studioIds) {
    body.append(node('h3', c.title), fields([['후보 ID', c.id], ['기준 SHA', c.base], ['후보 SHA', c.sha],
      ['검증 이미지', c.image], ['검증 결과', label(c.validation) || '대기'], ['종료 코드', c.exit_code],
      ['운영자 결정', decisionStatus(c, config.view.stable)]]));
    if (isId(c.job_id)) body.append(linkToDetail('job', c.job_id, '원래 생성 요청 보기'));
    section(body, '제안 근거', c.rationale);
    section(body, '기대 효과', c.expected);
    runLinks(body, c.run_ids, studioIds);
    section(body, '후보의 실제 변경 내용 (diff)', c.diff, 'pre');
    if (c.diff_truncated) body.append(node('small', '변경 내용의 일부만 게시되었습니다. 전체 diff는 운영자 터미널에서 확인하세요.'));
    section(body, '검증 출력', c.validation_output, 'pre');
    if (c.output_truncated) body.append(node('small', '검증 출력의 일부만 게시되었습니다. 전체 출력은 운영자 터미널에서 확인하세요.'));
    body.append(node('h4', '검증·결정 이력'));
    if (c.history_omitted > 0) body.append(node('small', '이전 기록 ' + c.history_omitted + '건은 운영자 터미널에서 확인하세요.'));
    const history = node('ol');
    for (const entry of c.history) history.append(node('li', dateLabel(entry.time) + ' · ' + label(entry.event)));
    body.append(history);
  }

  async function loadDetail() {
    if (!activeDetail) return;
    const {kind, id} = activeDetail;
    const seq = ++detailSeq;
    const live = () => current() && seq === detailSeq;
    const heading = kind === 'job' ? '생성 상세' : '후보 상세';
    say('detail-title', heading);
    try {
      if (kind === 'candidate' && !config?.view.available) throw new Error('후보 조회 자료를 확인할 수 없습니다.');
      const data = await api('/' + kind + 's/' + id);
      if (!live()) return;
      if (data[kind]?.id !== id) throw new Error('상세 기록 ID가 다릅니다.');
      const signature = JSON.stringify([data, kind === 'candidate' && config.view.stable]);
      if (signature !== detailSignature) {
        const content = doc.createDocumentFragment();
        if (kind === 'job') renderJob(content, data);
        else renderCandidate(content, data.candidate, data.studio_run_ids);
        get('detail-body').replaceChildren(content);
        detailSignature = signature;
      }
      get('detail-body').hidden = false;
      say('detail-note', '');
    } catch {
      if (live()) say('detail-note', detailSignature ? '상세 갱신 실패 · 마지막으로 받은 기록을 표시합니다.' : '이 호스트의 상세 기록을 불러올 수 없습니다.');
    }
  }
  function renderPage(kind, page) {
    const list = get(kind);
    const focusId = list.contains(doc.activeElement) ? doc.activeElement.value || doc.activeElement.dataset.id : null;
    const signature = JSON.stringify([page.rows, kind === 'candidates' && config.view.stable]);
    if (page.signature !== signature) {
      list.replaceChildren(...page.rows.map(row => {
        const item = node('li');
        if (kind === 'runs') {
          const wrap = node('label'), box = node('input'), text = node('span');
          wrap.className = 'check';
          box.type = 'checkbox';
          box.name = 'run';
          box.value = row.id;
          box.checked = checks.has(row.id);
          box.addEventListener('change', () => {
            if (box.checked && checks.size < 8) checks.add(row.id);
            else checks.delete(row.id);
            controls();
          });
          text.append(node('strong', row.request), node('small', label(row.outcome || row.state) + ' · ' + row.id.slice(0, 8)));
          wrap.append(box, text);
          item.append(wrap);
        } else {
          const isJob = kind === 'jobs';
          const link = linkToDetail(isJob ? 'job' : 'candidate', row.id);
          const status = isJob ? [engines[row.engine], label(row.state)].join(' · ') :
            '검증 ' + (label(row.validation) || '대기') + ' · 운영자 ' + decisionStatus(row, config.view.stable);
          link.append(node('strong', row.title || (isJob ? '개선안 생성 요청' : '검토 후보')),
            node('small', status + ' · ' + row.id.slice(0, 8)));
          item.append(link);
        }
        return item;
      }));
      page.signature = signature;
      if (focusId) [...list.querySelectorAll('a,input')].find(el => (el.value || el.dataset.id) === focusId)?.focus({preventScroll: true});
    }
    const pager = get(kind + '-pages');
    const pageKey = [page.offset, page.next].join('/');
    if (pager.dataset.key !== pageKey) {
      pager.dataset.key = pageKey;
      pager.replaceChildren();
      if (page.offset || page.next !== null) {
        const previous = node('button', '이전'), next = node('button', '다음');
        for (const button of [previous, next]) { button.type = 'button'; button.className = 'link-button'; }
        previous.disabled = !page.back.length;
        next.disabled = page.next === null;
        previous.addEventListener('click', () => loadPage(kind, page.back.at(-1), 'previous'));
        next.addEventListener('click', () => loadPage(kind, page.next, 'next'));
        pager.append(previous, node('small', (page.back.length + 1) + '페이지'), next);
      }
    }
    markSelection();
    controls();
  }

  async function loadPage(kind, offset = pages[kind].offset, move = null) {
    const page = pages[kind], seq = ++page.seq;
    const note = kind + '-note';
    try {
      const result = await api('/' + kind + '/page/' + offset);
      if (!current() || seq !== page.seq) return;
      const {items, next_offset: next} = result;
      if (!Array.isArray(items) || !items.every(row => isId(row.id)) ||
          !(next === null || (Number.isSafeInteger(next) && next > offset))) throw new Error('목록을 확인할 수 없습니다.');
      if (move === 'first') page.back = [];
      if (move === 'previous') page.back.pop();
      if (move === 'next') page.back.push(page.offset);
      Object.assign(page, {offset, next, rows: items, stale: false});
      say(note, items.length ? '' : emptyMessage[kind]);
      get(note).dataset.stale = 'false';
      renderPage(kind, page);
    } catch {
      if (!current() || seq !== page.seq) return;
      page.stale = true;
      say(note, page.signature ? '갱신 실패 · 마지막으로 받은 목록을 표시합니다.' : '목록을 불러올 수 없습니다. 새로고침으로 다시 확인하세요.');
      get(note).dataset.stale = 'true';
      controls();
    }
  }

  function clearPending(body) {
    if (pending?.id !== body.id) return;
    pending = null;
    try { win.sessionStorage.removeItem(storageKey); } catch { /* A saved receipt can still be reconciled on reload. */ }
    controls();
  }
  function accepted(body, job) {
    if (!matches(body, job)) throw new Error('접수된 요청의 기준·엔진·근거 실행이 다릅니다.');
    checks.clear();
    clearPending(body);
    say('submit-note', '이 호스트에 접수했습니다. 생성 내역에서 진행 상태를 확인하세요.');
  }

  async function lookup(id) {
    try { return (await api('/jobs/' + id)).job; }
    catch (error) {
      if (error.status === 404 && error.code === 'NOT_FOUND') return null;
      throw error;
    }
  }

  async function reconcile() {
    const saved = pending;
    if (!saved || sending) return;
    try {
      const job = await lookup(saved.id);
      if (!current() || saved !== pending || sending) return;
      if (!job) {
        say('submit-note', '아직 접수가 확인되지 않았습니다. 같은 요청으로 다시 확인할 수 있습니다.');
        return;
      }
      accepted(saved, job);
      if (!activeDetail) selectDetail('job', saved.id, false);
    } catch {
      if (!current() || saved !== pending || sending) return;
      say('submit-note', '이전 요청의 접수 여부를 확인할 수 없습니다. 다시 확인할 때 같은 요청 ID를 사용합니다.');
    }
  }

  async function submit(event) {
    event.preventDefault();
    if (!current() || sending || get('generate').disabled) return;
    const retrying = !!pending, previousSelection = activeDetail;
    let body = pending;
    if (!body) {
      body = {id: win.crypto.randomUUID().replaceAll('-', ''), base: config.view.stable, engine: get('engine').value, run_ids: [...checks]};
      if (!validPayload(body)) return;
      try { win.sessionStorage.setItem(storageKey, JSON.stringify(body)); }
      catch { say('submit-note', '접수 ID를 이 탭에 저장할 수 없습니다. 브라우저 저장 공간을 확인하세요.'); return; }
      pending = body;
    }
    sending = true;
    controls();
    let submitted = false;
    try {
      let job = retrying ? await lookup(body.id) : null;
      if (!job) {
        submitted = true;
        ({job} = await api('/jobs', body));
      }
      if (!current()) return;
      accepted(body, job);
      if (activeDetail === previousSelection) selectDetail('job', body.id, true);
      loadPage('jobs', 0, 'first');
    } catch (error) {
      if (!current()) return;
      const rejected = (error.status === 400 && error.code === 'INVALID_ARGUMENT') ||
        (error.status === 409 && ['REQUEST_CONFLICT', 'RUN_BUSY'].includes(error.code));
      if (submitted && rejected) {
        clearPending(body);
        say('submit-note', '접수되지 않았습니다. ' + error.message);
      } else {
        say('submit-note', '접수 응답을 확인할 수 없습니다. 같은 요청으로 다시 확인하세요.');
      }
    } finally {
      sending = false;
      if (current()) controls();
    }
  }
  function renderHosts(hosts) {
    const signature = JSON.stringify(hosts);
    if (hostSignature === signature) return;
    hostSignature = signature;
    get('hosts').replaceChildren(...hosts.map(info => {
      const li = node('li'), link = node('a', info.name);
      link.href = route('/improvements', {}, info.id);
      if (info.id === hostId) link.setAttribute('aria-current', 'page');
      li.append(link);
      return li;
    }));
  }
  async function refresh() {
    win.clearTimeout(timer);
    if (!current() || doc.hidden) return;
    const seq = ++cycle;
    const live = () => current() && seq === cycle;
    try {
      const {hosts} = await request('/api/hosts');
      if (!live()) return;
      if (!Array.isArray(hosts) || !hosts.every(info => info && (info.id === 'local' || isId(info.id)) && typeof info.name === 'string')) throw new Error('호스트 목록을 확인할 수 없습니다.');
      renderHosts(hosts);
      host = hosts.find(info => info.id === hostId);
      if (!host) throw new Error('선택한 호스트를 찾을 수 없습니다.');
      const status = await api('');
      if (!live()) return;
      if (typeof status?.source_enabled !== 'boolean' || typeof status?.view?.available !== 'boolean') throw new Error('호스트의 개선안 설정을 확인할 수 없습니다.');
      config = status;
      available = true;
      say('connection', host.name + ' 연결됨');
      get('connection').dataset.online = 'true';
      say('host-error', '');
      say('base', isRevision(config.view.stable) ? '기준 SHA · ' + config.view.stable : '');
      say('source-note', !config.source_enabled ? '이 호스트에 제안 생성용 소스가 설정되지 않았습니다.' :
        !config.view.available || !isRevision(config.view.stable) ? '게시된 기준 SHA가 있어야 새 개선안을 생성할 수 있습니다.' : '');
      if (config.view.available) say('published', '마지막 게시 · ' + dateLabel(config.view.updated_at));
      else {
        const previousStamp = get('published').dataset.stamp;
        say('published', previousStamp ? '게시 자료 갱신 실패 · 마지막 게시 ' + previousStamp : '운영자가 게시한 후보 조회 자료를 확인할 수 없습니다.');
        ++pages.candidates.seq;
        say('candidates-note', pages.candidates.rows.length ? '마지막으로 받은 후보 목록을 표시합니다.' : '');
      }
      if (config.view.available) get('published').dataset.stamp = dateLabel(config.view.updated_at);
      controls();
      const lists = categories.filter(kind => kind !== 'candidates' || config.view.available);
      await Promise.all([...lists.map(kind => loadPage(kind)), loadDetail(), reconcile()]);
    } catch (error) {
      if (!live()) return;
      available = false;
      say('connection', host ? host.name + ' 연결 확인 실패' : '연결 확인 실패');
      get('connection').dataset.online = 'false';
      say('host-error', config ? '연결이 끊겼습니다. 화면에는 마지막으로 받은 기록이 남아 있습니다.' :
        host ? '호스트에서 개선안 자료를 받을 수 없습니다. 연결과 작업실 버전을 확인하세요.' : error.message);
      controls();
    } finally {
      if (live() && !doc.hidden) timer = win.setTimeout(refresh, 6000);
    }
  }
  function visibility() {
    if (!doc.hidden) refresh();
    else win.clearTimeout(timer);
  }
  get('refresh').addEventListener('click', refresh);
  get('proposal-form').addEventListener('submit', submit);
  doc.addEventListener('visibilitychange', visibility);
  refresh();
  return () => {
    disposed = true;
    win.clearTimeout(timer);
    for (const controller of controllers) controller.abort();
    doc.removeEventListener('visibilitychange', visibility);
    get('refresh').removeEventListener('click', refresh);
    get('proposal-form').removeEventListener('submit', submit);
  };
}

if (typeof window !== 'undefined') mountImprovements(window);
