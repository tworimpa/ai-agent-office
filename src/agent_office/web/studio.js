const states = {
  QUEUED: ['대기 중', 'active'],
  RUNNING: ['개발 중', 'active'],
  TESTING: ['테스트 중', 'active'],
  REVIEWING: ['리뷰 중', 'active'],
  STOP_REQUESTED: ['중지 요청 중', 'attention'],
  CANCELED: ['중지됨', 'neutral'],
  PLAN_ONLY: ['계획 작성 완료', 'done'],
  READY_FOR_HUMAN_REVIEW: ['검토 필요', 'attention'],
  WAITING_USER: ['응답 필요', 'attention'],
  FAILED: ['실패', 'error'],
  RECOVERY_REQUIRED: ['복구 필요', 'error'],
};
const stoppable = new Set(['QUEUED', 'RUNNING', 'TESTING', 'REVIEWING']);
const outcomes = {
  PLAN_ONLY: '계획 작성을 마쳤습니다.',
  APPROVED: '리뷰어가 변경을 승인했습니다. 최종 검토할 수 있습니다.',
  DEVELOPER_FAILED: '개발자 실행이 실패했습니다.',
  TEST_FAILED: '테스트가 실패했습니다.',
  REVIEW_FAILED: '리뷰어 실행이 실패했습니다.',
  CHANGES_REQUESTED: '리뷰어가 수정을 요청했습니다.',
  REVIEW_UNDECIDED: '리뷰어의 판정을 확인할 수 없어 사람의 판단이 필요합니다.',
  CANCELED: '중지 요청에 따라 실행을 종료했습니다.',
  INTERRUPTED: '실행이 중단되었습니다. 마지막 작업 상태를 확인하세요.',
};
const testedOutcomes = new Set(['TEST_FAILED', 'REVIEW_FAILED', 'CHANGES_REQUESTED', 'REVIEW_UNDECIDED', 'APPROVED']);
const evidence = ['developer_output', 'test_output', 'review_output', 'changes', 'diff'];
const validId = value => typeof value === 'string' && /^[0-9a-f]{32}$/.test(value);
const connectionErrors = {
  JOURNAL_MISMATCH: '등록한 원격 실행 저널과 다릅니다.',
  PEER_DISABLED: '원격 작업실의 연결 접수가 꺼져 있습니다.',
};

function badge(node, state) {
  const [label, tone] = states[state] ?? ['상태 미확인', 'neutral'];
  if (node.textContent !== label) node.textContent = label;
  node.dataset.tone = tone;
}

export function mountStudio(win) {
  const doc = win.document;
  const get = id => doc.getElementById(id);
  const make = tag => doc.createElement(tag);
  const say = (id, message) => {
    if (get(id).textContent !== message) get(id).textContent = message;
  };
  const fromAddress = () => new URL(win.location.href).searchParams.get('run');
  const addressHost = () => {
    const hosts = new URL(win.location.href).searchParams.getAll('host');
    if (hosts.length === 0) return 'local';
    return hosts.length === 1 && (hosts[0] === 'local' || validId(hosts[0])) ? hosts[0] : null;
  };
  const hostId = addressHost();
  const pendingKey = hostId === 'local' ? 'agent-office.studio.pending' : 'agent-office.studio.pending.' + hostId;
  let host = null, hosts = [], online = false, registering = false;
  let selected = validId(fromAddress()) ? fromAddress() : null;
  let workspaces = [];
  let runs = [];
  let listSequence = 0, detailSequence = 0, catalogSequence = 0;
  let timer, closed = false, starting = false, restoring = true;
  const stopping = new Set();
  let pending = null;
  try {
    const saved = JSON.parse(win.sessionStorage.getItem(pendingKey));
    if (validId(saved?.id) && Array.isArray(saved.indices) && typeof saved.task === 'string') pending = saved;
  } catch { /* Storage is optional in private browsing. */ }

  function keepPending(request) {
    pending = request;
    try {
      if (request) win.sessionStorage.setItem(pendingKey, JSON.stringify(request));
      else win.sessionStorage.removeItem(pendingKey);
    } catch { /* In-memory retries still use the same ID. */ }
  }

  async function fetchJSON(path, body) {
    const controller = new win.AbortController();
    const timeout = win.setTimeout(() => controller.abort(), 15000);
    try {
      const options = {signal: controller.signal, mode: 'same-origin', redirect: 'error', cache: 'no-store'};
      if (body !== undefined) Object.assign(options, {
        method: 'POST', headers: {'Content-Type': 'application/json', 'X-Agent-Office': 'studio'},
        body: JSON.stringify(body),
      });
      const response = await win.fetch(path, options);
      const data = await response.json();
      if (!response.ok) {
        const hint = response.status < 500 && data.error?.message;
        const error = new Error(hint || '서버에서 요청을 처리하지 못했습니다.');
        error.status = response.status;
        error.code = data.error?.code;
        throw error;
      }
      return data;
    } finally { win.clearTimeout(timeout); }
  }

  function api(path, body) {
    if (!host || closed || hostId !== addressHost()) throw new Error('실행 호스트를 확인하세요.');
    const prefix = hostId === 'local' ? '/api' : '/api/hosts/' + hostId;
    return fetchJSON(prefix + path, body);
  }

  function renderHosts(list) {
    host = list.find(item => item.id === hostId) ?? null;
    if (JSON.stringify(hosts) !== JSON.stringify(list)) {
      hosts = list;
      get('hosts').replaceChildren(...hosts.map(item => {
        const li = make('li'), link = make('a');
        link.textContent = item.name;
        link.href = item.id === 'local' ? '/' : '/?host=' + item.id;
        if (item.id === hostId) link.setAttribute('aria-current', 'page');
        li.append(link);
        return li;
      }));
    }
    say('paths-help', hostId === 'local' ? '로컬 폴더의 절대 경로를 한 줄에 하나씩 입력하세요.' :
      (host?.name ?? '원격 호스트') + '에 있는 폴더의 절대 경로를 한 줄에 하나씩 입력하세요.');
  }

  const workspace = () => workspaces.find(item => item.id === get('workspace').value);
  const workspaceName = id => workspaces.find(item => item.id === id)?.name ?? '작업공간';
  const option = (value, text) => new win.Option(text, value);
  const checkedExtras = () => [...get('extras').querySelectorAll(':checked')].map(box => Number(box.value));

  function controls() {
    const unavailable = !host || !online;
    get('register').disabled = unavailable || registering;
    get('run-fields').disabled = unavailable || !workspace() || starting || Boolean(pending);
    get('start').disabled = unavailable || starting || (!pending && !workspace());
    get('stop').disabled = unavailable || stopping.has(selected);
    say('start', starting ? '접수 확인 중…' : pending ? '접수 확인 후 같은 요청 재시도 ↗' : '작업 시작 ↗');
  }

  function renderExtras(checked = []) {
    const primary = Number(get('primary').value);
    const nodes = [];
    for (const [index, path] of (workspace()?.folders ?? []).entries()) {
      if (index === primary) continue;
      const label = make('label');
      label.className = 'check';
      const box = make('input');
      box.type = 'checkbox';
      box.value = String(index);
      box.checked = checked.includes(index);
      label.append(box, doc.createTextNode(path));
      nodes.push(label);
    }
    get('extras').replaceChildren(...nodes);
    get('extra-group').hidden = !nodes.length;
  }

  function renderFolders(indices = [0]) {
    const folders = workspace()?.folders ?? [];
    get('primary').replaceChildren(...folders.map((path, index) => option(index, path)));
    get('folder-fields').hidden = !folders.length;
    if (folders.length) get('primary').value = String(indices[0]);
    renderExtras(indices.slice(1));
    controls();
  }

  function renderCatalog(preferred) {
    const previous = get('workspace').value;
    const indices = [Number(get('primary').value), ...checkedExtras()];
    const wanted = preferred ?? previous;
    get('workspace').replaceChildren(...workspaces.map(item => option(item.id, item.name)));
    if (workspaces.some(item => item.id === wanted)) get('workspace').value = wanted;
    if (!workspaces.length) {
      get('workspace').add(option('', '작업공간을 등록하세요'));
      get('registration').open = true;
    }
    renderFolders(previous && previous === get('workspace').value ? indices : [0]);
  }

  async function loadCatalog(preferred) {
    const sequence = ++catalogSequence;
    try {
      const {workspaces: list} = await api('/workspaces');
      if (closed || sequence !== catalogSequence) return;
      if (JSON.stringify(list) !== JSON.stringify(workspaces) || restoring || preferred) {
        workspaces = list;
        renderCatalog(preferred ?? (restoring ? pending?.workspace_id : undefined));
        if (restoring && pending) {
          get('task').value = pending.task;
          get('edit-mode').checked = pending.allow_edits;
          renderFolders(pending.indices);
        }
        renderRuns();
      }
      restoring = false;
      say('catalog-error', '');
    } catch {
      if (!closed && sequence === catalogSequence) say('catalog-error', '작업공간 목록을 불러오지 못했습니다.');
    }
  }

  function renderRuns() {
    const list = get('run-list');
    const existing = new Map([...list.children].map(li => [li.firstChild.dataset.runId, li]));
    for (const [index, run] of runs.entries()) {
      let li = existing.get(run.id);
      if (!li) {
        li = make('li');
        const button = make('button');
        button.type = 'button';
        button.className = 'run-row';
        button.dataset.runId = run.id;
        const body = make('span'), title = make('span'), meta = make('span'), status = make('span');
        title.className = 'row-title';
        meta.className = 'row-meta';
        status.className = 'state';
        body.append(title, meta);
        button.append(body, status);
        button.addEventListener('click', () => selectRun(run.id));
        li.append(button);
      }
      existing.delete(run.id);
      const button = li.firstChild;
      button.setAttribute('aria-current', String(run.id === selected));
      const title = button.querySelector('.row-title'), meta = button.querySelector('.row-meta');
      if (title.textContent !== run.task) title.textContent = run.task;
      const info = workspaceName(run.workspace_id) + ' · ' + run.id.slice(0, 8);
      if (meta.textContent !== info) meta.textContent = info;
      badge(button.querySelector('.state'), run.state);
      if (list.children[index] !== li) list.insertBefore(li, list.children[index] ?? null);
    }
    for (const li of existing.values()) li.remove();
    say('count', String(runs.length));
  }

  function setSelection(id, writeAddress = true) {
    if (writeAddress) {
      const url = new URL(win.location.href);
      if (id) url.searchParams.set('run', id);
      else url.searchParams.delete('run');
      win.history.replaceState(null, '', url);
    }
    if (selected !== id) {
      ++detailSequence;
      selected = id;
      get('detail-body').hidden = true;
      get('run-state').hidden = true;
      say('stop-note', '');
      get('detail-note').hidden = false;
      say('detail-note', id ? '실행 상세를 불러오는 중입니다.' : '목록에서 실행을 선택하세요.');
      renderRuns();
    }
  }

  function selectRun(id, writeAddress = true) {
    setSelection(id, writeAddress);
    return loadDetail();
  }

  function renderDetail(run) {
    get('detail-body').hidden = false;
    get('detail-note').hidden = true;
    get('run-state').hidden = false;
    badge(get('run-state'), run.state);
    say('request', run.task);
    say('detail-workspace', workspaceName(run.workspace_id));
    say('run-id', run.id);
    get('attempts').hidden = !Number.isInteger(run.attempts);
    say('attempts', Number.isInteger(run.attempts) ? '개발 시도 ' + run.attempts + '회' : '');
    get('retry-note').hidden = !(run.attempts > 1);
    get('outcome').hidden = !run.outcome;
    say('outcome', outcomes[run.outcome] ?? (run.outcome ? '종료 결과를 확인할 수 없습니다.' : ''));
    get('diagnostic').hidden = !run.diagnostic;
    say('diagnostic', run.diagnostic || '');
    const tested = Boolean(run.test_output) || testedOutcomes.has(run.outcome) || ['TESTING', 'REVIEWING'].includes(run.state);
    get('test-command').hidden = !run.allow_edits || !tested;
    get('run-context').hidden = !Array.isArray(run.folders);
    if (Array.isArray(run.folders)) {
      say('context-mode', run.allow_edits ? '개발·검증 맡기기' : '계획 작성');
      say('context-path', run.workspace ?? '—');
      say('context-primary', run.folders[0] ?? '—');
      say('context-extras', run.folders.slice(1).join('\n') || '없음');
      say('context-branch', run.branch ?? '—');
    }
    get('stop').hidden = !stoppable.has(run.state);
    get('stop').disabled = !online || stopping.has(run.id);
    for (const field of evidence) say(field, run[field] || '기록된 내용이 없습니다.');
  }

  async function loadDetail() {
    const sequence = ++detailSequence, id = selected;
    if (!id) return;
    try {
      const {run} = await api('/runs/' + id);
      if (run?.id !== id) throw new Error('잘못된 실행 응답');
      if (!closed && sequence === detailSequence) renderDetail(run);
    } catch (error) {
      if (closed || sequence !== detailSequence) return;
      if (error.status === 404 && error.code === 'NOT_FOUND') {
        get('detail-note').hidden = false;
        get('detail-body').hidden = true;
        get('run-state').hidden = true;
        say('detail-note', pending?.id === id ? '작업 접수 결과를 기다리고 있습니다.' : '이 주소의 실행을 찾을 수 없습니다.');
      } else disconnected(connectionErrors[error.code]);
    }
  }

  function disconnected(message = '호스트 연결을 확인하세요.') {
    ++detailSequence;
    online = false;
    controls();
    say('connection', message);
    get('connection').dataset.online = 'false';
    get('list-note').hidden = false;
    say('list-note', '실행 기록을 갱신하지 못했습니다. 새로고침하면 다시 연결합니다.');
    if (selected) {
      get('detail-note').hidden = false;
      say('detail-note', get('detail-body').hidden ? '연결 후 실행 상태를 확인할 수 있습니다.' :
        '마지막으로 확인한 상태와 결과입니다. 연결 후 최신 내용을 표시합니다.');
    }
  }

  async function refresh() {
    if (doc.hidden || closed) return;
    win.clearTimeout(timer);
    const sequence = ++listSequence;
    let catalog;
    try {
      const directory = await fetchJSON('/api/hosts');
      if (closed || sequence !== listSequence) return;
      renderHosts(directory.hosts);
      if (!host) {
        disconnected('등록되지 않은 호스트');
        say('list-note', '위 목록에서 실행 호스트를 선택하세요.');
        return;
      }
      catalog = loadCatalog();
      const {runs: list} = await api('/runs');
      if (closed || sequence !== listSequence) return;
      runs = list;
      renderRuns();
      online = true;
      controls();
      say('connection', host.name + ' 연결됨');
      get('connection').dataset.online = 'true';
      get('list-note').hidden = runs.length > 0;
      say('list-note', '아직 실행 기록이 없습니다. 새 작업을 시작해 보세요.');
      const recovered = await checkPending();
      if (closed || sequence !== listSequence) return;
      if (!selected && recovered) await selectRun(recovered.id);
      else if (!selected && runs.length) await selectRun(runs[0].id);
      else await loadDetail();
    } catch (error) {
      if (closed || sequence !== listSequence) return;
      disconnected(connectionErrors[error.code]);
    } finally {
      await catalog;
      if (!closed && sequence === listSequence && !doc.hidden) timer = win.setTimeout(refresh, 3000);
    }
  }

  get('workspace-form').addEventListener('submit', async event => {
    event.preventDefault();
    if (get('register').disabled) return;
    const name = get('name').value.trim();
    const paths = get('paths').value.split(/\r?\n/).map(path => path.trim()).filter(Boolean);
    if (!name || !paths.length) return say('register-note', '이름과 폴더 경로를 입력하세요.');
    registering = true;
    controls();
    try {
      const {workspace: added} = await api('/workspaces', {name, paths});
      ++catalogSequence;
      workspaces = [...workspaces.filter(item => item.id !== added.id), added];
      renderCatalog(pending?.workspace_id ?? added.id);
      if (pending) renderFolders(pending.indices);
      get('workspace-form').reset();
      get('registration').open = false;
      get('workspace').focus();
      say('start-note', added.name + ' 작업공간을 등록했습니다.');
      say('register-note', '');
    } catch (error) {
      say('register-note', error.status ? error.message : '등록 여부를 확인하려면 서버 연결 후 새로고침하세요.');
    } finally { registering = false; controls(); }
  });

  async function lookupRequest(id) {
    try {
      const {run} = await api('/runs/' + id);
      if (run?.id !== id) throw new Error('잘못된 실행 응답');
      return run;
    } catch (error) {
      if (error.status === 404 && error.code === 'NOT_FOUND') return null;
      throw error;
    }
  }

  function accepted(run) {
    keepPending(null);
    get('task').value = '';
    say('start-note', '접수했습니다. 실행 상세에서 진행 상황을 확인하세요.');
    runs = [run, ...runs.filter(item => item.id !== run.id)];
    renderRuns();
  }

  async function checkPending() {
    if (!pending || starting) return;
    starting = true;
    controls();
    try {
      const run = await lookupRequest(pending.id);
      if (closed) return;
      if (run) accepted(run);
      else say('start-note', '아직 접수 기록이 없습니다. 같은 요청을 다시 보낼 수 있습니다.');
      return run;
    } finally { starting = false; controls(); }
  }

  get('run-form').addEventListener('submit', async event => {
    event.preventDefault();
    if (get('start').disabled) return;
    let request = pending;
    const retrying = Boolean(pending);
    if (!request) {
      const task = get('task').value.trim();
      if (!workspace() || !task) return say('start-note', '작업공간을 선택하고 작업 요청을 입력하세요.');
      if (new TextEncoder().encode(task).length > 8192) {
        get('task').focus();
        return say('start-note', '작업 요청은 UTF-8 기준 8,192바이트 이하로 입력하세요.');
      }
      request = {
        id: win.crypto.randomUUID().replaceAll('-', ''),
        workspace_id: workspace().id,
        indices: [Number(get('primary').value), ...checkedExtras()],
        task, allow_edits: get('edit-mode').checked,
      };
      keepPending(request);
    }
    const previous = selected;
    setSelection(request.id);
    starting = true;
    controls();
    say('start-note', '작업 접수를 확인하고 있습니다.');
    let submitting = false;
    try {
      let run = retrying ? await lookupRequest(request.id) : null;
      if (!run) {
        submitting = true;
        ({run} = await api('/runs', request));
      }
      if (closed) return;
      if (run?.id !== request.id) throw new Error('잘못된 접수 응답');
      accepted(run);
      if (selected === run.id) await loadDetail();
    } catch (error) {
      const rejected = (error.status === 400 && error.code === 'INVALID_ARGUMENT') ||
        (error.status === 409 && ['REQUEST_CONFLICT', 'RUN_BUSY'].includes(error.code)) ||
        (!retrying && error.status === 404 && error.code === 'NOT_FOUND');
      if (submitting && rejected) {
        keepPending(null);
        if (selected === request.id) setSelection(previous === request.id ? null : previous);
        say('start-note', error.message);
      } else say('start-note', '접수 결과를 확인하지 못했습니다. 같은 요청으로 다시 확인하세요.');
    } finally { starting = false; controls(); refresh(); }
  });

  get('stop').addEventListener('click', async () => {
    const id = selected;
    if (!id || get('stop').disabled || get('stop').hidden) return;
    stopping.add(id);
    get('stop').disabled = true;
    say('stop-note', '중지 요청을 보내는 중입니다.');
    try {
      await api('/runs/' + id + '/stop', {});
      if (selected === id) say('stop-note', '중지 요청을 전달했습니다.');
    } catch (error) {
      if (selected === id) say('stop-note', error.status ? error.message : '중지 요청 결과를 확인하지 못했습니다.');
    } finally {
      stopping.delete(id);
      if (selected === id) refresh();
    }
  });

  get('workspace').addEventListener('change', () => renderFolders());
  get('primary').addEventListener('change', () => {
    renderExtras(checkedExtras());
  });
  get('refresh').addEventListener('click', refresh);
  doc.addEventListener('visibilitychange', () => {
    if (doc.hidden) win.clearTimeout(timer);
    else refresh();
  });
  win.addEventListener('popstate', () => {
    if (hostId !== addressHost()) {
      closed = true;
      win.location.reload();
      return;
    }
    const id = fromAddress();
    selectRun(validId(id) ? id : null, false);
  });
  if (pending) say('start-note', '이전 작업의 접수 결과를 같은 요청으로 확인할 수 있습니다.');
  controls();
  refresh();
  return () => { closed = true; win.clearTimeout(timer); };
}

if (typeof window !== 'undefined') mountStudio(window);
