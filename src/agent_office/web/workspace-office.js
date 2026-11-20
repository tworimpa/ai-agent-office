const roleLabels = {developer: '담당자', reviewer: '검토자'};
const engines = {codex: 'Codex', antigravity: 'Antigravity', claude: 'Claude Code', gemini: 'Gemini CLI', cline: 'Cline CLI'};
const states = {
  QUEUED: '대기 중', RUNNING: '작업 중', TESTING: '테스트 중', REVIEWING: '검토 중',
  STOP_REQUESTED: '중지 요청 중', CANCELED: '중지됨', PLAN_ONLY: '조회 완료',
  READY_FOR_HUMAN_REVIEW: '검토 결과 확인', WAITING_USER: '응답 필요',
  FAILED: '실패', RECOVERY_REQUIRED: '복구 필요',
};
const activeStates = new Set(['QUEUED', 'RUNNING', 'TESTING', 'REVIEWING', 'STOP_REQUESTED']);
const stoppable = state => activeStates.has(state) && state !== 'STOP_REQUESTED';
const followupStates = {developer: 'RUNNING', reviewer: 'REVIEWING'};
const requestLabels = {question: '기술 질문', product_decision: '제품 동작 결정', permission: '권한 승인 요청'};
const requestStateLabels = {decided: '결정 접수됨', delivered: '에이전트에게 전달됨', expired: '만료됨'};
const deliveryLabels = {queued: '전달 대기', sending: '전달 중', delivered: '전달됨', uncertain: '전달 여부 확인 필요', undelivered: '전달되지 않음'};
const decisionLabels = {answer: '답변', approve: '이번 요청 승인', reject: '거절'};
const validId = value => typeof value === 'string' && /^[a-f0-9]{32}$/.test(value);
const validHistoryCursor = value => typeof value === 'string' && /^[ah]-[a-f0-9]{32}$/.test(value);
function uniqueRuns(runs) {
  const ids = new Set();
  return runs.filter(run => {
    if (ids.has(run.id)) return false;
    ids.add(run.id);
    return true;
  });
}
const stateName = state => states[state] ?? '상태 미확인';
const engineName = engine => engines[engine] ?? '엔진 미확인';
const codeBytes = value => new TextEncoder().encode(value).length;
const stamp = ts => {
  if (!ts || Number.isNaN(Date.parse(ts))) return '';
  return new Intl.DateTimeFormat('ko-KR', {month: 'numeric', day: 'numeric', hour: 'numeric', minute: '2-digit'}).format(new Date(ts));
};
const scope = host => host === 'local' ? '/api' : '/api/hosts/' + host;

function address(host, workspace, run, base = '/') {
  const query = new URLSearchParams();
  if (host !== 'local') query.set('host', host);
  if (workspace) query.set('workspace', workspace);
  if (run) query.set('run', run);
  return base + (query.size ? '?' + query : '');
}

export function mountWorkspaceOffice(win = window) {
  const doc = win.document;
  const get = id => doc.getElementById(id);
  const say = (id, value) => {
    const node = get(id);
    if (node.textContent !== value) node.textContent = value;
  };
  const make = (tag, value, cls) => {
    const node = doc.createElement(tag);
    if (value) node.textContent = value;
    if (cls) node.className = cls;
    return node;
  };
  const option = (value, name) => {
    const node = make('option', name);
    node.value = value;
    return node;
  };
  const snapshots = new Map();
  function update(id, snapshot, fn) {
    const serialized = JSON.stringify(snapshot);
    if (snapshots.get(id) !== serialized) {
      snapshots.set(id, serialized);
      fn(get(id));
    }
  }
  function readAddress() {
    const query = new URLSearchParams(win.location.search);
    return {
      host: query.get('host') || 'local',
      workspace: validId(query.get('workspace')) ? query.get('workspace') : null,
      run: validId(query.get('run')) ? query.get('run') : null,
    };
  }

  let {host: hostId, workspace: workspaceId, run: runId} = readAddress();
  let directory = [], workspaces = [], room = null, detail = null;
  let olderRuns = [], historyCursor = null, historyPaged = false, historyBusy = false, historyEpoch = 0;
  let online = false, roomOnline = false, closed = false, epoch = 0, refreshToken = 0, timer;
  let chatRole = null, chatAttempt = null, chatRows = [], directionRows = [], chatDetailsOnline = false, inboxDetailsOnline = false;
  let chatState = null, settingsBusy = false, registrationBusy = false, deciding = new Set();
  let inboxFocus;
  const stopping = new Set();
  let dialogEpoch = 0, inboxEpoch = 0, detailRead = 0, chatRead = 0, inboxRead = 0, skills = [], skillsCursor = null;
  const recovery = new Map();
  const editors = new Map();
  const roomPath = () => '/rooms/' + workspaceId;
  const runPath = id => '/runs/' + id;
  const roomRuns = () => room ? [...room.runs, ...olderRuns] : [];
  const summary = () => roomRuns().find(run => run.id === runId);
  const selected = () => detail ? {...summary(), ...detail} : summary();
  const trustedRun = id => Boolean(id && (roomRuns().some(run => run.id === id) ||
    (detail?.id === id && detail.workspace_id === workspaceId)));
  const sameContext = token => !closed && token === epoch;
  const canWrite = () => !closed && roomOnline && !!room;

  async function request(path, body) {
    const controller = new win.AbortController();
    const timeout = win.setTimeout(() => controller.abort(), 15000);
    try {
      const response = await win.fetch(path, {
        mode: 'same-origin', redirect: 'error', cache: 'no-store', signal: controller.signal,
        ...(body === undefined ? {} : {
          method: 'POST', headers: {'Content-Type': 'application/json', 'X-Agent-Office': 'studio'},
          body: JSON.stringify(body),
        }),
      });
      const data = await response.json();
      if (!response.ok) {
        const error = new Error(response.status < 500 && data.error?.message || '요청을 처리하지 못했습니다.');
        error.status = response.status;
        error.code = data.error?.code;
        throw error;
      }
      return data;
    } finally {
      win.clearTimeout(timeout);
    }
  }

  const api = (path, body) => request(scope(hostId) + path, body);

  function sessionKey(role) {
    const target = role === 'coordinator' ? 'new' : runId + ':' + chatAttempt;
    return ['workspace-office', hostId, workspaceId, role, target].join(':');
  }

  function readRecovery(key) {
    try {
      const stored = win.sessionStorage.getItem(key);
      return stored ? JSON.parse(stored) : recovery.get(key);
    } catch { return recovery.get(key); }
  }

  function saveRecovery(key, entry) {
    if (entry) recovery.set(key, entry);
    else recovery.delete(key);
    try {
      if (entry) win.sessionStorage.setItem(key, JSON.stringify(entry));
      else win.sessionStorage.removeItem(key);
    } catch { /* The current tab still retains the request ID in memory. */ }
  }

  function navigation(host, workspace, run, replace = false) {
    const changingRoom = host !== hostId || workspace !== workspaceId;
    if (host !== hostId) {
      online = false;
      workspaces = [];
    }
    if (changingRoom) {
      room = null;
      roomOnline = false;
      olderRuns = [];
      historyCursor = null;
      historyPaged = false;
      historyBusy = false;
      ++historyEpoch;
      say('history-note', '');
    }
    hostId = host;
    workspaceId = workspace;
    runId = run;
    detail = null;
    say('detail-status', '');
    say('stop-note', '');
    closeDialogs();
    ++epoch;
    win.history[replace ? 'replaceState' : 'pushState'](null, '', address(hostId, workspaceId, runId));
    render();
    return refresh();
  }

  function navClick(event, host, workspace, run) {
    if (event.ctrlKey || event.metaKey || event.shiftKey || event.altKey || event.button !== 0) return;
    event.preventDefault();
    if (host === hostId && workspace === workspaceId && run === runId) return;
    navigation(host, workspace, run);
  }

  function runLink(run) {
    const link = make('a', '', 'nav-link');
    link.href = address(hostId, workspaceId, run.id);
    if (run.id === runId) link.setAttribute('aria-current', 'page');
    const meta = [stateName(run.state), stamp(run.created_at), run.pending_count && '응답 ' + run.pending_count + '건'].filter(Boolean);
    link.append(make('span', run.task, 'run-task'), make('small', meta.join(' · ')));
    link.title = run.task;
    link.addEventListener('click', event => navClick(event, hostId, workspaceId, run.id));
    return link;
  }

  function renderNavigation() {
    const host = get('host');
    update('host', directory, node => node.replaceChildren(...directory.map(item => option(item.id, item.name))));
    if (directory.some(item => item.id === hostId)) host.value = hostId;
    else host.selectedIndex = -1;
    host.disabled = !directory.length;
    get('register').disabled = !online || registrationBusy;
    update('workspaces', [hostId, workspaceId, workspaces], node => {
      node.replaceChildren(...workspaces.map(workspace => {
        const link = make('a', workspace.name, 'nav-link');
        link.href = address(hostId, workspace.id);
        if (workspace.id === workspaceId) link.setAttribute('aria-current', 'page');
        link.addEventListener('click', event => navClick(event, hostId, workspace.id, null));
        return link;
      }));
      if (online && !workspaces.length) node.append(make('p', '등록된 작업공간이 없습니다.', 'list-empty'));
    });
    get('history').hidden = !room;
    get('history-older').hidden = !historyCursor;
    get('history-older').disabled = historyBusy || !roomOnline;
    const runs = roomRuns();
    update('run-list', [workspaceId, runId, runs.map(({id, task, state, created_at, pending_count}) => ({
      id, task, state, created_at, pending_count,
    }))], node => {
      const focused = node.contains(doc.activeElement) && doc.activeElement.href;
      node.replaceChildren(...runs.map(runLink));
      if (!runs.length) node.append(make('p', '아직 작업 기록이 없습니다.', 'list-empty'));
      if (focused) [...node.children].find(link => link.href === focused)?.focus({preventScroll: true});
    });
    get('classic').href = address(hostId, null, null, '/studio');
    get('versions-link').href = address(hostId, null, null, '/versions');
  }

  async function loadOlderHistory() {
    if (!canWrite() || !historyCursor || historyBusy) return;
    const token = historyEpoch;
    const current = () => !closed && token === historyEpoch;
    historyBusy = true;
    renderNavigation();
    say('history-note', '이전 작업을 불러오는 중입니다.');
    try {
      const query = historyCursor;
      const page = await api(roomPath() + '/history/' + query);
      if (!current()) return;
      if (!Array.isArray(page.runs) || (page.next_cursor !== null && (!validHistoryCursor(page.next_cursor) || page.next_cursor === query))) {
        throw new Error('작업 기록 형식 오류');
      }
      const freshIds = new Set(room.runs.map(run => run.id));
      olderRuns = uniqueRuns([...olderRuns, ...page.runs])
        .filter(run => validId(run.id) && run.workspace_id === workspaceId && !freshIds.has(run.id));
      historyCursor = page.next_cursor;
      historyPaged = true;
      say('history-note', '');
    } catch {
      if (current()) say('history-note', '이전 작업을 불러오지 못했습니다. 다시 시도할 수 있습니다.');
    } finally {
      if (current()) {
        historyBusy = false;
        renderNavigation();
      }
    }
  }

  const seat = role => doc.querySelector('.seat[data-person="' + role + '"]');
  const roomPerson = role => room.settings[role];
  const activeRuns = () => room.runs.filter(run => activeStates.has(run.state));

  function visibleRun(role) {
    const run = selected();
    const atDesk = row => row.state === followupStates[role] || role === 'developer' && row.state === 'TESTING';
    if (run && atDesk(run)) return run;
    return activeRuns().find(atDesk) ?? (run && activeStates.has(run.state) ? run : activeRuns()[0] ?? run);
  }

  function renderRoom() {
    get('room').hidden = !room;
    get('empty-room').hidden = !!room;
    get('empty-register').hidden = !online || workspaces.length > 0;
    get('empty-register').disabled = registrationBusy;
    if (!room) {
      say('empty-title', workspaces.length ? '작업실 선택' : '작업실');
      say('empty-note', workspaceId && !roomOnline ? '작업실 정보를 불러오는 중이거나 호스트와 연결되지 않았습니다.' :
        workspaces.length ? '목록에서 들어갈 작업공간을 선택하세요.' :
        online ? '폴더를 등록해 작업실을 열 수 있습니다.' : '작업공간을 불러오는 중입니다.');
      return;
    }
    say('room-title', room.workspace.name);
    const count = room.runs.reduce((total, run) => total + (run.pending_count ?? 0), 0);
    get('inbox-count').hidden = count < 1;
    say('inbox-count', String(count));
    get('settings').disabled = !roomOnline;
    get('inbox').disabled = !roomOnline && !get('requests').open;
    for (const [role, label] of Object.entries(roleLabels)) {
      const current = visibleRun(role);
      const profile = current?.assignment?.roles?.[role] ?? roomPerson(role);
      const engine = current?.[role + '_engine'] ?? profile.engine;
      const node = seat(role);
      let line = current?.lines?.[role] ?? room.runs.find(run => run.id === current?.id)?.lines?.[role];
      if (!line) line = current && (role === 'developer' || current.allow_edits) ? stateName(current.state) : '배정된 작업이 없습니다.';
      const roleText = label + ' · ' + engineName(engine);
      const task = node.querySelector('.seat-task');
      const assignment = role === 'developer' || current?.allow_edits ? current?.task : '';
      node.querySelector('.seat-name').textContent = profile.name;
      node.querySelector('.seat-role').textContent = roleText;
      node.querySelector('.seat-line').textContent = line;
      task.textContent = assignment ?? '';
      task.hidden = !assignment;
      node.title = [assignment, line].filter(Boolean).join(' · ');
      node.dataset.request = String((current?.pending_count ?? room.runs.find(run => run.id === current?.id)?.pending_count ?? 0) > 0);
      node.setAttribute('aria-label', [profile.name, roleText, assignment, line, '대화 열기'].filter(Boolean).join(', '));
    }
    seat('coordinator').querySelector('.seat-line').textContent = room.settings.delegation ? '새 작업 배정 · 기술 위임 켜짐' : '새 작업을 말씀해 주세요.';
    renderLedger();
  }

  function renderLedger() {
    const run = selected();
    get('detail-link').hidden = !run;
    get('detail-link').href = address(hostId, null, run?.id, '/studio') + '#detail';
    get('stop-run').hidden = !stoppable(run?.state);
    get('stop-run').disabled = !canWrite() || stopping.has(run?.id);
    say('ledger-label', run ? stateName(run.state) : '작업 기록');
    say('ledger-heading', run?.task ?? '선택한 작업이 없습니다');
    const date = stamp(run?.created_at);
    say('ledger-note', run ? [date, run.assignment?.delegation === true && '기술 질문 위임', run.pending_count && '응답 대기 ' + run.pending_count + '건'].filter(Boolean).join(' · ') :
      '조정자에게 요청을 보내 작업을 시작할 수 있습니다.');
    const testFiles = detail?.assignment?.test_files;
    const evidence = detail ? [
      ['개발 결과', detail.developer_output], ['테스트 파일', Array.isArray(testFiles) && testFiles.join('\n')],
      ['테스트 결과', detail.test_output], ['검토 결과', detail.review_output],
      ['변경 파일', detail.changes], ['변경 내용', detail.diff], ['진단', detail.diagnostic],
    ].filter(([, value]) => value) : [];
    get('ledger-result').hidden = !evidence.length;
    update('ledger-result', evidence, node => {
      node.replaceChildren(...evidence.map(([name, value]) => {
        const summary = make('details', '', 'evidence');
        summary.append(make('summary', name), make('p', value));
        return summary;
      }));
      if (node.firstElementChild) node.firstElementChild.open = true;
    });
  }

  function renderConnection() {
    const status = get('connection');
    status.dataset.online = String(workspaceId ? roomOnline : online);
    const label = directory.find(item => item.id === hostId)?.name;
    say('connection', status.dataset.online === 'true' ? label + ' 연결됨' :
      room ? '연결 끊김 · 마지막 기록' : directory.length ? '연결 확인 필요' : '연결 중');
  }

  function render() {
    renderConnection();
    renderNavigation();
    renderRoom();
    chatControls();
  }

  async function fetchRun(token, expected) {
    if (!expected) return;
    const read = ++detailRead;
    try {
      const {run} = await api(runPath(expected));
      if (!sameContext(token) || expected !== runId || read !== detailRead) return;
      if (run?.id !== expected) throw new Error('실행 정보가 달라졌습니다.');
      if (run.workspace_id !== workspaceId) {
        detail = null;
        say('detail-status', '이 실행은 선택한 작업공간에 속하지 않습니다.');
        renderRoom();
        return;
      }
      detail = run;
      say('detail-status', '');
      renderRoom();
      if (get('conversation').open) {
        renderChatLog();
        chatControls();
      }
    } catch {
      if (sameContext(token) && expected === runId && read === detailRead) say('detail-status', '실행 상세의 최신 상태를 확인할 수 없습니다.');
    }
  }

  async function refresh() {
    win.clearTimeout(timer);
    const token = epoch;
    const sequence = ++refreshToken;
    const current = () => sameContext(token) && sequence === refreshToken;
    try {
      if (!directory.length) {
        const {hosts} = await request('/api/hosts');
        if (!current()) return;
        directory = hosts;
      }
      if (!directory.some(host => host.id === hostId)) {
        say('empty-note', '이 주소의 실행 호스트를 찾을 수 없습니다.');
        renderNavigation();
        return;
      }
      if (!workspaceId || !workspaces.some(item => item.id === workspaceId)) {
        const {workspaces: list} = await api('/workspaces');
        if (!current()) return;
        workspaces = list;
      }
      if (!workspaceId && runId) {
        try {
          const {run} = await api(runPath(runId));
          if (!current()) return;
          if (run?.id === runId && validId(run.workspace_id)) {
            workspaceId = run.workspace_id;
            detail = run;
          }
        } catch (error) {
          if (!current()) return;
          if (error.status !== 404) throw error;
          runId = null;
        }
      }
      online = true;
      if (!workspaceId) workspaceId = workspaces[0]?.id ?? null;
      if (workspaceId) {
        const snapshot = await api(roomPath());
        if (!current()) return;
        if (snapshot.workspace?.id !== workspaceId || !snapshot.settings || !Array.isArray(snapshot.runs)) throw new Error('작업실 정보 오류');
        const runs = snapshot.runs.filter(run => validId(run.id) && run.workspace_id === workspaceId);
        if (historyPaged || historyBusy) {
          const freshIds = new Set(runs.map(run => run.id));
          olderRuns = uniqueRuns([...(room?.runs ?? []), ...olderRuns]).filter(run => !freshIds.has(run.id));
        }
        if (!historyPaged && !historyBusy) historyCursor = validHistoryCursor(snapshot.next_cursor) ? snapshot.next_cursor : null;
        room = {...snapshot, runs};
        roomOnline = true;
        if (!runId) runId = (activeRuns()[0] ?? room.runs[0])?.id ?? null;
        if (room.runs.some(run => run.id === runId && (run.state !== detail?.state || run.attempts !== detail?.attempts))) detail = null;
      }
      win.history.replaceState(null, '', address(hostId, workspaceId, runId) + win.location.hash);
      render();
      const detailRequest = fetchRun(token, runId);
      if (runId && !trustedRun(runId)) await detailRequest;
      if (!current()) return;
      await Promise.all([
        detailRequest,
        get('conversation').open ? loadChat() : null,
        get('requests').open ? loadRequests() : null,
      ]);
    } catch {
      if (!current()) return;
      online = false;
      roomOnline = false;
      chatDetailsOnline = false;
      inboxDetailsOnline = false;
      render();
      if (get('requests').open) {
        requestControls();
        say('request-note', '연결이 끊겼습니다. 마지막으로 확인한 요청을 표시합니다.');
      }
    } finally {
      if (current()) timer = win.setTimeout(refresh, 3000);
    }
  }

  function openDialog(dialog, focus) {
    if (!dialog.open) dialog.showModal();
    focus?.focus();
  }

  function closeDialogs() {
    for (const id of ['conversation', 'requests', 'room-settings', 'register-dialog']) {
      if (get(id).open) get(id).close();
    }
    ++dialogEpoch;
    ++inboxEpoch;
  }

  function folderChoices(indices) {
    const root = get('assignment-folders');
    root.hidden = room.workspace.folders.length < 2;
    root.replaceChildren(make('legend', '이번 작업의 폴더'));
    room.workspace.folders.forEach((folder, index) => {
      const label = make('label', '', 'checkbox');
      const box = make('input');
      box.type = 'checkbox';
      box.name = 'folder';
      box.value = String(index);
      box.checked = indices?.includes(index) ?? false;
      label.append(box, make('span', typeof folder === 'string' ? folder : folder.path));
      root.append(label);
    });
    root.append(make('p', '선택하지 않으면 서버가 작업실의 기본 폴더를 사용합니다.', 'field-help'));
  }

  function showChat(role) {
    if (!room) return;
    if (role !== 'coordinator') {
      const run = visibleRun(role);
      if (run?.id !== runId && run?.id) {
        closeDialogs();
        ++epoch;
        runId = run.id;
        detail = null;
        say('detail-status', '');
        say('stop-note', '');
        win.history.pushState(null, '', address(hostId, workspaceId, runId));
        refresh();
      }
    }
    const token = ++dialogEpoch;
    chatRole = role;
    const current = selected();
    chatAttempt = role === 'coordinator' ? null : current?.attempts;
    chatState = null;
    chatRows = [];
    directionRows = [];
    chatDetailsOnline = false;
    const key = sessionKey(role);
    const saved = readRecovery(key)?.body;
    const editor = editors.get(key);
    const assignment = role === 'coordinator';
    const name = assignment ? '조정자' : (current?.assignment?.roles?.[role] ?? roomPerson(role)).name;
    say('chat-title', name + '와 대화');
    get('chat-portrait').dataset.person = role;
    say('chat-context', assignment ? room.workspace.name : current?.task ?? '배정된 작업이 없습니다.');
    say('chat-label', assignment ? '요청할 작업' : '이 작업에 추가할 지시');
    get('message').placeholder = assignment ? '예시 · 이 프로젝트의 테스트 실패 원인을 찾아줘.' : '예시 · Windows에서만 재현되는지 확인해줘.';
    get('assignment-options').hidden = !assignment;
    get('edit-option').hidden = !assignment;
    get('assignment-options').open = Boolean(saved?.delegation != null || saved?.indices || saved?.test_files);
    get('run-delegation').options[0].textContent = '작업실 기본값 · ' + (room.settings.delegation ? '조정자가 결정' : '나에게 질문');
    get('run-delegation').value = saved?.delegation == null ? '' : String(saved.delegation);
    get('allow-edits').checked = saved?.allow_edits ?? false;
    get('test-files').value = saved?.test_files?.join('\n') ?? '';
    if (assignment) folderChoices(saved?.indices);
    get('message').value = saved?.message ?? editor ?? '';
    say('chat-note', saved ? '이전 접수 결과를 확인하지 못했습니다. 같은 요청을 다시 보낼 수 있습니다.' : '');
    say('chat-request-note', '');
    renderChatLog();
    chatControls();
    openDialog(get('conversation'), get('message').disabled ? get('chat-log') : get('message'));
    loadChat(token);
  }

  function message(owner, name, value, meta) {
    const node = make('article', '', 'message');
    node.dataset.owner = owner;
    node.append(make('small', name));
    if (value) node.append(make('p', value));
    if (meta) node.append(make('small', meta));
    return node;
  }

  function decisionStatus(row) {
    return [decisionLabels[row.decision], requestStateLabels[row.status], stamp(row.delivered_at ?? row.expired_at ?? row.decided_at)]
      .filter(Boolean).join(' · ');
  }

  function decisionEvidence(row) {
    const delegated = row.delegation_details;
    if (!delegated?.evidence) return null;
    const info = make('details');
    info.dataset.evidence = row.run_id + ':' + row.id;
    const source = delegated.evidence;
    info.append(make('summary', '대리 결정 근거'),
      make('p', '전문가 · ' + engineName(delegated.specialist_engine) + ' / 독립 검증자 · ' + engineName(delegated.verifier_engine)),
      make('p', source.path + ' ' + source.line + '행 · 기준 커밋 ' + source.revision), make('p', source.quote));
    return info;
  }

  function renderChatLog() {
    const run = selected();
    const row = room?.runs.find(item => item.id === runId);
    const role = chatRole;
    const assignment = run?.assignment ?? row?.assignment;
    const profile = role !== 'coordinator' && (assignment?.roles?.[role] ?? room?.settings[role]);
    const name = profile?.name ?? roleLabels[role];
    const line = row?.lines?.[role];
    const output = role === 'developer' ? detail?.developer_output : detail?.review_output;
    const relevant = chatRows.filter(item => item.role === role || role === 'coordinator');
    const directions = directionRows.filter(item => item.role === role || role === 'coordinator');
    update('chat-log', [runId, role, run?.task, assignment, name, line, output, relevant, directions], node => {
      const messages = [];
      if (run?.task) {
        messages.push(message('user', '나', run.task, stamp(run.created_at)));
        const roles = assignment?.roles;
        if (role === 'coordinator' && roles) {
          const applied = ['담당 · ' + roles.developer.name + ' (' + engineName(roles.developer.engine) + ')'];
          if (run.allow_edits) applied.push('검토 · ' + roles.reviewer.name + ' (' + engineName(roles.reviewer.engine) + ')');
          applied.push(assignment.delegation ? '기술 질문 위임' : '사용자에게 기술 질문');
          messages.push(message('agent', '조정자', assignment.note, applied.join(' / ')));
        }
      }
      if (role === 'coordinator') {
        if (!run) messages.push(message('agent', '조정자', '어떤 작업을 맡길까요?'));
      } else if (line || output) {
        messages.push(message('agent', name, output || line));
      }
      const events = [];
      for (const item of relevant) {
        const requestLabel = requestLabels[item.kind];
        if (!requestLabel) continue;
        if (item.status === 'pending') {
          events.push([item.created_at, requestItem(item, epoch, 'chat')]);
          continue;
        }
        events.push([item.created_at, message('agent', requestLabel + ' · ' + (roleLabels[item.role] ?? '에이전트'), item.prompt, stamp(item.created_at))]);
        if (item.decision) {
          const owner = item.source === 'delegate' ? '조정자 대리 결정' : '나';
          const receipt = message(item.source === 'delegate' ? 'agent' : 'user', owner, item.answer, decisionStatus(item));
          const evidence = decisionEvidence(item);
          if (evidence) receipt.append(evidence);
          events.push([item.decided_at, receipt]);
        } else if (item.status === 'expired') {
          events.push([item.expired_at, message('agent', requestLabel, '', decisionStatus(item))]);
        }
      }
      for (const item of directions) {
        events.push([item.created_at, message('user', '나 → ' + (roleLabels[item.role] ?? '에이전트'), item.message,
          [deliveryLabels[item.delivery] ?? '전달 상태 확인 필요', item.attempt && item.attempt + '번째 시도', stamp(item.created_at)].filter(Boolean).join(' · '))]);
      }
      events.sort((a, b) => String(a[0] ?? '').localeCompare(String(b[0] ?? '')));
      messages.push(...events.map(([, event]) => event));
      if (!messages.length) messages.push(make('p', '이 에이전트의 대화 기록이 없습니다.', 'field-help'));
      const pinned = node.scrollHeight - node.scrollTop - node.clientHeight < 25;
      const keptFocus = replaceRequests(node, messages);
      if (pinned && !keptFocus) node.scrollTop = node.scrollHeight;
    });
  }

  function chatControls() {
    if (!chatRole) return;
    const role = chatRole;
    const assignment = role === 'coordinator';
    const recoverable = Boolean(readRecovery(sessionKey(role)));
    const run = selected();
    const interactive = run && run.assignment && run.use_inbox === true;
    const staleAttempt = interactive && run.attempts !== chatAttempt;
    const open = assignment || interactive && !staleAttempt && chatAttempt > 0 && (run.state === followupStates[role] || recoverable);
    const locked = !!chatState || recoverable;
    const enabled = canWrite() && (assignment || chatDetailsOnline);
    get('message').disabled = !open || !enabled;
    get('message').readOnly = locked;
    get('run-delegation').disabled = locked;
    get('allow-edits').disabled = locked;
    const editAssignment = assignment && get('allow-edits').checked;
    get('assignment-test-scope').hidden = !editAssignment;
    get('test-files').disabled = locked || !editAssignment;
    for (const box of get('assignment-folders').querySelectorAll('input')) box.disabled = locked;
    get('send').hidden = !open;
    get('send').disabled = !enabled || !!chatState;
    say('send', recoverable && !chatState ? '같은 요청 다시 보내기' : assignment ? '작업 맡기기' : '추가 지시 보내기');
    if (!open && !chatState) {
      let reason = '진행 중인 작업에서 추가 지시를 보낼 수 있습니다.';
      if (staleAttempt) reason = '실행 시도가 바뀌었습니다. 현재 시도에 추가 지시를 보내려면 대화를 다시 여세요.';
      else if (run && !interactive) reason = '추가 지시는 작업실에서 새로 맡긴 작업에 보낼 수 있습니다.';
      else if (run && activeStates.has(run.state)) reason = '이 에이전트의 실행 단계가 시작되면 추가 지시를 보낼 수 있습니다.';
      say('chat-note', reason);
    }
    requestControls();
  }

  async function loadChat(token = dialogEpoch) {
    const id = runId;
    const read = ++chatRead;
    if (!trustedRun(id) || selected()?.kind === 'parallel' || !get('conversation').open) {
      chatDetailsOnline = false;
      chatControls();
      return;
    }
    const prefix = runPath(id);
    try {
      const [{requests}, {directions}] = await Promise.all([api(prefix + '/inbox'), api(prefix + '/directions')]);
      if (closed || dialogEpoch !== token || id !== runId || read !== chatRead || !get('conversation').open) return;
      chatRows = requests.filter(row => row.run_id === id);
      directionRows = directions;
      chatDetailsOnline = true;
      const key = sessionKey(chatRole);
      const saved = readRecovery(key)?.body;
      if (saved && chatRole !== 'coordinator' && directions.some(item =>
        item.id === saved.id && item.attempt === saved.attempt && item.role === saved.role)) {
        saveRecovery(key, null);
        editors.delete(key);
        get('message').value = '';
        say('chat-note', '추가 지시의 접수를 확인했습니다.');
      }
      renderChatLog();
    } catch {
      if (closed || dialogEpoch !== token || id !== runId || read !== chatRead || !get('conversation').open) return;
      chatDetailsOnline = false;
      say('chat-note', '이 작업의 최신 대화 내역을 확인할 수 없습니다.');
    }
    chatControls();
  }

  function chatBody(role) {
    const value = get('message').value.trim();
    const limit = role === 'coordinator' ? 8192 : 4096;
    if (!value || codeBytes(value) > limit) throw new Error('내용을 UTF-8 기준 1~' + limit.toLocaleString('ko-KR') + '바이트로 입력하세요.');
    const id = win.crypto.randomUUID().replaceAll('-', '');
    if (role !== 'coordinator') return {id, role, attempt: chatAttempt, message: value};
    const mode = get('run-delegation').value;
    const body = {id, message: value, allow_edits: get('allow-edits').checked, delegation: mode === '' ? null : mode === 'true'};
    const indices = Array.from(get('assignment-folders').querySelectorAll('input:checked'), input => Number(input.value));
    if (indices.length) body.indices = indices;
    if (body.allow_edits) {
      const files = get('test-files').value.split('\n').map(path => path.trim()).filter(Boolean);
      if (files.length) body.test_files = files;
    }
    return body;
  }

  get('allow-edits').addEventListener('change', chatControls);
  get('chat-form').addEventListener('submit', async event => {
    event.preventDefault();
    const role = chatRole;
    if (!role || get('send').disabled || get('send').hidden) return;
    const context = epoch, token = dialogEpoch;
    const intendedWorkspace = workspaceId;
    const key = sessionKey(role);
    const assignment = role === 'coordinator';
    const pending = readRecovery(key);
    let body;
    try { body = pending?.body ?? chatBody(role); }
    catch (error) { say('chat-note', error.message); return; }
    saveRecovery(key, {body});
    chatState = body.id;
    chatControls();
    say('chat-note', '접수 중입니다.');
    try {
      const path = assignment ? roomPath() + '/assignments' : runPath(runId) + '/directions';
      const receipt = await api(path, body);
      const entry = assignment ? receipt.run : receipt.direction;
      if (entry?.id !== body.id || (assignment ? entry.workspace_id !== intendedWorkspace : entry.attempt !== body.attempt || entry.role !== role)) {
        throw new Error('접수 결과가 확인되지 않았습니다.');
      }
      saveRecovery(key, null);
      editors.delete(key);
      if (!sameContext(context) || token !== dialogEpoch) return;
      get('message').value = '';
      if (assignment) {
        // Keep the coordinator conversation open once the just-assigned execution is selected.
        runId = entry.id;
        detail = entry;
        say('stop-note', '');
        chatRows = [];
        directionRows = [];
        win.history.pushState(null, '', address(hostId, workspaceId, runId));
        say('chat-note', '작업을 배정했습니다.');
        renderChatLog();
      } else {
        directionRows = [...directionRows.filter(item => item.id !== entry.id), entry];
        say('chat-note', '추가 지시를 접수했습니다.');
        renderChatLog();
      }
      refresh();
    } catch (error) {
      const rejected = (error.status === 400 && error.code === 'INVALID_ARGUMENT') ||
        (error.status === 409 && ['REQUEST_CONFLICT', 'REQUEST_CLOSED', 'RUN_BUSY'].includes(error.code)) ||
        (!pending && error.status === 404 && error.code === 'NOT_FOUND');
      if (rejected) saveRecovery(key, null);
      if (!sameContext(context) || token !== dialogEpoch) return;
      say('chat-note', rejected ? error.message :
        '접수 여부를 확인하지 못했습니다. 연결 후 같은 요청을 다시 보내세요.');
    } finally {
      if (chatState === body.id) chatState = null;
      chatControls();
    }
  });
  get('message').addEventListener('input', () => {
    if (chatRole) editors.set(sessionKey(chatRole), get('message').value);
  });
  get('conversation').addEventListener('close', () => {
    if (get('conversation').open) return;
    ++dialogEpoch;
    chatRole = null;
    chatDetailsOnline = false;
  });
  for (const role of ['coordinator', ...Object.keys(roleLabels)]) seat(role).addEventListener('click', () => showChat(role));

  function requestControls() {
    for (const [id, available] of [['chat-log', chatDetailsOnline], ['request-log', inboxDetailsOnline]]) {
      for (const row of get(id).querySelectorAll('.request-item')) {
        for (const input of row.querySelectorAll('button, textarea')) input.disabled = !canWrite() || !available || deciding.has(row.dataset.request);
      }
    }
  }

  function replaceRequests(root, rows) {
    const previous = doc.activeElement;
    const requestKey = root.contains(previous) && previous.closest('[data-request]')?.dataset.request;
    const control = previous?.dataset.control;
    const selection = [previous?.selectionStart, previous?.selectionEnd];
    const expanded = new Set([...root.querySelectorAll('[data-evidence][open]')].map(item => item.dataset.evidence));
    root.replaceChildren(...rows);
    for (const item of root.querySelectorAll('[data-evidence]')) item.open = expanded.has(item.dataset.evidence);
    if (!requestKey) return false;
    const row = rows.find(item => item.dataset.request === requestKey);
    const next = row && [...row.querySelectorAll('[data-control]')].find(node => node.dataset.control === control);
    if (next && !next.disabled) {
      next.focus({preventScroll: true});
      if (typeof selection[0] === 'number') next.setSelectionRange(...selection);
      return true;
    }
    root.focus({preventScroll: true});
    return false;
  }

  function requestItem(row, context, view = 'inbox') {
    const key = row.run_id + ':' + row.id;
    const title = requestLabels[row.kind] ?? '요청 종류 확인 필요';
    const node = make('article', '', 'request-item');
    node.dataset.request = key;
    node.tabIndex = -1;
    const run = roomRuns().find(item => item.id === row.run_id);
    const meta = [roleLabels[row.role], run?.task, stamp(row.created_at)].filter(Boolean);
    node.append(make('h3', title), make('small', meta.join(' · ')), make('p', row.prompt));
    if (row.delegation_reason) node.append(make('small', row.delegation_reason));
    else if (row.delegation_in_progress) node.append(make('small', '전문가가 근거를 확인하고 있습니다.'));
    if (row.status !== 'pending') {
      const owner = row.source === 'delegate' ? '조정자 대리 결정' : '사용자 결정';
      node.append(make('small', [row.decision && owner, decisionStatus(row)].filter(Boolean).join(' · ')));
      if (row.answer) node.append(make('p', row.answer));
      const evidence = decisionEvidence(row);
      if (evidence) node.append(evidence);
      return node;
    }
    if (!requestLabels[row.kind]) return node;
    if (row.kind !== 'permission') {
      const textarea = make('textarea');
      textarea.id = view + '-answer-' + row.run_id + '-' + row.id;
      textarea.rows = 2;
      textarea.dataset.control = 'draft';
      textarea.value = editors.get('request:' + key) ?? '';
      textarea.addEventListener('input', () => editors.set('request:' + key, textarea.value));
      const label = make('label', '답변');
      label.htmlFor = textarea.id;
      const send = make('button', '답변 보내기', 'primary');
      send.type = 'button';
      send.dataset.control = 'answer';
      send.addEventListener('click', () => submitDecision(row, 'answer', textarea.value, context, view));
      node.append(label, textarea, send);
    } else if (view === 'chat') {
      const button = make('button', '요청함에서 확인', 'secondary');
      button.type = 'button';
      button.dataset.control = 'inbox';
      button.addEventListener('click', () => {
        get('conversation').close();
        showRequests(key);
      });
      node.append(button);
    } else {
      const actions = make('div', '', 'request-actions');
      for (const [action, label] of [['approve', '이번 요청만 승인'], ['reject', '거절']]) {
        const button = make('button', label, 'secondary');
        button.type = 'button';
        button.dataset.control = action;
        button.addEventListener('click', () => submitDecision(row, action, '', context, view));
        actions.append(button);
      }
      node.append(actions);
    }
    return node;
  }

  function showRequests(focusKey) {
    const token = ++inboxEpoch;
    inboxFocus = focusKey;
    inboxDetailsOnline = false;
    get('request-log').replaceChildren(make('p', '요청을 불러오는 중입니다.', 'field-help'));
    snapshots.delete('request-log');
    say('request-note', '');
    openDialog(get('requests'));
    loadRequests(token);
  }

  async function loadRequests(token = inboxEpoch) {
    if (!room || !get('requests').open) return;
    const context = epoch;
    const read = ++inboxRead;
    const id = trustedRun(runId) && selected()?.kind !== 'parallel' ? runId : null;
    try {
      const [pending, history] = await Promise.all([
        api(roomPath() + '/inbox'),
        id ? api(runPath(id) + '/inbox') : {requests: []},
      ]);
      if (!sameContext(context) || token !== inboxEpoch || read !== inboxRead || !get('requests').open) return;
      const rows = [...pending.requests.filter(row => trustedRun(row.run_id)),
        ...history.requests.filter(row => row.run_id === id && row.status !== 'pending')];
      inboxDetailsOnline = true;
      update('request-log', rows, node => {
        replaceRequests(node, rows.map(row => requestItem(row, context)));
        if (!rows.length) node.append(make('p', '응답할 요청이 없습니다.', 'field-help'));
      });
      if (inboxFocus) {
        [...get('request-log').children].find(item => item.dataset.request === inboxFocus)?.focus();
        inboxFocus = null;
      }
      requestControls();
    } catch {
      if (!sameContext(context) || token !== inboxEpoch || read !== inboxRead) return;
      inboxDetailsOnline = false;
      requestControls();
      say('request-note', '최신 요청을 확인할 수 없습니다.');
    }
  }

  async function submitDecision(row, action, answer, context, view) {
    const key = row.run_id + ':' + row.id;
    const chat = view === 'chat';
    const viewEpoch = () => chat ? dialogEpoch : inboxEpoch;
    const note = chat ? 'chat-request-note' : 'request-note';
    const token = viewEpoch();
    const active = () => sameContext(context) && token === viewEpoch() && get(chat ? 'conversation' : 'requests').open;
    if (!active() || !canWrite() || !trustedRun(row.run_id) || !(chat ? chatDetailsOnline : inboxDetailsOnline) || deciding.has(key)) return;
    const body = {action};
    if (action === 'answer') {
      body.answer = answer.trim();
      if (!body.answer || codeBytes(body.answer) > 8192) return say(note, '답변을 UTF-8 기준 1~8,192바이트로 입력하세요.');
    }
    deciding.add(key);
    requestControls();
    say(note, '요청 상태를 확인하고 있습니다.');
    try {
      const path = runPath(row.run_id) + '/inbox';
      const {requests} = await api(path);
      if (!active() || !canWrite() || !trustedRun(row.run_id)) return;
      const current = requests.find(item => item.id === row.id && item.run_id === row.run_id);
      if (!current || ['kind', 'role', 'engine', 'prompt'].some(field => current[field] !== row[field])) {
        say(note, '요청이 변경되었습니다. 갱신된 요청을 확인하세요.');
        return;
      }
      if (current.status !== 'pending') {
        say(note, '이미 처리된 요청입니다. 최신 기록을 확인하세요.');
        return;
      }
      const {request: receipt} = await api(path + '/' + row.id + '/decision', body);
      if (receipt?.id !== row.id || receipt.run_id !== row.run_id || receipt.status === 'pending') throw new Error();
      editors.delete('request:' + key);
      if (active()) say(note, '결정을 기록했습니다.');
    } catch (error) {
      if (active()) say(note, error.status === 400 ? error.message :
        '결정 접수를 확인하지 못했습니다. 최신 요청을 다시 확인합니다.');
    } finally {
      deciding.delete(key);
      if (sameContext(context)) {
        requestControls();
        refresh();
      }
    }
  }

  get('inbox').addEventListener('click', () => showRequests());
  get('requests').addEventListener('close', () => {
    if (!get('requests').open) ++inboxEpoch;
  });

  function pickedSkills(role, fallback) {
    const root = get(role + '-skills');
    return root.children.length ? Array.from(root.querySelectorAll('input:checked'), input => input.value) : fallback;
  }

  function renderSkills(picks) {
    for (const role of Object.keys(roleLabels)) {
      const root = get(role + '-skills');
      const ids = new Set([...skills.map(skill => skill.id), ...picks[role]]);
      if (!ids.size && !skillsCursor) {
        root.replaceChildren();
        continue;
      }
      const wrapper = make('details');
      wrapper.append(make('summary', '역할 스킬'));
      for (const id of ids) {
        const skill = skills.find(item => item.id === id);
        const label = make('label', '', 'checkbox');
        const box = make('input');
        box.type = 'checkbox';
        box.value = id;
        box.checked = picks[role].includes(id);
        label.append(box, make('span', skill ? skill.name + ' · v' + skill.version : '지정한 스킬 ' + id));
        wrapper.append(label);
      }
      if (skillsCursor) {
        const older = make('button', '이전 스킬 버전', 'secondary');
        older.type = 'button';
        older.addEventListener('click', () => loadSkills(true));
        wrapper.append(older);
      }
      const wasOpen = root.firstElementChild?.open;
      root.replaceChildren(wrapper);
      if (wasOpen) wrapper.open = true;
    }
  }

  async function loadSkills(older = false) {
    const token = epoch;
    try {
      const path = older && skillsCursor ? '/skills/before/' + skillsCursor : '/skills';
      const page = await api(path);
      if (!sameContext(token) || !get('room-settings').open) return;
      const selected = Object.fromEntries(Object.keys(roleLabels).map(role => [role, pickedSkills(role, room.settings[role].skills)]));
      skills = Array.from(new Map([...skills, ...page.skills].map(row => [row.id, row])).values());
      skillsCursor = validId(page.next_cursor) ? page.next_cursor : null;
      renderSkills(selected);
    } catch {
      if (sameContext(token) && get('room-settings').open) say('settings-note', '스킬 목록을 확인할 수 없습니다. 지정된 스킬은 그대로 적용합니다.');
    }
  }

  get('settings').addEventListener('click', () => {
    if (!canWrite() || settingsBusy) return;
    skills = [];
    skillsCursor = null;
    get('room-delegation').checked = room.settings.delegation;
    for (const role of Object.keys(roleLabels)) {
      const profile = room.settings[role];
      get(role + '-name').value = profile.name;
      const picker = get(role + '-engine');
      picker.replaceChildren(...Object.entries(engines).map(([id, name]) => {
        const probe = room.engines?.find(item => item.engine === id);
        const hint = probe?.probe_status === 'ok' ? ' · CLI 확인' : probe?.probe_status === 'missing' ? ' · 설치 필요' : '';
        return option(id, name + hint);
      }));
      picker.value = profile.engine;
    }
    renderSkills(Object.fromEntries(Object.keys(roleLabels).map(role => [role, room.settings[role].skills])));
    say('settings-note', '');
    openDialog(get('room-settings'), get('room-delegation'));
    loadSkills();
  });

  get('settings-form').addEventListener('submit', async event => {
    event.preventDefault();
    if (!canWrite() || settingsBusy) return;
    const token = epoch;
    const body = {delegation: get('room-delegation').checked};
    for (const role of Object.keys(roleLabels)) {
      body[role] = {name: get(role + '-name').value.trim(), engine: get(role + '-engine').value,
        skills: pickedSkills(role, room.settings[role].skills)};
      if (!body[role].name) return say('settings-note', '에이전트 이름을 입력하세요.');
    }
    settingsBusy = get('save-settings').disabled = true;
    say('settings-note', '설정을 저장하고 있습니다.');
    try {
      const {settings} = await api(roomPath(), body);
      if (!sameContext(token)) return;
      room.settings = settings;
      get('room-settings').close();
      refresh();
    } catch (error) {
      if (sameContext(token)) say('settings-note', error.status < 500 ? error.message : '설정 저장을 확인할 수 없습니다.');
    } finally {
      settingsBusy = get('save-settings').disabled = false;
    }
  });

  function showRegister() {
    if (!online || registrationBusy) return;
    say('register-note', '');
    openDialog(get('register-dialog'), get('workspace-name'));
  }

  get('register').addEventListener('click', showRegister);
  get('empty-register').addEventListener('click', showRegister);
  get('register-form').addEventListener('submit', async event => {
    event.preventDefault();
    if (!online || registrationBusy) return;
    const token = epoch;
    const body = {name: get('workspace-name').value.trim(),
      paths: get('workspace-paths').value.split(/\r?\n/).map(path => path.trim()).filter(Boolean)};
    if (!body.name || !body.paths.length) return say('register-note', '이름과 폴더 경로를 입력하세요.');
    registrationBusy = get('save-workspace').disabled = true;
    try {
      const {workspace} = await api('/workspaces', body);
      if (!sameContext(token)) return;
      get('register-form').reset();
      registrationBusy = false;
      navigation(hostId, workspace.id, null);
    } catch (error) {
      if (sameContext(token)) say('register-note', error.status < 500 ? error.message : '등록 여부를 확인할 수 없습니다.');
    } finally {
      registrationBusy = get('save-workspace').disabled = false;
    }
  });

  get('host').addEventListener('change', event => navigation(event.target.value, null, null));
  get('refresh').addEventListener('click', refresh);
  get('history-older').addEventListener('click', loadOlderHistory);
  get('stop-run').addEventListener('click', async () => {
    const run = selected();
    if (!run || !canWrite() || !stoppable(run.state) || stopping.has(run.id)) return;
    const token = epoch;
    const key = run.id;
    const current = () => sameContext(token) && runId === key;
    stopping.add(key);
    renderLedger();
    say('stop-note', '중지 요청을 보내는 중입니다.');
    try {
      const {run: receipt} = await api(runPath(key) + '/stop', {});
      if (receipt?.id !== key || receipt.workspace_id !== workspaceId) throw new Error();
      if (current()) {
        detail = {...detail, ...receipt};
        renderRoom();
        say('stop-note', receipt.state === 'STOP_REQUESTED' ? '중지 요청을 접수했습니다.' : '최신 실행 상태를 확인했습니다.');
      }
    } catch (error) {
      if (current()) say('stop-note', error.status < 500 ? error.message : '중지 요청 접수를 확인하지 못했습니다. 최신 상태를 다시 확인합니다.');
    } finally {
      stopping.delete(key);
      if (current()) refresh();
    }
  });
  function restoreAddress() {
    const target = readAddress();
    navigation(target.host, target.workspace, target.run, true);
  }
  win.addEventListener('popstate', restoreAddress);
  win.addEventListener('online', refresh);
  refresh();
  return () => {
    closed = true;
    win.clearTimeout(timer);
    win.removeEventListener('popstate', restoreAddress);
    win.removeEventListener('online', refresh);
  };
}

if (typeof document !== 'undefined') mountWorkspaceOffice();
