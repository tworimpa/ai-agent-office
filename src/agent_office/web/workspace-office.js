import {roomRefresh} from './workspace-refresh.js';
import {mountSeats} from './workspace-seats.js';
import {watchProgress} from './workspace-progress.js';
import {activeStates, stateName, taskTitle, summaryConclusion, resultFacts, outcomes} from './workspace-summary.js';
import {validId, pageDom, requestJson, visiblePoller} from './studio-shared.js';
import {firstProseParagraph, renderDiff, renderMarkdown, renderSource} from './office-content.js';
import {planFlow} from './office-flow.js';
import {mountGoals} from './workspace-goal.js';
import {mountStorage} from './workspace-storage.js';
import {mountGithub} from './workspace-github.js';
import {mountBriefing} from './workspace-briefing.js';
import {mountWorkProfiles} from './workspace-profile.js';
import {mountCharacterRecords} from './workspace-character.js';

const roleLabels = {developer: '담당자', reviewer: '검토자'};
const engines = {codex: 'Codex', antigravity: 'Antigravity', claude: 'Claude Code', gemini: 'Gemini CLI', cline: 'Cline CLI'};
const stoppable = state => activeStates.has(state) && state !== 'STOP_REQUESTED';
const followupStates = {developer: 'RUNNING', reviewer: 'REVIEWING'};
const requestLabels = {question: '기술 질문', product_decision: '제품 동작 결정', permission: '권한 승인 요청'};
const requestStateLabels = {decided: '결정 접수됨', delivered: '에이전트에게 전달됨', expired: '만료됨'};
const deliveryLabels = {queued: '전달 대기', sending: '전달 중', delivered: '전달됨', uncertain: '전달 여부 확인 필요', undelivered: '전달되지 않음'};
const decisionLabels = {answer: '답변', approve: '이번 요청 승인', reject: '거절'};
const meetingStates = {planning: '계획 논의 중', awaiting_review: '계획 확인 대기', approved: '계획 확정', stopped: '회의 중단', failed: '회의 실패'};
const verificationStates = {not_verified: '검증 전', passed: '통과', failed: '실패'};
const meetingRoles = {coordinator: '조정자', ...roleLabels, operator: '사용자', direction: '추가 지시'};
const validHistoryCursor = value => typeof value === 'string' && /^[ah]-[a-f0-9]{32}$/.test(value);
function uniqueRuns(runs) {
  const ids = new Set();
  return runs.filter(run => {
    if (ids.has(run.id)) return false;
    ids.add(run.id);
    return true;
  });
}
const runLabel = run => (run.state === 'RUNNING' || run.state === 'QUEUED') &&
  ['planning', 'awaiting_review'].includes(run.meeting_status) ? meetingStates[run.meeting_status] : stateName(run.state);
const engineName = engine => engines[engine] ?? '엔진 미확인';
const codeBytes = value => new TextEncoder().encode(value).length;
const stamp = ts => {
  if (!ts || Number.isNaN(Date.parse(ts))) return '';
  return new Intl.DateTimeFormat('ko-KR', {month: 'numeric', day: 'numeric', hour: 'numeric', minute: '2-digit'}).format(new Date(ts));
};
const scope = host => host === 'local' ? '/api' : '/api/hosts/' + host;
const viewNames = {simple: '간단히', collaboration: '협업 과정', technical: '기술 상세'};
function address(host, workspace, run, base = '/') {
  const query = new URLSearchParams();
  if (host !== 'local') query.set('host', host);
  if (workspace) query.set('workspace', workspace);
  if (run) query.set('run', run);
  return base + (query.size ? '?' + query : '');
}

export function mountWorkspaceOffice(win = window) {
  const doc = win.document;
  const {get, say, make} = pageDom(doc);
  const progress = watchProgress(doc, win);
  const seats = mountSeats(doc, roleLabels);

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
  let roomLoadError = '';
  let olderRuns = [], historyCursor = null, historyPaged = false, historyBusy = false, historyEpoch = 0;
  let online = false, roomOnline = false, closed = false, epoch = 0, refreshToken = 0;
  let retryBundleAt = 0, slowReadAt = 0, previousRunState = '';
  const poller = visiblePoller(win, () => refresh(true), () => { ++refreshToken; });
  let chatRole = null, chatAttempt = null, receiptRunId = null, chatRows = [], directionRows = [], chatDetailsOnline = false, inboxDetailsOnline = false;
  let chatState = null, settingsBusy = false, registrationBusy = false, deciding = new Set();
  let meetingDecision = null, meetingReadOK = false;
  let view = 'simple', jump = null, threadRun = null, directionsOnline = false;
  const viewPreference = 'agent-office.conversation-view';
  try {
    const saved = win.localStorage.getItem(viewPreference);
    if (Object.hasOwn(viewNames, saved)) view = saved;
  } catch { /* View selection also works when local storage is unavailable. */ }
  let inboxFocus;
  const stopping = new Set();
  let dialogEpoch = 0, inboxEpoch = 0, detailRead = 0, chatRead = 0, inboxRead = 0, skills = [], skillsCursor = null;
  const recovery = new Map();
  const editors = new Map();
  const answerDetails = new Map();
  const roomPath = () => '/rooms/' + workspaceId;
  const runPath = id => '/runs/' + id;
  const roomRuns = () => room ? [...room.runs, ...olderRuns] : [];
  const summary = () => roomRuns().find(run => run.id === runId);
  const selected = () => detail ? {...summary(), ...detail} : summary();
  const trustedRun = id => Boolean(id && (roomRuns().some(run => run.id === id) ||
    (detail?.id === id && detail.workspace_id === workspaceId)));
  const sameContext = token => !closed && token === epoch;
  const canWrite = () => !closed && roomOnline && !!room;
  const supportsMeetings = () => hostId === 'local' || room?.meeting_supported === true;

  const request = (path, body, timeoutMs) => requestJson(win, path, body, {timeoutMs});
  const api = (path, body, timeoutMs) => request(scope(hostId) + path, body, timeoutMs);
  const uuid = () => win.crypto.randomUUID().replaceAll('-', '');
  const profileContext = () => ({workspaceId, runId, identity: [hostId, room?.journal_id, workspaceId].join(':'),
    supported: room?.work_profiles_supported === true, assignment: selected()?.assignment, settings: room?.settings, writable: canWrite()});
  const storage = mountStorage({get, api, doc, context: () => ({
    workspaceId, journal: room?.journal_id, supported: room?.storage_supported === true, epoch, writable: canWrite(),
  })});
  const profiles = mountWorkProfiles({get, make, api, context: profileContext});
  const records = mountCharacterRecords({get, make, api, context: profileContext, openEvidence: openCharacterEvidence});
  const briefing = mountBriefing({win, get, make, api, taskTitle, stateName, uuid, refresh, openAction: openBriefAction,
    context: () => ({workspaceId, identity: hostId + ':' + workspaceId, runs: roomRuns(),
      visitKey: room?.work_profiles_supported && validId(room?.journal_id) ? profileContext().identity : null,
      sharedVisit: room?.shared_visits_supported === true,
      supported: room?.briefing_supported === true, writable: canWrite()})});
  const github = mountGithub({get, make, api, desktop: /\bElectron\//i.test(win.navigator.userAgent),
    uuid, briefing, pollSoon: () => poller.schedule(3000),
    openGoal: id => { if (validId(id)) navigation(hostId, workspaceId, id); },
    context: () => ({id: workspaceId, host: hostId, supported: room?.github_supported === true,
      folders: room?.workspace.folders ?? [], runs: roomRuns(), writable: canWrite(), name: directory.find(host => host.id === hostId)?.name ?? hostId})});
  const goals = mountGoals({win, get, make, api, refresh, onPolicySaved: () => { room.goal_default = true; }, context: () => ({
    hostId, workspaceId, runId, epoch, supported: room?.goal_supported === true,
    writable: canWrite(), pending: selected()?.pending_count, meetingPhase: selected()?.meeting?.status ?? selected()?.meeting_status,
  }), openInbox: () => get('inbox').click(), openMeeting: () => openEvidence('room', 'meeting'),
    openRun: id => navigation(hostId, workspaceId, id)});

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
    resetThread();
    roomLoadError = '';
    const changingRoom = host !== hostId || workspace !== workspaceId;
    if (host !== hostId) {
      online = false;
      workspaces = [];
    }
    if (changingRoom) {
      retryBundleAt = 0;
      github.reset();
      briefing.reset();
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
    meetingReadOK = false;
    say('meeting-note', '');
    say('detail-status', '');
    say('stop-note', '');
    closeDialogs();
    ++epoch;
    win.history[replace ? 'replaceState' : 'pushState'](null, '', address(hostId, workspaceId, runId));
    render();
    return refresh();
  }

  function resetThread() {
    goals.reset();
    answerDetails.clear();
    chatRows = [];
    directionRows = [];
    threadRun = null;
    chatDetailsOnline = directionsOnline = false;
    if (jump) view = jump.view;
    jump = null;
    say('thread-note', '');
    say('thread-read-note', '');
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
    const meta = [runLabel(run), stamp(run.created_at), run.pending_count && '응답 ' + run.pending_count + '건'].filter(Boolean);
    const title = taskTitle(run.task);
    link.append(make('span', title, 'run-task'), make('small', meta.join(' · ')));
    link.title = title;
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
    update('run-list', [workspaceId, runId, runs.map(({id, task, state, created_at, pending_count, meeting_status}) => ({
      id, task, state, created_at, pending_count, meeting_status,
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

  function nextStep(run) {
    const phase = run.meeting?.status ?? run.meeting_status;
    const row = chatDetailsOnline && threadRun === run.id && chatRows.find(item => item.status === 'pending' &&
      !item.delegation_in_progress && Object.hasOwn(requestLabels, item.kind));
    if (row && activeStates.has(run.state) && run.state !== 'STOP_REQUESTED') {
      const permission = row.kind === 'permission';
      const prompt = permission ? '요청 범위를 읽고 승인 또는 거절을 선택해 주세요.' : firstProseParagraph(row.prompt);
      return {key: row.kind, row, role: row.role, state: requestLabels[row.kind], next: prompt || '질문에 답해 주세요.',
        label: permission ? '권한 요청 확인' : '질문에 답하기'};
    }
    if (phase === 'awaiting_review' && stoppable(run.state)) {
      const choices = run.meeting?.choices ?? [];
      return {key: 'plan', role: 'coordinator', state: '계획 확인 대기',
        next: choices.length ? '제품 동작을 선택하고 계획을 확인해 주세요.' : '제안된 계획을 읽고 시작 여부를 결정해 주세요.', label: '계획 확인하기'};
    }
    if (stoppable(run.state) && run.pending_count > 0 && (threadRun !== run.id || !chatDetailsOnline)) {
      return {key: 'inbox', state: '응답 필요', next: '이 작업에서 응답을 기다리는 요청이 있습니다.', label: '요청 확인'};
    }
    if (activeStates.has(run.state)) {
      const planning = phase === 'planning' && stoppable(run.state);
      const role = planning || ['QUEUED', 'STOP_REQUESTED'].includes(run.state) || run.kind === 'parallel' ? 'coordinator' :
        run.state === 'TESTING' ? 'tester' : run.state === 'REVIEWING' ? 'reviewer' : 'developer';
      let next = '지금 응답할 항목이 없습니다.';
      if (!chatDetailsOnline && run.kind !== 'parallel') next = threadRun === run.id || !roomOnline ?
        '질문과 권한 요청의 최신 상태를 확인할 수 없습니다.' : '질문과 권한 요청의 최신 상태를 확인 중입니다.';
      return {key: 'chat', state: planning ? meetingStates.planning : runLabel(run), role, next, label: '진행 상황 보기'};
    }
    const role = run.outcome === 'TEST_FAILED' ? 'tester' : ['READY_FOR_HUMAN_REVIEW', 'WAITING_USER'].includes(run.state) ||
      ['CHANGES_REQUESTED', 'REVIEW_FAILED'].includes(run.outcome) ? 'reviewer' :
      run.state === 'PLAN_ONLY' || run.outcome === 'DEVELOPER_FAILED' ? 'developer' : 'coordinator';
    if (run.state === 'FAILED' || run.state === 'RECOVERY_REQUIRED') {
      const key = run.diagnostic ? 'diagnostic' : run.outcome === 'TEST_FAILED' ? 'tests' :
        run.meeting?.error ? 'meeting' : run.outcome === 'REVIEW_FAILED' && run.review_output ? 'review' : 'chat';
      const cause = outcomes[run.outcome] ?? '실패 원인을 확인해 주세요.';
      return {key, role, state: runLabel(run), next: cause, label: '실행 기록 확인'};
    }
    const next = {PLAN_ONLY: '담당자가 남긴 조회 결과를 확인하세요.', READY_FOR_HUMAN_REVIEW: '변경 사항과 검토 결과를 확인하세요.',
      WAITING_USER: outcomes.REVIEW_UNDECIDED, CANCELED: outcomes.CANCELED}[run.state] ?? '이 작업의 실행 기록을 확인하세요.';
    return {key: 'chat', role, state: runLabel(run), next, label: '결과 확인'};
  }

  function renderTaskSummary(run) {
    get('task-summary').hidden = !run;
    if (!run) return;
    const step = nextStep(run);
    const profile = run.assignment?.roles?.[step.role];
    const role = roleLabels[step.role];
    const owner = role ? [role, profile?.name, engineName(run[step.role + '_engine'] ?? profile?.engine)].filter(Boolean).join(' · ') :
      step.role === 'tester' ? '테스트 실행기' : '조정자';
    say('summary-title', taskTitle(run.task));
    say('summary-state', step.state);
    say('summary-owner', owner);
    say('summary-next', step.next);
    const conclusion = detail && !activeStates.has(run.state) ? summaryConclusion(run) : '';
    get('summary-result').hidden = !conclusion;
    say('summary-result', conclusion);
    say('summary-action', step.label);
    get('summary-action').disabled = !roomOnline || (['plan', 'meeting', 'diagnostic', 'tests', 'review'].includes(step.key) && !meetingReadOK);
    const facts = detail && !activeStates.has(run.state) ? resultFacts(run) : [];
    get('summary-facts').hidden = !facts.length;
    update('summary-facts', facts, list => list.replaceChildren(...facts.map(fact => make('li', fact))));
    const others = room.runs.filter(row => row.id !== run.id && row.pending_count > 0);
    get('summary-other').hidden = !others.length;
    update('summary-other', [hostId, workspaceId, others.map(({id, task, pending_count}) => [id, task, pending_count])], node => {
      const focused = node.contains(doc.activeElement) && doc.activeElement.dataset.run;
      node.replaceChildren(make('span', '다른 작업에서 응답을 기다립니다'));
      for (const other of others) {
        const button = make('button', taskTitle(other.task) + ' · ' + other.pending_count + '건 확인');
        button.type = 'button';
        button.dataset.run = other.id;
        button.addEventListener('click', () => openOtherRequest(other.id));
        node.append(button);
        if (focused === other.id) button.focus({preventScroll: true});
      }
    });
    for (const button of get('summary-other').querySelectorAll('button')) button.disabled = !roomOnline;
  }

  async function openOtherRequest(id) {
    if (!canWrite() || !room.runs.some(row => row.id === id && row.pending_count > 0)) return;
    const token = epoch + 1;
    await navigation(hostId, workspaceId, id);
    if (!sameContext(token) || runId !== id || !roomOnline) return;
    const run = selected();
    if (!run) return;
    const step = nextStep(run);
    if (step.row || ['plan', 'inbox'].includes(step.key)) openSummaryStep(run, step);
    else get('summary-action').focus();
  }

  function openSummaryStep(run, step) {
    if (!canWrite() || run.id !== runId) return;
    if (step.key === 'plan') {
      if (!meetingReadOK || get('meeting-decision').hidden) return;
      openEvidence('room', 'meeting');
      (get('meeting-inputs').querySelector('input[type="radio"]:not(:disabled)') ?? get('approve-plan')).focus();
    } else if (step.key === 'inbox' || step.key === 'permission') {
      showRequests(step.row ? run.id + ':' + step.row.id : null, run.id);
    } else if (step.row) {
      doc.getElementById('thread-answer-' + run.id + '-' + step.row.id)?.focus();
    } else if (step.key === 'chat') {
      showChat(step.role === 'reviewer' ? 'reviewer' : 'developer', run.id);
    } else {
      openEvidence('room', step.key);
    }
  }

  async function openBriefAction(kind, row) {
    if (!canWrite() || !validId(row.run_id)) return false;
    const switchRun = runId !== row.run_id, token = epoch + Number(switchRun);
    if (switchRun) await navigation(hostId, workspaceId, row.run_id);
    if (!sameContext(token) || !canWrite() || !trustedRun(row.run_id) || runId !== row.run_id) return false;
    if (kind === 'permission') {
      showRequests(row.run_id + ':' + row.request_id, row.run_id);
      return true;
    }
    if (kind === 'plan') {
      if (!meetingReadOK || get('meeting-decision').hidden || detail?.meeting?.revision !== row.revision) return false;
      openSummaryStep(selected(), {key: 'plan'});
      return true;
    }
    let target;
    if (kind === 'answer') {
      target = get('thread-answer-' + row.run_id + '-' + row.request_id);
      if (!chatDetailsOnline || target?.disabled) return false;
    } else if (kind === 'candidate') {
      const fold = [...get('goal-record').querySelectorAll('[data-goal-attempt]')].find(node =>
        node.dataset.goalAttempt === String(row.attempt) && node.dataset.digest === row.digest && node.dataset.current === 'true');
      if (!fold || get('goal-report').hidden) return false;
      fold.open = true;
      target = fold.querySelector('[data-goal-diff]');
    } else if (kind === 'usage' && !get('work-usage').hidden) {
      get('work-usage').open = true;
      target = get('work-usage').querySelector('summary');
    } else if (kind === 'work') {
      target = get(get('goal-report').hidden ? 'ledger-heading' : 'goal-title');
    }
    if (!target) return false;
    if (!target.matches('button,textarea')) target.tabIndex = -1;
    target.focus();
    return true;
  }

  async function openCharacterEvidence(fact) {
    if (!canWrite() || !validId(fact.run_id)) return;
    get('conversation').close();
    if (!await openBriefAction('work', fact)) return;
    const candidate = Number.isInteger(fact.attempt) &&
      [...get('goal-record').querySelectorAll('[data-goal-attempt]')].find(node => node.dataset.goalAttempt === String(fact.attempt));
    let target;
    if (fact.section === 'candidate' && candidate) {
      candidate.open = true;
      target = candidate.querySelector('summary');
    } else if (fact.section === 'checks') {
      target = [...get('goal-record').querySelectorAll('[data-goal-check]')].find(node =>
        (!Number.isInteger(fact.attempt) || node.dataset.attempt === String(fact.attempt)) &&
        (!Number.isInteger(fact.position) || node.dataset.position === String(fact.position)));
      if (!target) { openEvidence('room', 'tests'); return; }
      target.open = true;
      target = target.querySelector('summary') || target;
    } else if (fact.section === 'review' && candidate?.querySelector('[data-review-kind="review"]')) {
      candidate.open = true;
      const review = candidate.querySelector('[data-review-kind="review"]');
      review.open = true;
      target = review.querySelector('summary');
    } else if (fact.section === 'review' || fact.section === 'meeting') {
      openEvidence('room', fact.section);
      if (fact.section === 'meeting' && Number.isInteger(fact.revision)) {
        const version = get('meeting-record').querySelector('[data-revision="' + fact.revision + '"]');
        if (version) { version.open = true; target = version.querySelector('summary'); }
      }
    } else if (fact.section === 'directions' || fact.section === 'requests') {
      changeView('collaboration', false);
      const key = fact.direction_id || fact.request_id;
      target = [...get('office-thread').querySelectorAll('[data-receipt]')].find(node =>
        key ? node.dataset.receipt === key : node.dataset.section === fact.section);
    }
    if (target) { target.tabIndex = -1; target.focus(); }
  }

  get('summary-action').addEventListener('click', () => {
    const run = selected();
    if (run) openSummaryStep(run, nextStep(run));
  });

  function renderRoom() {
    const currentRun = selected();
    progress.select({id: currentRun?.id, active: roomOnline && activeStates.has(currentRun?.state),
      url: currentRun && hostId === 'local' && validId(room?.journal_id) ? '/api/runs/' + currentRun.id + '/stream?office_journal=' + room.journal_id : null,
      latest: currentRun?.last_progress});
    profiles.controls();
    records.controls();
    get('office-main').dataset.briefing = String(room?.briefing_supported === true);
    get('room').hidden = !room;
    get('empty-room').hidden = !!room;
    get('empty-register').hidden = !online || workspaces.length > 0;
    get('empty-register').disabled = registrationBusy;
    say('empty-register', canPickFolders() ? '폴더 선택' : '작업공간 등록');
    get('empty-retry').hidden = Boolean(room) || !roomLoadError;
    if (!room) {
      say('empty-title', workspaces.length ? '작업실 선택' : '작업실');
      say('empty-note', roomLoadError || (workspaceId && !roomOnline ? '작업실 정보를 불러오는 중이거나 호스트와 연결되지 않았습니다.' :
        workspaces.length ? '목록에서 들어갈 작업공간을 선택하세요.' :
        online ? '폴더를 등록해 작업실을 열 수 있습니다.' : '작업공간을 불러오는 중입니다.'));
      return;
    }
    say('room-title', room.workspace.name);
    briefing.controls();
    github.controls();
    if (!roomOnline) goals.stale();
    const count = room.runs.reduce((total, run) => total + (run.pending_count ?? 0), 0);
    get('inbox-count').hidden = count < 1;
    say('inbox-count', String(count));
    get('settings').disabled = !roomOnline;
    get('inbox').disabled = !roomOnline && !get('requests').open;
    const firstTask = !room.runs.length && !selected() && !runId;
    doc.querySelector('.room-scene').dataset.first = String(firstTask);
    seat('coordinator').querySelector('.seat-start').hidden = !firstTask;
    for (const [role, label] of Object.entries(roleLabels)) {
      const current = visibleRun(role);
      const profile = current?.assignment?.roles?.[role] ?? roomPerson(role);
      const engine = current?.[role + '_engine'] ?? profile.engine;
      const node = seat(role);
      let line = current?.lines?.[role] || room.runs.find(run => run.id === current?.id)?.lines?.[role];
      if (!line) line = current && (role === 'developer' || current.allow_edits) ? stateName(current.state) : '배정된 작업이 없습니다.';
      const roleText = label + ' · ' + engineName(engine);
      const task = node.querySelector('.seat-task');
      const assignment = current && (role === 'developer' || current.allow_edits || current.assignment?.meeting) ? taskTitle(current.task) : '';
      node.querySelector('.seat-name').textContent = profile.name;
      node.querySelector('.seat-role').textContent = roleText;
      node.querySelector('.seat-line').textContent = line;
      task.textContent = assignment ?? '';
      task.hidden = !assignment;
      node.title = [assignment, line].filter(Boolean).join(' · ');
      node.dataset.request = String((current?.pending_count ?? room.runs.find(run => run.id === current?.id)?.pending_count ?? 0) > 0);
      node.setAttribute('aria-label', [profile.name, roleText, assignment, line, '대화 열기'].filter(Boolean).join(', '));
    }
    const children = currentRun?.subtasks ?? room.runs.filter(row => validId(row.team_parent_id) &&
      (activeStates.has(row.state) || row.team_parent_id === currentRun?.team_parent_id || row.team_parent_id === currentRun?.parent_id));
    const cards = children.map(row => ({id: row.id, role: 'child', title: taskTitle(row.task), state: stateName(row.state),
      label: '하위 작업 · ' + engineName(row.developer_engine)}));
    for (const row of room.runs) {
      for (const visitor of row.visiting_roles ?? []) {
        cards.push({id: row.id, role: visitor.role, title: taskTitle(row.task), state: '근거 확인 중',
          label: (visitor.role === 'specialist' ? '전문가' : '독립 검증자') + ' · ' + engineName(visitor.engine)});
      }
    }
    seats.showRuns(cards, id => address(hostId, workspaceId, id));
    const coordinatorLine = selected()?.lines?.coordinator ?? summary()?.lines?.coordinator;
    seat('coordinator').querySelector('.seat-line').textContent = firstTask ? '원하는 결과를 적어 주세요.' :
      coordinatorLine || '새 작업을 맡길 수 있습니다.';
    const run = selected(), selectedSummary = get('selected-summary'), summaryPanel = get('task-summary');
    selectedSummary.hidden = !room.briefing_supported || !run;
    if (room.briefing_supported && summaryPanel.parentNode !== selectedSummary) selectedSummary.append(summaryPanel);
    if (!room.briefing_supported && summaryPanel.parentNode === selectedSummary) get('workspace-briefing').after(summaryPanel);
    renderTaskSummary(run);
    renderLedger();
  }

  function renderLedger() {
    const run = selected();
    get('detail-link').hidden = !run;
    get('detail-link').href = address(hostId, null, run?.id, '/studio') + '#detail';
    get('stop-run').hidden = !stoppable(run?.state);
    get('stop-run').disabled = !canWrite() || stopping.has(run?.id);
    say('ledger-label', run ? runLabel(run) : '작업 기록');
    say('ledger-heading', run ? taskTitle(run.task) : '선택한 작업이 없습니다');
    say('ledger-note', run ? stamp(run.created_at) : '조정자에게 요청을 보내 작업을 시작할 수 있습니다.');
    get('office-thread').hidden = !run;
    renderViewTools('room', run);
    if (run) renderThread('office-thread', null);
    renderMeeting(detail);
    renderEvidence();
  }

  function changeView(next, persist) {
    view = next;
    if (persist) {
      try { win.localStorage.setItem(viewPreference, next); } catch { /* Tab-local fallback. */ }
    }
    renderRoom();
    if (chatRole) renderChatLog();
    requestControls();
  }

  function renderViewTools(place, run) {
    const id = place + '-view-controls';
    get(id).hidden = !run;
    if (!run) return;
    update(id, [hostId, run.id, Boolean(run.meeting), view, !!jump], node => {
      const picker = make('div', '', 'view-picker');
      picker.setAttribute('role', 'group');
      picker.setAttribute('aria-label', '대화 보기');
      for (const [key, label] of Object.entries(viewNames)) {
        const button = make('button', label, 'secondary');
        button.type = 'button';
        button.dataset.view = key;
        button.setAttribute('aria-pressed', String(key === view));
        button.addEventListener('click', () => {
          jump = null;
          changeView(key, true);
          node.querySelector('[data-view="' + key + '"]').focus({preventScroll: true});
        });
        picker.append(button);
      }
      const links = make('div', '', 'evidence-links');
      if (jump) {
        const back = make('button', '대화로 돌아가기', 'secondary view-return');
        back.type = 'button';
        back.dataset.back = '';
        back.addEventListener('click', returnToThread);
        links.append(back);
      } else if (view === 'simple') {
        for (const [key, label] of Object.entries({changes: '변경 보기', tests: '테스트 보기', meeting: '회의 보기'})) {
          if (key === 'meeting' && !run.meeting) continue;
          const button = make('button', label, 'secondary');
          button.type = 'button';
          button.dataset.jump = key;
          button.disabled = !detail;
          button.addEventListener('click', () => openEvidence(place, key));
          links.append(button);
        }
      }
      const active = node.contains(doc.activeElement) && doc.activeElement.dataset;
      node.replaceChildren(picker, links);
      if (active) {
        const restored = [...node.querySelectorAll('button')].find(button =>
          ['view', 'jump', 'back'].some(key => active[key] !== undefined && active[key] === button.dataset[key]));
        restored?.focus({preventScroll: true});
      }
    });
    for (const button of get(id).querySelectorAll('[data-jump]')) button.disabled = !detail;
  }

  function scrollContainers(place) {
    return [doc.scrollingElement ?? doc.documentElement, ...(place === 'chat' ? [get('conversation'), get('chat-log')] : [get('office-thread')])];
  }

  function openEvidence(place, key) {
    if (!detail || detail.id !== runId || key === 'meeting' && !detail.meeting) return;
    jump = {place, key, view, scroll: scrollContainers(place).map(node => node.scrollTop)};
    changeView(key === 'meeting' ? 'collaboration' : 'technical', false);
    const target = key === 'meeting' ? get('meeting-heading') : get('evidence-' + key);
    if (!target) { returnToThread(); return; }
    if (key !== 'meeting') target.open = true;
    if (key === 'changes' && !target.querySelector('.office-diff details[open]')) {
      const first = target.querySelector('.office-diff details');
      if (first) first.open = true;
    }
    (target.querySelector('summary') ?? target).focus();
  }

  function returnToThread() {
    if (!jump) return;
    const previous = jump;
    jump = null;
    changeView(previous.view, false);
    const root = get(previous.place + '-view-controls');
    (root.querySelector('[data-jump="' + previous.key + '"]') ?? root.querySelector('[aria-pressed="true"]'))?.focus({preventScroll: true});
    scrollContainers(previous.place).forEach((node, index) => { node.scrollTop = previous.scroll[index]; });
  }

  function renderEvidence() {
    const node = get('ledger-result');
    node.hidden = view !== 'technical' || !detail;
    if (node.hidden) return;
    const run = detail;
    const files = run.assignment?.test_files ?? [];
    const sections = [
      ['changes', '변경 파일과 diff', [run.changes, run.diff], () => renderDiff(doc, {changes: run.changes ?? '', diff: run.diff ?? '', key: run.id + '-diff'})],
      ['tests', '테스트 결과', [files, run.test_output, run.state, run.outcome], () => {
        const body = make('div');
        if (files.length) meetingList(body, '지정한 테스트 파일', files);
        body.append(make('p', resultFacts(run)[1]));
        if (run.test_output) body.append(renderSource(doc, run.test_output, run.id + '-tests'));
        return body;
      }],
      ...[['developer', '개발 결과', run.developer_output], ['review', '검토 결과', run.review_output], ['diagnostic', '진단', run.diagnostic]]
        .filter(([, , value]) => value)
        .map(([key, label, value]) => [key, label, value, () => renderMarkdown(doc, value, {source: true, key: run.id + '-' + key})]),
    ];
    const context = [hostId, workspaceId, run.id].join(':');
    if (node.dataset.run !== context) {
      node.replaceChildren();
      node.dataset.run = context;
    }
    for (const item of [...node.children]) if (!sections.some(([key]) => item.id === 'evidence-' + key)) item.remove();
    for (const [key, label, data, body] of sections) {
      let item = get('evidence-' + key);
      if (!item) {
        item = make('details', '', 'evidence');
        item.id = 'evidence-' + key;
        item.append(make('summary', label), make('div'));
        node.append(item);
        snapshots.delete(item.id);
      }
      update(item.id, data, () => replaceContent(item.lastElementChild, [body()]));
    }
  }

  function meetingList(parent, heading, rows, ordered = false) {
    if (!rows?.length) return;
    const list = make(ordered ? 'ol' : 'ul');
    for (const row of rows) list.append(make('li', row));
    parent.append(make('h5', heading), list);
  }

  const planRecords = new Map();
  let planRun;
  function planRecord(plan, revision, slot) {
    const signature = JSON.stringify(plan);
    if (planRecords.get(slot)?.signature === signature) return planRecords.get(slot).body;
    const body = make('div', '', 'meeting-plan');
    if (!plan) return body;
    body.append(make('h5', '목표 결과'), make('p', plan.outcome));
    meetingList(body, '작업 범위', plan.scope);
    body.append(planFlow(doc, plan, revision));
    meetingList(body, '완료 기준과 검증 방법', plan.criteria.map(row =>
      [row.criterion, row.verification, row.test_files?.join(', ')].filter(Boolean).join(' · ')));
    meetingList(body, '계획 근거', plan.rationale);
    if (plan.evidence) {
      body.append(make('h5', '참고한 저장소 근거'), make('p', '기준 커밋 ' + plan.evidence.revision));
      meetingList(body, '파일', plan.evidence.paths);
    }
    planRecords.set(slot, {signature, body});
    return body;
  }

  function verificationRecord(parent, rows) {
    meetingList(parent, '완료 기준 검증 결과', rows?.map(row =>
      [row.criterion, verificationStates[row.status] ?? row.status, row.evidence].filter(Boolean).join(' · ')));
  }

  const decisionKey = (id, revision) => ['meeting-decision', hostId, workspaceId, id, revision].join(':');

  function renderMeeting(run) {
    const meeting = run?.meeting;
    const waiting = meeting?.status === 'awaiting_review' && !!meeting.plan && stoppable(run.state);
    const showRecord = view === 'collaboration';
    get('meeting').hidden = !meeting || (!showRecord && !waiting);
    get('meeting-record').hidden = !showRecord;
    get('chat-meeting').hidden = !meeting || view !== 'technical' || chatRole === 'coordinator' && !receiptRunId;
    get('meeting-decision').hidden = !waiting;
    const key = hostId + ':' + runId;
    if (planRun !== key) {
      planRecords.clear();
      planRun = key;
    }
    if (!meeting) return;
    const phase = meetingStates[meeting.status] ?? '상태 미확인';
    say('meeting-phase', [phase, meeting.revision > 0 && '계획 ' + meeting.revision + '판'].filter(Boolean).join(' · '));
    say('chat-meeting', phase + ' · 기획 회의 열기');
    if (meeting.decision) {
      const key = decisionKey(run.id, meeting.decision.revision);
      if (readRecovery(key)?.id === meeting.decision.id) {
        saveRecovery(key, null);
        say('meeting-note', '계획 결정을 기록했습니다.');
      }
    }
    if (showRecord) update('meeting-record', [hostId, run.id, meeting], node => {
      const focus = node.contains(doc.activeElement) ? doc.activeElement : null;
      const focusedRevision = focus?.closest('details[data-revision]')?.dataset.revision;
      const body = doc.createDocumentFragment();
      if (meeting.error) body.append(make('p', meeting.error));
      for (const [index, entry] of meeting.contributions.entries()) {
        const name = run.assignment?.roles?.[entry.role]?.name ?? meetingRoles[entry.role] ?? entry.role;
        const label = [name, engines[entry.engine] ?? entry.engine, entry.revision && '계획 ' + entry.revision + '판', stamp(entry.created_at)].filter(Boolean).join(' · ');
        const row = make('article', '', 'contribution');
        row.append(make('small', label), renderMarkdown(doc, entry.text, {key: run.id + '-contribution-' + index}));
        body.append(row);
      }
      if (meeting.plan) body.append(planRecord(meeting.plan, meeting.revision, 'current-' + meeting.revision));
      meetingList(body, '병렬 작업 실행 기록', meeting.work?.map(entry =>
        ['계획 ' + entry.revision + '판', entry.task_id, engines[entry.engine] ?? entry.engine,
          stamp(entry.created_at), entry.result].filter(Boolean).join(' · ')), true);
      for (const choice of meeting.choices) {
        const answer = meeting.answers[choice.id];
        const row = make('div', '', 'meeting-choice');
        row.append(make('h5', '제품 결정 · ' + choice.question),
          make('p', answer ? '사용자 선택 · ' + answer : '선택지 · ' + choice.options.join(' / ')));
        body.append(row);
      }
      verificationRecord(body, meeting.verification);
      for (const version of meeting.revisions) {
        const item = make('details', '', 'meeting-version');
        item.dataset.revision = version.revision;
        const source = meetingRoles[version.source] ?? version.source;
        item.append(make('summary', ['계획 ' + version.revision + '판', source, stamp(version.created_at)].filter(Boolean).join(' · ')));
        if (version.approved_by) item.append(make('p', '확정 · ' + (meetingRoles[version.approved_by] ?? version.approved_by)));
        if (version.reason) item.append(make('p', version.reason));
        if (version.direction_id) item.append(make('small', '추가 지시 ' + version.direction_id));
        item.append(planRecord(version.plan, version.revision, 'history-' + version.revision));
        version.plan.questions.forEach((question, index) => {
          const answer = version.answers?.['q' + (index + 1)];
          item.append(make('p', question.question + ' · ' + (answer || question.options.join(' / '))));
        });
        verificationRecord(item, version.verification);
        body.append(item);
      }
      const expanded = new Set([...node.querySelectorAll('details[data-revision][open]')].map(item => item.dataset.revision));
      replaceContent(node, [body]);
      for (const item of node.querySelectorAll('details[data-revision]')) item.open = expanded.has(item.dataset.revision);
      if (focus?.isConnected) focus.focus({preventScroll: true});
      else if (focusedRevision && doc.activeElement === doc.body) node.querySelector('details[data-revision="' + focusedRevision + '"] > summary')?.focus({preventScroll: true});
    });
    get('meeting-decision').hidden = !waiting;
    if (!waiting) return;
    get('decision-summary').hidden = showRecord;
    say('decision-summary', [meeting.plan.outcome, meeting.plan.scope?.join(', ')].filter(Boolean).join(' · '));
    update('meeting-inputs', [hostId, workspaceId, run.id, meeting.revision], node => {
      node.replaceChildren(...decisionInputs(meeting));
      say('meeting-note', '');
    });
    const disabled = !canWrite() || !meetingReadOK || !trustedRun(run.id) || !!meetingDecision;
    for (const control of get('meeting-decision').elements) control.disabled = disabled;
  }

  function decisionInputs(meeting) {
    const plan = meeting.plan;
    const questions = meeting.choices.map((choice, index) => {
      const group = make('fieldset');
      group.append(make('legend', choice.question + ' (필수)'));
      choice.options.forEach((value, number) => {
        const radio = make('input');
        radio.type = 'radio';
        radio.name = choice.id;
        radio.id = 'meeting-choice-' + index + '-' + number;
        radio.value = value;
        radio.required = true;
        radio.checked = meeting.answers[choice.id] === value;
        const label = make('label', '', 'meeting-option');
        label.htmlFor = radio.id;
        label.append(radio, make('span', value));
        group.append(label);
      });
      return group;
    });
    const edit = make('details', '', 'meeting-editor');
    edit.append(make('summary', '계획 수정'));
    function field(parent, name, label, value, options) {
      const control = make(options ? 'select' : 'textarea');
      control.name = name;
      control.id = 'meeting-' + name;
      if (options) {
        for (const [key, title] of options) control.append(option(key, title));
      } else control.rows = 2;
      control.value = value;
      control.dataset.initial = control.value;
      const title = make('label', label);
      title.htmlFor = control.id;
      parent.append(title, control);
      return control;
    }
    function pathField(group, name, title, paths, guards) {
      const control = field(group, name, title + ' (한 줄에 하나씩)', paths.join('\n'));
      if (!paths.length) return;
      const box = make('input');
      box.type = 'checkbox';
      box.name = 'retain-' + name;
      const label = make('label', '', 'checkbox');
      label.append(box, make('span', '수정한 내용에도 기존 ' + title + ' 연결 유지'));
      label.hidden = true;
      group.append(label);
      const changed = () => {
        label.hidden = control.value !== control.dataset.initial || guards.every(item => item.value === item.dataset.initial);
        box.checked = false;
      };
      for (const input of [control, ...guards]) input.addEventListener('input', changed);
    }
    field(edit, 'outcome', '목표 결과', plan.outcome);
    const scopeInput = field(edit, 'scope', '작업 범위 (한 줄에 하나씩)', plan.scope.join('\n'));
    plan.tasks.forEach((task, i) => {
      const group = make('fieldset');
      group.append(make('legend', '작업 ' + task.id));
      const description = field(group, 'task-' + i, '작업 내용', task.description);
      const owner = field(group, 'owner-' + i, '담당', task.owner, Object.entries(roleLabels));
      const dependencies = field(group, 'dependencies-' + i, '선행 작업 ID (쉼표로 구분)', task.depends_on.join(', '));
      pathField(group, 'folders-' + i, '작업 폴더', task.folders ?? [], [scopeInput, description, owner, dependencies]);
      edit.append(group);
    });
    plan.criteria.forEach((row, i) => {
      const group = make('fieldset');
      group.append(make('legend', '완료 기준 ' + (i + 1)));
      const criterion = field(group, 'criterion-' + i, '완료 기준', row.criterion);
      const verification = field(group, 'verification-' + i, '검증 방법', row.verification);
      pathField(group, 'tests-' + i, '테스트 파일', row.test_files ?? [], [criterion, verification]);
      edit.append(group);
    });
    field(edit, 'rationale', '계획 근거 (한 줄에 하나씩)', plan.rationale.join('\n'));
    return [...questions, edit];
  }

  function readDecision(meeting) {
    const form = get('meeting-decision');
    const edit = (name, original, separator) => {
      const control = form.elements.namedItem(name);
      if (control.value === control.dataset.initial) return original;
      return separator ? control.value.split(separator).map(item => item.trim()).filter(Boolean) : control.value.trim();
    };
    const paths = (name, original, guardNames) => {
      const changed = key => {
        const input = form.elements.namedItem(key);
        return input.value !== input.dataset.initial;
      };
      if (changed(name)) return edit(name, [], '\n');
      if (original?.length && guardNames.some(changed) && !form.elements.namedItem('retain-' + name).checked) return [];
      return original;
    };
    const plan = meeting.plan;
    return {
      revision: meeting.revision,
      plan: {
        ...plan, outcome: edit('outcome', plan.outcome), scope: edit('scope', plan.scope, '\n'), rationale: edit('rationale', plan.rationale, '\n'),
        tasks: plan.tasks.map((task, i) => {
          const folders = paths('folders-' + i, task.folders, ['scope', 'task-' + i, 'owner-' + i, 'dependencies-' + i]);
          return {...task, description: edit('task-' + i, task.description), owner: edit('owner-' + i, task.owner),
            depends_on: edit('dependencies-' + i, task.depends_on, ','), ...(folders && {folders})};
        }),
        criteria: plan.criteria.map((row, i) => {
          const files = paths('tests-' + i, row.test_files, ['criterion-' + i, 'verification-' + i]);
          return {...row, criterion: edit('criterion-' + i, row.criterion), verification: edit('verification-' + i, row.verification),
            ...(files && {test_files: files})};
        }),
      },
      answers: Object.fromEntries(meeting.choices.map(choice => [choice.id, form.elements.namedItem(choice.id).value])),
    };
  }

  get('meeting-decision').addEventListener('submit', async event => {
    event.preventDefault();
    const id = runId, token = epoch, meeting = detail?.meeting;
    if (!canWrite() || !meetingReadOK || meetingDecision || !trustedRun(id) || detail?.id !== id || !stoppable(detail.state) ||
        meeting?.status !== 'awaiting_review' || !event.currentTarget.reportValidity()) return;
    const decision = readDecision(meeting);
    const key = decisionKey(id, meeting.revision);
    const previous = readRecovery(key);
    const signature = JSON.stringify(decision);
    const body = {id: previous?.signature === signature ? previous.id : win.crypto.randomUUID().replaceAll('-', ''), ...decision};
    saveRecovery(key, {id: body.id, signature});
    meetingDecision = body.id;
    renderMeeting(detail);
    say('meeting-note', '계획 결정을 보내는 중입니다.');
    try {
      const receipt = (await api(runPath(id) + '/meeting/decision', body)).meeting;
      if (receipt?.run_id !== id || receipt.decision?.id !== body.id || receipt.decision?.revision !== body.revision) {
        throw new Error('계획 결정의 접수 여부를 확인하지 못했습니다.');
      }
      saveRecovery(key, null);
      if (sameContext(token) && runId === id) {
        say('meeting-note', '계획 결정을 기록했습니다.');
        if (detail?.id === id && detail.meeting.revision === meeting.revision) {
          detail = {...detail, meeting: receipt};
          renderRoom();
        }
      }
    } catch (error) {
      if (sameContext(token) && runId === id) say('meeting-note', error.status ? error.message : '접수 여부를 확인하지 못했습니다. 최신 회의 기록을 확인해 주세요.');
    } finally {
      if (sameContext(token) && runId === id) await refresh();
      meetingDecision = null;
      if (!closed) renderRoom();
    }
  });

  get('chat-meeting').addEventListener('click', () => {
    openEvidence('chat', 'meeting');
  });

  function renderConnection() {
    const status = get('connection');
    status.dataset.online = String(workspaceId ? roomOnline : online);
    const label = directory.find(item => item.id === hostId)?.name;
    say('connection', status.dataset.online === 'true' ? label + ' 연결됨' :
      room ? '연결 끊김 · 마지막 기록' : directory.length ? '연결 확인 필요' : '연결 중');
    renderChatConnection();
  }

  function renderChatConnection() {
    say('chat-connection', room && !roomOnline ? '연결 끊김 · 마지막 기록' : get('detail-status').textContent);
  }

  function render() {
    renderConnection();
    renderNavigation();
    renderRoom();
    if (chatRole) renderChatLog();
    chatControls();
    requestControls();
  }

  async function fetchRun(token, expected, readSection = api) {
    if (!expected) return;
    const read = ++detailRead;
    try {
      const {run} = await readSection(runPath(expected));
      if (!sameContext(token) || expected !== runId || read !== detailRead) return;
      if (run?.id !== expected) throw new Error('실행 정보가 달라졌습니다.');
      if (run.workspace_id !== workspaceId) {
        meetingReadOK = false;
        detail = null;
        say('detail-status', '이 실행은 선택한 작업공간에 속하지 않습니다.');
        renderChatConnection();
        renderRoom();
        return;
      }
      detail = run;
      if (!doc.hidden && room?.goal_supported === true) await goals.load(expected, readSection);
      if (!sameContext(token) || expected !== runId || read !== detailRead) return;
      meetingReadOK = true;
      say('detail-status', '');
      renderChatConnection();
      renderRoom();
      requestControls();
      if (get('conversation').open) {
        renderChatLog();
        chatControls();
      }
    } catch {
      if (sameContext(token) && expected === runId && read === detailRead) {
        meetingReadOK = false;
        say('detail-status', '실행 상세의 최신 상태를 확인할 수 없습니다.');
        renderChatConnection();
        renderRoom();
      }
    }
  }

  async function refresh(periodic = false) {
    poller.cancel();
    if (closed || doc.hidden) return;
    let readSection = api;
    let updateSlow = periodic !== true || Date.now() - slowReadAt >= 60000;
    const token = epoch;
    const sequence = ++refreshToken;
    const current = () => sameContext(token) && sequence === refreshToken;
    try {
      if (!directory.length || roomLoadError) {
        const {hosts} = await request('/api/hosts');
        if (!current()) return;
        directory = hosts;
      }
      if (!directory.some(host => host.id === hostId)) {
        roomLoadError = `실행 호스트를 찾을 수 없습니다 (${hostId}). 호스트 설정을 확인하고 다시 시도하세요.`;
        online = false;
        render();
        return;
      }
      if (!workspaceId || !workspaces.some(item => item.id === workspaceId)) {
        const {workspaces: list} = await api('/workspaces');
        if (!current()) return;
        workspaces = list;
      }
      if (!workspaceId && runId) {
        const {run} = await api(runPath(runId));
        if (!current()) return;
        if (run?.id !== runId || !validId(run.workspace_id)) throw new Error('실행의 작업공간을 확인할 수 없습니다.');
        workspaceId = run.workspace_id;
        detail = run;
      }
      online = true;
      if (!workspaceId) workspaceId = workspaces[0]?.id ?? null;
      if (workspaceId) {
        const reads = await roomRefresh(api, roomPath(), trustedRun(runId) ? runId : null,
          room?.bundle_supported === true && Date.now() >= retryBundleAt,
          updateSlow, get('work-usage').open, current);
        if (!current()) return;
        if (reads.retry) retryBundleAt = Date.now() + 60000;
        const snapshot = reads.room;
        readSection = reads.read;
        if (snapshot.workspace?.id !== workspaceId || !snapshot.settings || !Array.isArray(snapshot.runs)) throw new Error('작업실 정보 오류');
        const runs = snapshot.runs.filter(run => validId(run.id) && run.workspace_id === workspaceId);
        if (historyPaged || historyBusy) {
          const freshIds = new Set(runs.map(run => run.id));
          olderRuns = uniqueRuns([...(room?.runs ?? []), ...olderRuns]).filter(run => !freshIds.has(run.id));
        }
        if (!historyPaged && !historyBusy) historyCursor = validHistoryCursor(snapshot.next_cursor) ? snapshot.next_cursor : null;
        room = {...snapshot, runs};
        roomOnline = true;
        if (!runId) runId = (room.runs.find(row => row.meeting_status === 'awaiting_review') ??
          room.runs.find(row => row.pending_count > 0) ?? activeRuns()[0] ?? room.runs[0])?.id ?? null;
        if (room.runs.some(run => run.id === runId && (run.state !== detail?.state || run.attempts !== detail?.attempts))) detail = null;
      }
      roomLoadError = '';
      win.history.replaceState(null, '', address(hostId, workspaceId, runId) + win.location.hash);
      render();
      const state = [hostId, workspaceId, runId, ...(room?.runs ?? []).map(row => row.id + row.state + (row.pending_count ?? 0))].join(':');
      updateSlow ||= previousRunState !== state;
      previousRunState = state;
      if (updateSlow) slowReadAt = Date.now();
      const detailRequest = fetchRun(token, runId, readSection);
      if (runId && !trustedRun(runId)) await detailRequest;
      if (!current()) return;
      await Promise.all([
        detailRequest,
        updateSlow && records.loadUsage(readSection),
        updateSlow && briefing.load(readSection).then(() => github.controls()),
        (updateSlow || github.needsStatus()) && github.load(readSection),
        loadChat(readSection),
        get('requests').open ? loadRequests() : null,
      ]);
    } catch {
      if (!current()) return;
      online = false;
      roomOnline = false;
      const hostName = directory.find(item => item.id === hostId)?.name ?? (hostId === 'local' ? '이 컴퓨터' : hostId);
      const target = workspaces.find(item => item.id === workspaceId)?.name ?? workspaceId;
      roomLoadError = !workspaceId && runId ?
        `${hostName}에서 저장된 실행 ${runId}을 불러올 수 없습니다. 작업실 연결과 실행 기록을 확인하고 다시 시도하세요.` :
        `${hostName}${target ? ' · ' + target : ''}에 연결할 수 없습니다. 작업실이 실행 중인지 확인하고 다시 시도하세요.`;
      chatDetailsOnline = false;
      inboxDetailsOnline = false;
      render();
      if (get('requests').open) {
        requestControls();
        say('request-note', '연결이 끊겼습니다. 마지막으로 확인한 요청을 표시합니다.');
      }
    } finally {
      if (current()) poller.schedule(roomOnline && room && !activeRuns().length && !github.needsStatus() ? 20000 : 3000);
    }
  }

  function openDialog(dialog, focus) {
    if (!dialog.open) dialog.showModal();
    focus?.focus();
  }

  function closeDialogs() {
    for (const id of ['conversation', 'requests', 'room-settings', 'register-dialog', 'coordination-dialog', 'decision-revision-dialog']) {
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

  function engineOptions() {
    return Object.entries(engines).map(([id, name]) => {
      const probe = room.engines?.find(item => item.engine === id);
      const hint = probe?.probe_status === 'ok' ? ' · CLI 확인' : probe?.probe_status === 'missing' ? ' · 설치 필요' : '';
      return option(id, name + hint);
    });
  }

  function showChat(role, pinnedRunId = null) {
    if (!room) return;
    if (pinnedRunId && (selected()?.id !== pinnedRunId || !trustedRun(pinnedRunId))) return;
    receiptRunId = null;
    if (jump) {
      view = jump.view;
      jump = null;
      renderRoom();
    }
    if (role !== 'coordinator' && !pinnedRunId) {
      const run = visibleRun(role);
      if (run?.id !== runId && run?.id) {
        closeDialogs();
        resetThread();
        ++epoch;
        runId = run.id;
        detail = null;
        say('detail-status', '');
        say('stop-note', '');
        win.history.pushState(null, '', address(hostId, workspaceId, runId));
        refresh();
      }
    }
    ++dialogEpoch;
    chatRole = role;
    const current = selected();
    chatAttempt = role === 'coordinator' ? null : current?.attempts;
    chatState = null;
    const key = sessionKey(role);
    const saved = readRecovery(key)?.body;
    const editor = editors.get(key);
    const assignment = role === 'coordinator';
    records.portrait(role);
    const name = assignment ? '조정자' : (current?.assignment?.roles?.[role] ?? roomPerson(role)).name;
    say('chat-title', name + '와 대화');
    get('chat-portrait').dataset.person = role;
    say('chat-context', assignment ? room.workspace.name : current ? taskTitle(current.task) : '배정된 작업이 없습니다.');
    say('chat-label', assignment ? '새로 맡길 작업' : '이 작업에 추가할 지시');
    get('message').placeholder = assignment ? '예시 · 이 프로젝트의 테스트 실패 원인을 찾아줘.' : '예시 · Windows에서만 재현되는지 확인해줘.';
    get('assignment-options').hidden = !assignment;
    get('edit-option').hidden = !assignment;
    get('review-option').hidden = !assignment || !supportsMeetings();
    get('assignment-options').open = Boolean(saved?.delegation != null || saved?.indices || saved?.test_files || saved?.role_engines || saved?.goal_criteria || saved?.coordination || saved?.work_profile);
    get('assignment-engines').hidden = !assignment || !supportsMeetings();
    if (assignment && supportsMeetings()) {
      for (const role of Object.keys(roleLabels)) {
        const picker = get('assignment-' + role + '-engine');
        picker.replaceChildren(option('', '작업실 기본값 · ' + engineName(room.settings[role].engine)), ...engineOptions());
        picker.value = saved?.role_engines?.[role] ?? '';
      }
    }
    get('run-delegation').options[0].textContent = '작업실 기본값 · ' + (room.settings.delegation ? '조정자가 결정' : '나에게 질문');
    get('run-delegation').value = saved?.delegation == null ? '' : String(saved.delegation);
    get('allow-edits').checked = saved?.allow_edits ?? false;
    get('overnight-goal').checked = saved ? saved.goal === true : room.goal_default === true;
    get('goal-criteria').value = saved?.goal_criteria?.join('\n') ?? '';
    get('review-plan').checked = saved?.review_plan === true;
    get('test-files').value = saved?.test_files?.join('\n') ?? '';
    if (assignment) folderChoices(saved?.indices);
    if (assignment) profiles.intake(saved?.work_profile);
    if (assignment) briefing.intake.load(null, saved?.coordination, !get('allow-edits').checked);
    get('message').value = saved?.message ?? editor ?? '';
    say('chat-note', saved ? '이전 접수 결과를 확인하지 못했습니다. 같은 요청을 다시 보낼 수 있습니다.' : '');
    say('chat-request-note', '');
    get('chat-evidence').append(get('evidence-panel'));
    renderChatLog();
    chatControls();
    openDialog(get('conversation'), get('message').disabled ? get('chat-log') : get('message'));
    loadChat();
  }

  function message(owner, name, value, meta, key = name + '-' + (meta ?? '')) {
    const node = make('article', '', 'message');
    node.dataset.owner = owner;
    node.append(make('small', name));
    if (value) node.append(renderMarkdown(doc, value, {key: runId + '-' + key, source: view === 'technical'}));
    if (meta) node.append(make('small', meta));
    return node;
  }

  function decisionStatus(row) {
    return [decisionLabels[row.decision], requestStateLabels[row.status], stamp(row.delivered_at ?? row.expired_at ?? row.decided_at)]
      .filter(Boolean).join(' · ');
  }

  function sharedEvidence(record) {
    const evidence = make('details');
    evidence.dataset.evidence = 'shared:' + record.source_run + ':' + record.request_id + ':' + record.revision;
    evidence.append(make('summary', '원래 제품 결정 · 버전 ' + record.revision));
    const source = roomRuns().find(row => row.id === record.source_run);
    if (validId(record.source_run)) {
      const link = make('a', '출처 목표 · ' + (source ? taskTitle(source.task) : record.source_run));
      link.href = address(hostId, workspaceId, record.source_run);
      link.addEventListener('click', event => navClick(event, hostId, workspaceId, record.source_run));
      evidence.append(link);
    }
    evidence.append(make('p', record.prompt), make('small', '원본 요청 · ' + record.request_id));
    return evidence;
  }

  function decisionEvidence(row) {
    const delegated = row.delegation_details;
    if (!delegated?.evidence) return null;
    const info = make('details');
    info.dataset.evidence = row.run_id + ':' + row.id;
    const source = delegated.evidence;
    info.append(make('summary', '대리 결정 근거'),
      make('p', source.method === 'committed_fact' ? '기준 커밋의 파일에서 확인 · 모델 호출 0회' :
        '전문가 · ' + engineName(delegated.specialist_engine) + ' / 독립 검증자 · ' + engineName(delegated.verifier_engine)),
      make('p', source.path + ' ' + source.line + '행 · 기준 커밋 ' + source.revision), make('p', source.quote));
    return info;
  }

  function renderChatLog() {
    const composing = chatRole === 'coordinator' && !receiptRunId;
    const run = composing ? null : selected();
    renderViewTools('chat', run);
    renderThread('chat-log', chatRole, run);
    get('chat-evidence').hidden = composing;
    if (composing) get('chat-meeting').hidden = true;
    const waiting = threadRun === runId ? chatRows.filter(row => row.status === 'pending').length : 0;
    const planPending = detail?.meeting?.status === 'awaiting_review' && stoppable(detail.state);
    const count = waiting + (planPending ? 1 : 0);
    get('chat-pending').hidden = composing || count === 0;
    say('chat-pending', '응답할 요청 ' + count + '건');
  }

  function receiptRole(run) {
    return run.state === 'REVIEWING' ? 'reviewer' : 'developer';
  }

  get('receipt-owner').addEventListener('click', () => {
    const run = selected();
    if (receiptRunId && run?.id === receiptRunId) showChat(receiptRole(run), receiptRunId);
  });
  get('receipt-new').addEventListener('click', () => showChat('coordinator'));

  get('chat-pending').addEventListener('click', () => {
    const target = get('chat-log').querySelector('.request-item') ??
      get('meeting-decision').querySelector('input:not(:disabled), button:not(:disabled)') ?? get('decision-heading');
    target?.scrollIntoView?.({block: 'center'});
    target?.focus();
  });

  function fullAnswer(id, run, role, text) {
    const key = [hostId, workspaceId, run.id, role].join(':');
    let entry = answerDetails.get(id);
    if (entry?.key !== key) {
      const node = make('details', '', 'content-fold');
      node.dataset.disclosureKey = key + '-answer';
      node.append(make('summary', '답변 더 보기'), make('div'));
      entry = {node, key, text: '', rendered: null};
      entry.populate = () => {
        if (node.open && entry.rendered !== entry.text) {
          replaceContent(node.lastElementChild, [renderMarkdown(doc, entry.text, {key, source: true})]);
          entry.rendered = entry.text;
        }
      };
      node.addEventListener('toggle', entry.populate);
      answerDetails.set(id, entry);
    }
    entry.text = text;
    entry.populate();
    return entry.node;
  }

  function renderThread(id, role, run = selected()) {
    const simple = view === 'simple';
    const assignment = run?.assignment;
    const rows = threadRun === runId ? chatRows : [];
    const directions = threadRun === runId ? directionRows : [];
    const relevant = rows.filter(item => item.status === 'pending' || !role || role === 'coordinator' || item.role === role);
    update(id, [hostId, workspaceId, run, role, view, relevant, directions], node => {
      const previousFocus = node.contains(doc.activeElement) ? doc.activeElement : null;
      const messages = [], events = [];
      if (!run) {
        replaceContent(node, [message('agent', '조정자', '어떤 작업을 맡길까요?')]);
        return;
      }
      messages.push(message('user', '나', run.task, stamp(run.created_at)));
      if (assignment?.note && (!simple || role === 'coordinator' && activeStates.has(run.state))) {
        messages.push(message('agent', '조정자', assignment.note));
      }
      for (const item of relevant) {
        const label = requestLabels[item.kind];
        if (item.status === 'pending') {
          const request = requestItem(item, epoch, id === 'chat-log' ? 'chat' : 'thread');
          request.dataset.receipt = item.id; request.dataset.section = 'requests';
          events.push([item.created_at, request]);
          continue;
        }
        if (item.shared_decision) {
          const receipt = message('agent', '선행 목표의 사용자 결정 적용', item.shared_decision.answer,
            decisionStatus(item), item.id + '-shared');
          receipt.dataset.receipt = item.id; receipt.dataset.section = 'requests';
          receipt.append(sharedEvidence(item.shared_decision));
          events.push([item.decided_at || item.created_at, receipt]);
          continue;
        }
        if (simple && item.source === 'delegate') continue;
        const request = message('agent', label ?? '요청', item.prompt, stamp(item.created_at), item.id);
        request.dataset.receipt = item.id; request.dataset.section = 'requests';
        events.push([item.created_at, request]);
        if (item.decision) {
          const delegated = item.source === 'delegate';
          const receipt = message(delegated ? 'agent' : 'user', delegated ? '조정자 대리 결정' : '나', item.answer, decisionStatus(item), item.id + '-answer');
          receipt.dataset.receipt = item.id; receipt.dataset.section = 'requests';
          const evidence = decisionEvidence(item);
          if (evidence) receipt.append(evidence);
          events.push([item.decided_at, receipt]);
        } else if (item.status === 'expired') {
          events.push([item.expired_at, message('agent', label, '', decisionStatus(item))]);
        }
      }
      for (const item of directions) {
        const direction = message('user', '나 → ' + (roleLabels[item.role] ?? '에이전트'), item.message,
          [deliveryLabels[item.delivery] ?? '전달 상태 확인 필요', item.attempt && item.attempt + '번째 시도', stamp(item.created_at)].filter(Boolean).join(' · '), item.id);
        direction.dataset.receipt = item.id; direction.dataset.section = 'directions';
        events.push([item.created_at, direction]);
      }
      events.sort((a, b) => (Date.parse(a[0]) || 0) - (Date.parse(b[0]) || 0));
      messages.push(...events.map(([, event]) => event));

      const failed = ['FAILED', 'RECOVERY_REQUIRED'].includes(run.state);
      const reviewStage = run.state === 'REVIEWING' || ['CHANGES_REQUESTED', 'REVIEW_FAILED', 'REVIEW_UNDECIDED'].includes(run.outcome);
      const interrupted = run.state === 'RECOVERY_REQUIRED';
      const ownerRole = run.kind === 'parallel' || interrupted || failed && (run.diagnostic || run.meeting?.error || run.outcome === 'TEST_FAILED') ? 'coordinator' :
        reviewStage || role === 'reviewer' && !activeStates.has(run.state) ? 'reviewer' : 'developer';
      const name = assignment?.roles?.[ownerRole]?.name ?? meetingRoles[ownerRole];
      const phase = run.meeting?.status;
      const state = ['planning', 'awaiting_review'].includes(phase) ? meetingStates[phase] : runLabel(run);
      const status = message('agent', name + ' · ' + state);
      const testFailure = run.outcome === 'TEST_FAILED' && run.test_output?.split(/\r?\n/).find(line => /^FAILED |^ERROR |^E\s{3}\S/.test(line));
      const interruption = interrupted ? (run.review_output || run.test_output || '').split(/\r?\n/).find(line => /timed?\s*out|timeout|시간\s*(?:초과|제한)|중단/i.test(line)) || outcomes.INTERRUPTED : '';
      const explanation = failed ? run.diagnostic || run.meeting?.error || interruption || testFailure || (run.outcome === 'TEST_FAILED' ? run.test_output :
        ownerRole === 'reviewer' ? run.review_output : run.developer_output) :
        activeStates.has(run.state) ? run.lines?.[ownerRole] : ownerRole === 'reviewer' ? run.review_output : run.developer_output;
      const prose = ownerRole === 'reviewer' ? explanation?.replace(/^\s*VERDICT: (?:CHANGES_REQUESTED|APPROVED)\s*$/gm, '') : explanation;
      const body = simple ? firstProseParagraph(prose ?? '') : explanation;
      if (body) status.append(renderMarkdown(doc, body, {key: run.id + '-conclusion'}));
      else if (failed && prose?.trim()) status.append(make('p', String(testFailure || prose).trim().split(/\r?\n/)[0].slice(0, 320)));
      else if (run.kind === 'parallel' && failed) status.append(make('p', '하위 작업의 실행 상태를 확인해 주세요.'));
      else if (failed || !explanation && !activeStates.has(run.state) && run.outcome !== 'APPROVED') {
        status.append(make('p', outcomes[run.outcome] ?? (failed ? '실패 원인 미확인' : '결과 설명 미확인')));
      }
      const facts = resultFacts(run).filter(fact => {
        if (!activeStates.has(run.state)) return true;
        if (fact.startsWith('테스트 ')) return ['TESTING', 'REVIEWING'].includes(run.state) || !!run.test_output;
        if (fact.startsWith('검토 ')) return run.state === 'REVIEWING' || !!run.review_output;
        if (fact.startsWith('변경 파일 ')) return !!run.changes;
        return !fact.startsWith('완료 기준 ');
      });
      if (facts.length) {
        const list = make('ul', '', 'result-facts');
        for (const fact of facts) list.append(make('li', fact));
        status.append(list);
      }
      if (simple && explanation?.trim() && explanation.trim() !== body?.trim()) {
        status.append(fullAnswer(id, run, ownerRole, explanation));
      }
      messages.push(status);
      if (!simple && view === 'collaboration' && role && role !== 'coordinator' && role !== ownerRole && run.lines?.[role]) {
        messages.push(message('agent', assignment?.roles?.[role]?.name ?? roleLabels[role], run.lines[role]));
      }
      const top = node.scrollTop;
      const pinned = node.scrollHeight - top - node.clientHeight < 25;
      const focus = replaceRequests(node, messages);
      if (previousFocus?.isConnected && doc.activeElement !== previousFocus) previousFocus.focus({preventScroll: true});
      node.scrollTop = id === 'chat-log' && pinned && !focus ? node.scrollHeight : top;
    });
  }

  function chatControls() {
    if (!chatRole) return;
    const role = chatRole;
    const assignment = role === 'coordinator';
    const recoverable = Boolean(readRecovery(sessionKey(role)));
    const run = selected();
    const receipt = assignment && !!receiptRunId && run?.id === receiptRunId;
    get('assignment-receipt').hidden = !receipt;
    if (receipt) {
      const owner = receiptRole(run);
      const name = run.assignment?.roles?.[owner]?.name ?? roomPerson(owner).name;
      get('receipt-portrait').dataset.person = owner;
      say('receipt-task', taskTitle(run.task));
      const phase = run.meeting?.status ?? run.meeting_status;
      const stage = activeStates.has(run.state) && phase === 'planning' ? '작업 계획을 검토하고 있습니다.' :
        activeStates.has(run.state) && phase === 'awaiting_review' ? '계획에 대한 사용자 결정을 기다리고 있습니다.' : runLabel(run);
      say('receipt-status', name + ' · ' + stage);
    }
    const interactive = run && run.assignment && run.use_inbox === true;
    // Opening the receipt before the first attempt starts must not require reopening the chat.
    if (!assignment && chatAttempt === 0 && run?.attempts === 1) chatAttempt = 1;
    const staleAttempt = interactive && run.attempts !== chatAttempt;
    const open = assignment ? !receiptRunId : interactive && !staleAttempt && chatAttempt > 0 && (run.state === followupStates[role] || recoverable);
    const locked = !!chatState || recoverable;
    const enabled = canWrite() && (assignment || directionsOnline && chatDetailsOnline);
    get('message').disabled = !open || !enabled;
    get('message').readOnly = locked;
    get('run-delegation').disabled = locked;
    get('allow-edits').disabled = locked;
    get('review-plan').disabled = locked;
    for (const picker of get('assignment-engines').querySelectorAll('select')) picker.disabled = locked;
    const editAssignment = assignment && get('allow-edits').checked;
    const coordinate = get('assignment-coordination');
    coordinate.hidden = !assignment || room?.briefing_supported !== true;
    coordinate.disabled = locked || !enabled || coordinate.hidden;
    if (!coordinate.hidden) briefing.intake.mode(!editAssignment);
    const canAssignGoal = editAssignment && room?.goal_supported === true;
    profiles.intakeControls(assignment, locked || !enabled, canAssignGoal && get('overnight-goal').checked);
    get('goal-option').hidden = !canAssignGoal;
    get('overnight-goal').disabled = locked || !canAssignGoal;
    get('goal-option-help').hidden = !canAssignGoal || !get('overnight-goal').checked;
    get('assignment-goal-criteria').hidden = get('goal-option-help').hidden;
    get('goal-criteria').disabled = locked || get('assignment-goal-criteria').hidden;
    get('assignment-test-scope').hidden = !editAssignment;
    get('test-files').disabled = locked || !editAssignment;
    for (const box of get('assignment-folders').querySelectorAll('input')) box.disabled = locked;
    get('send').hidden = !open;
    get('chat-form').hidden = !open;
    get('send').disabled = !enabled || !!chatState;
    say('send', recoverable && !chatState ? '같은 요청 다시 보내기' : assignment ? '작업 맡기기' : '추가 지시 보내기');
    if (!open && !assignment && !chatState) {
      let reason = '진행 중인 작업에서 추가 지시를 보낼 수 있습니다.';
      if (staleAttempt) reason = '실행 시도가 바뀌었습니다. 현재 시도에 추가 지시를 보내려면 대화를 다시 여세요.';
      else if (run && !interactive) reason = '추가 지시는 작업실에서 새로 맡긴 작업에 보낼 수 있습니다.';
      else if (run && activeStates.has(run.state)) reason = '이 에이전트의 실행 단계가 시작되면 추가 지시를 보낼 수 있습니다.';
      say('chat-note', reason);
    } else if (open && !assignment && !chatState && get('chat-note').dataset.waiting === 'true') {
      say('chat-note', '');
    }
    get('chat-note').dataset.waiting = String(!open && !assignment && !chatState);
    requestControls();
  }

  async function loadChat(readSection = api) {
    const id = runId, context = epoch;
    const read = ++chatRead;
    if (!trustedRun(id) || selected()?.kind === 'parallel') return;
    const prefix = runPath(id);
    const [inbox, history] = await Promise.allSettled([readSection(prefix + '/inbox'), readSection(prefix + '/directions')]);
    if (!sameContext(context) || id !== runId || read !== chatRead) return;
    if (threadRun !== id) {
      chatRows = [];
      directionRows = [];
    }
    threadRun = id;
    chatDetailsOnline = inbox.status === 'fulfilled' && Array.isArray(inbox.value.requests);
    directionsOnline = history.status === 'fulfilled' && Array.isArray(history.value.directions);
    if (chatDetailsOnline) chatRows = inbox.value.requests.filter(row => row.run_id === id);
    if (directionsOnline) directionRows = history.value.directions.filter(row => !row.run_id || row.run_id === id);
    const missing = [];
    if (!chatDetailsOnline) missing.push('질문과 권한 요청');
    if (!directionsOnline) missing.push('추가 지시');
    const warning = missing.length ? missing.join(', ') + '의 최신 기록을 확인할 수 없습니다.' : '';
    say('thread-read-note', warning);
    if (get('conversation').open && chatRole) {
      if (warning) say('chat-request-note', warning);
      else if (get('chat-request-note').textContent.endsWith('최신 기록을 확인할 수 없습니다.')) say('chat-request-note', '');
      const key = sessionKey(chatRole);
      const saved = readRecovery(key)?.body;
      if (directionsOnline && saved && chatRole !== 'coordinator' && directionRows.some(item =>
        item.id === saved.id && item.attempt === saved.attempt && item.role === saved.role)) {
        saveRecovery(key, null);
        editors.delete(key);
        get('message').value = '';
        say('chat-note', '추가 지시의 접수를 확인했습니다.');
      }
      renderChatLog();
    }
    renderRoom();
    chatControls();
    requestControls();
  }

  function chatBody(role) {
    const value = get('message').value.trim();
    const limit = role === 'coordinator' ? 8192 : 4096;
    if (!value || codeBytes(value) > limit) throw new Error('내용을 UTF-8 기준 1~' + limit.toLocaleString('ko-KR') + '바이트로 입력하세요.');
    const id = win.crypto.randomUUID().replaceAll('-', '');
    if (role !== 'coordinator') return {id, role, attempt: chatAttempt, message: value};
    const mode = get('run-delegation').value;
    const body = {id, message: value, allow_edits: get('allow-edits').checked, delegation: mode === '' ? null : mode === 'true'};
    const workProfile = profiles.value();
    if (Object.keys(workProfile).length) body.work_profile = workProfile;
    if (body.allow_edits && room?.goal_supported === true && get('overnight-goal').checked) {
      body.goal = true;
      const criteria = get('goal-criteria').value.split('\n').map(value=>value.trim()).filter(Boolean);
      if (criteria.length > 20 || criteria.some(value=>codeBytes(value)>8192)) {
        throw new Error('완료 기준은 한 줄당 8,192바이트 이내로 최대 20개 입력하세요.');
      }
      if (criteria.length) body.goal_criteria = criteria;
    }
    if (supportsMeetings()) {
      if (get('review-plan').checked) {
        body.meeting = body.review_plan = true;
      } else {
        body.meeting = room.quick_dispatch_supported ? 'auto' : !profiles.quick(workProfile.mode, body.allow_edits);
      }
      const overrides = {};
      for (const role of Object.keys(roleLabels)) {
        const engine = get('assignment-' + role + '-engine').value;
        if (engine) overrides[role] = engine;
      }
      if (Object.keys(overrides).length) body.role_engines = overrides;
    }
    const indices = Array.from(get('assignment-folders').querySelectorAll('input:checked'), input => Number(input.value));
    if (indices.length) body.indices = indices;
    if (body.allow_edits) {
      const files = get('test-files').value.split('\n').map(path => path.trim()).filter(Boolean);
      if (files.length) body.test_files = files;
    }
    if (room?.briefing_supported) {
      const values = briefing.intake.value();
      if (values.priority || values.dependencies.length || values.areas.length) body.coordination = values;
    }
    return body;
  }

  get('allow-edits').addEventListener('change', chatControls);
  get('overnight-goal').addEventListener('change', chatControls);
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
        resetThread();
        runId = entry.id;
        receiptRunId = entry.id;
        detail = entry;
        say('stop-note', '');
        win.history.pushState(null, '', address(hostId, workspaceId, runId));
        say('chat-note', '작업을 배정했습니다.');
        renderChatLog();
        chatControls();
        get('receipt-owner').focus();
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
    receiptRunId = null;
    get('room-evidence').append(get('evidence-panel'));
    if (jump?.place === 'chat') {
      view = jump.view;
      jump = null;
    }
    renderRoom();
  });
  for (const role of ['coordinator', ...Object.keys(roleLabels)]) seat(role).addEventListener('click', () => showChat(role));

  function requestControls() {
    for (const [id, available] of [['office-thread', chatDetailsOnline], ['chat-log', chatDetailsOnline], ['request-log', inboxDetailsOnline]]) {
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
    replaceContent(root, rows);
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

  function replaceContent(root, children) {
    const folds = new Map([...root.querySelectorAll('[data-disclosure-key]')].map(node => [node.dataset.disclosureKey, node.open]));
    const wraps = new Set([...root.querySelectorAll('[data-wrap-key][aria-pressed="true"]')].map(node => node.dataset.wrapKey));
    const previous = doc.activeElement;
    const focusKey = root.contains(previous) && previous.closest('[data-disclosure-key]')?.dataset.disclosureKey;
    const control = focusKey && previous.tagName === 'BUTTON' ? previous.textContent : null;
    const sourceKey = root.contains(previous) && previous.closest('.content-source')?.querySelector('[data-wrap-key]')?.dataset.wrapKey;
    const scroll = new Map([...root.querySelectorAll('.content-source')].map(box => {
      const pre = box.querySelector('pre');
      return [box.querySelector('[data-wrap-key]').dataset.wrapKey, [pre.scrollLeft, pre.scrollTop]];
    }));
    root.replaceChildren(...children);
    for (const node of root.querySelectorAll('[data-disclosure-key]')) {
      if (folds.has(node.dataset.disclosureKey)) node.open = folds.get(node.dataset.disclosureKey);
    }
    for (const node of root.querySelectorAll('[data-wrap-key]')) {
      if (wraps.has(node.dataset.wrapKey) && node.getAttribute('aria-pressed') === 'false') node.click();
      const box = node.closest('.content-source');
      const position = scroll.get(node.dataset.wrapKey);
      if (position) {
        const pre = box.querySelector('pre');
        [pre.scrollLeft, pre.scrollTop] = position;
      }
      if (sourceKey === node.dataset.wrapKey && previous.tagName === 'BUTTON') {
        [...box.querySelectorAll('button')].find(button => button.textContent === previous.textContent)?.focus({preventScroll: true});
      }
    }
    if (focusKey) {
      const fold = [...root.querySelectorAll('[data-disclosure-key]')].find(node => node.dataset.disclosureKey === focusKey);
      const next = control ? [...(fold?.querySelectorAll('button') ?? [])].find(node => node.textContent === control) : fold?.querySelector('summary');
      next?.focus({preventScroll: true});
    }
  }

  function requestItem(row, context, view = 'inbox') {
    const key = row.run_id + ':' + row.id;
    const draftKey = ['request', hostId, workspaceId, key].join(':');
    const title = requestLabels[row.kind] ?? '요청 종류 확인 필요';
    const node = make('article', '', 'request-item');
    node.dataset.request = key;
    node.tabIndex = -1;
    const run = roomRuns().find(item => item.id === row.run_id);
    const meta = [roleLabels[row.role], run && taskTitle(run.task), stamp(row.created_at)].filter(Boolean);
    node.append(make('h3', title), make('small', meta.join(' · ')), make(row.kind === 'permission' ? 'pre' : 'p', row.prompt));
    if (row.kind === 'permission') node.append(make('small', '승인 범위 · 이 작업의 이번 요청 1회'));
    if (row.delegation_reason) node.append(make('small', row.delegation_reason));
    else if (row.delegation_in_progress) node.append(make('small', '전문가가 근거를 확인하고 있습니다.'));
    if (row.status !== 'pending') {
      const owner = row.shared_decision ? '선행 목표의 사용자 결정 적용' : row.source === 'delegate' ? '조정자 대리 결정' : '사용자 결정';
      node.append(make('small', [row.decision && owner, decisionStatus(row)].filter(Boolean).join(' · ')));
      if (row.answer) node.append(make('p', row.answer));
      if (row.shared_decision) node.append(sharedEvidence(row.shared_decision));
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
      textarea.dataset.draftKey = draftKey;
      textarea.value = editors.get(draftKey) ?? '';
      textarea.addEventListener('input', () => {
        editors.set(draftKey, textarea.value);
        for (const other of doc.querySelectorAll('.request-item textarea[data-control="draft"]')) {
          if (other !== textarea && other.dataset.draftKey === draftKey) other.value = textarea.value;
        }
      });
      const label = make('label', '답변');
      label.htmlFor = textarea.id;
      const send = make('button', '답변 보내기', 'primary');
      send.type = 'button';
      send.dataset.control = 'answer';
      send.addEventListener('click', () => submitDecision(row, 'answer', textarea.value, context, view));
      node.append(label, textarea, send);
    } else if (view !== 'inbox') {
      const button = make('button', '요청함에서 확인', 'secondary');
      button.type = 'button';
      button.dataset.control = 'inbox';
      button.addEventListener('click', () => {
        if (get('conversation').open) get('conversation').close();
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

  function showRequests(focusKey, focusRun) {
    const token = ++inboxEpoch;
    inboxFocus = focusKey || focusRun ? {key: focusKey, run: focusRun} : null;
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
      const waiting = room.runs.filter(row => row.meeting_status === 'awaiting_review');
      inboxDetailsOnline = true;
      update('request-log', [rows, waiting.map(row => [row.id, row.task])], node => {
        const gates = waiting.map(row => {
          const item = make('article', '', 'request-item');
          const open = make('button', '기획 회의 열기', 'secondary');
          open.type = 'button';
          open.addEventListener('click', async () => {
            if (!sameContext(context) || !trustedRun(row.id)) return;
            get('requests').close();
            if (runId !== row.id) await navigation(hostId, workspaceId, row.id);
            if (!closed && runId === row.id && detail?.meeting) openEvidence('room', 'meeting');
          });
          item.append(make('h3', '계획 확인'), make('p', row.task), open);
          return item;
        });
        replaceRequests(node, [...gates, ...rows.map(row => requestItem(row, context))]);
        if (!rows.length && !gates.length) node.append(make('p', '응답할 요청이 없습니다.', 'field-help'));
      });
      if (inboxFocus) {
        const target = [...get('request-log').children].find(item => inboxFocus.key ?
          item.dataset.request === inboxFocus.key : item.dataset.request?.startsWith(inboxFocus.run + ':') && item.querySelector('[data-control]'));
        if (target) target.focus();
        else say('request-note', '선택한 요청의 최신 상태를 확인해 주세요.');
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
    const draftKey = ['request', hostId, workspaceId, key].join(':');
    const chat = view === 'chat';
    const thread = view === 'thread';
    const viewEpoch = () => thread ? epoch : chat ? dialogEpoch : inboxEpoch;
    const note = thread ? 'thread-note' : chat ? 'chat-request-note' : 'request-note';
    const token = viewEpoch();
    const active = () => sameContext(context) && token === viewEpoch() &&
      (thread ? runId === row.run_id : get(chat ? 'conversation' : 'requests').open);
    // Dismissing the conversation after pressing Send still submits the explicit answer
    // to that same run. Permission decisions must retain their original review surface.
    const sameAnswer = () => action === 'answer' && sameContext(context) && runId === row.run_id;
    const feedback = value => {
      if (active()) say(note, value);
      else if (sameAnswer()) say('thread-note', value);
    };
    if (!active() || !canWrite() || !trustedRun(row.run_id) || !(thread || chat ? chatDetailsOnline : inboxDetailsOnline) || deciding.has(key)) return;
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
      if ((!active() && !sameAnswer()) || !canWrite() || !trustedRun(row.run_id)) return;
      const current = requests.find(item => item.id === row.id && item.run_id === row.run_id);
      if (!current || ['kind', 'role', 'engine', 'prompt'].some(field => current[field] !== row[field])) {
        feedback('요청이 변경되었습니다. 갱신된 요청을 확인하세요.');
        return;
      }
      if (current.status !== 'pending') {
        feedback('이미 처리된 요청입니다. 최신 기록을 확인하세요.');
        return;
      }
      const {request: receipt} = await api(path + '/' + row.id + '/decision', body);
      if (receipt?.id !== row.id || receipt.run_id !== row.run_id || receipt.status === 'pending') throw new Error();
      editors.delete(draftKey);
      feedback('결정을 기록했습니다.');
    } catch (error) {
      feedback(error.status === 400 ? error.message :
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
      picker.replaceChildren(...engineOptions());
      picker.value = profile.engine;
    }
    renderSkills(Object.fromEntries(Object.keys(roleLabels).map(role => [role, room.settings[role].skills])));
    say('settings-note', '');
    openDialog(get('room-settings'), get('room-delegation'));
    goals.settings();
    profiles.settings();
    github.settings();
    storage.settings();
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

  let registrationHost, suggestedName = '', nameEdited = false;
  function canPickFolders() {
    return hostId === 'local' && typeof win.officeFolders?.select === 'function';
  }
  const pathsField = get('workspace-paths'), nameField = get('workspace-name'), picker = get('pick-folders');
  const registeredPaths = () => pathsField.value.split(/\r?\n/).map(path => path.trim()).filter(Boolean);
  function suggestName() {
    const paths = registeredPaths();
    suggestedName = paths[0]?.split(/[\\/]/).filter(Boolean).at(-1) ?? '';
    if (!nameEdited) nameField.value = suggestedName;
    picker.textContent = paths.length ? '폴더 추가' : '폴더 선택';
  }

  function showRegister() {
    if (!online || registrationBusy) return;
    if (registrationHost !== hostId) {
      registrationHost = hostId;
      get('register-form').reset();
      nameEdited = false;
    }
    const native = canPickFolders();
    picker.hidden = !native;
    const hostName = directory.find(item => item.id === hostId)?.name ?? hostId;
    say('register-host', '등록할 실행 호스트 · ' + hostName);
    say('paths-help', native ? '선택한 폴더를 확인하거나 경로를 한 줄씩 입력하세요.' :
      hostName + '에서 사용할 폴더의 절대 경로를 한 줄씩 입력하세요.');
    suggestName();
    say('register-note', '');
    openDialog(get('register-dialog'), native ? picker : pathsField);
  }

  nameField.addEventListener('input', () => { nameEdited = nameField.value !== suggestedName; });
  pathsField.addEventListener('input', suggestName);
  async function pickFolders() {
    if (!online || picker.hidden || registrationBusy) return;
    const token = epoch;
    const current = () => sameContext(token) && get('register-dialog').open;
    try {
      const paths = await win.officeFolders.select();
      if (!current() || !paths.length) return;
      if (paths.some(path => /[\r\n]/.test(path))) throw new Error('줄바꿈이 없는 폴더 이름을 선택하세요.');
      pathsField.value = [...new Set([...registeredPaths(), ...paths])].join('\n');
      suggestName();
      say('register-note', '');
    } catch (error) {
      if (current()) say('register-note', error.message || '폴더를 선택할 수 없습니다. 경로를 직접 입력하세요.');
    }
  }
  picker.addEventListener('click', pickFolders);
  get('empty-retry').addEventListener('click', refresh);
  get('register').addEventListener('click', showRegister);
  get('empty-register').addEventListener('click', () => {
    showRegister();
    if (canPickFolders() && get('register-dialog').open) pickFolders();
  });
  get('register-form').addEventListener('submit', async event => {
    event.preventDefault();
    if (!online || registrationBusy) return;
    const token = epoch;
    const body = {name: nameField.value.trim(), paths: registeredPaths()};
    if (!body.name || !body.paths.length) return say('register-note', '이름과 폴더 경로를 입력하세요.');
    registrationBusy = get('save-workspace').disabled = true;
    try {
      const {workspace} = await api('/workspaces', body);
      if (!sameContext(token)) return;
      get('register-form').reset();
      nameEdited = false;
      registrationBusy = false;
      navigation(hostId, workspace.id, null);
    } catch (error) {
      if (sameContext(token)) say('register-note', error.status < 500 ? error.message : '등록 여부를 확인할 수 없습니다.');
    } finally {
      registrationBusy = get('save-workspace').disabled = false;
    }
  });

  get('host').addEventListener('change', event => navigation(event.target.value, null, null));
  get('mobile-navigation').addEventListener('click', event => {
    const button = event.currentTarget, expanded = button.getAttribute('aria-expanded') !== 'true';
    button.setAttribute('aria-expanded', String(expanded));
    button.textContent = expanded ? '작업공간·기록 접기' : '작업공간·기록 열기';
  });
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
    poller.dispose();
    progress.dispose();
    win.removeEventListener('popstate', restoreAddress);
    win.removeEventListener('online', refresh);
  };
}

if (typeof document !== 'undefined') mountWorkspaceOffice();
