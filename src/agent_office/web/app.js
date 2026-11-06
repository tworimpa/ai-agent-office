import {emptyEvents, mergeEvents, stateLabel, stateGroup} from './state.js';

const $ = id => document.getElementById(id);
const validId = id => typeof id === 'string' && /^[a-f0-9]{32}$/.test(id);
const initial = new URL(location.href).searchParams.get('task');
let selected = validId(initial) ? initial : null;
let tasks = [], journal = emptyEvents(), generation = 0;
let busy = false, queued = false, timer = null, controller = null, lastSuccess = null;
let eventRenderKey = '', metaLoaded = false;
let lastAnnouncedState = null, hadError = false;
const taskNodes = new Map();
const timeFormat = new Intl.DateTimeFormat('ko-KR', {dateStyle:'short', timeStyle:'medium'});
const timeLabel = value => { const date = new Date(value); return Number.isNaN(date.valueOf()) ? '시각 미상' : timeFormat.format(date); };
const setText = (id, value) => { const node = $(id), text = String(value ?? '—'); if (node.textContent !== text) node.textContent = text; };
const textNode = (tag, className, text='') => {const node = document.createElement(tag); node.className = className; node.textContent = text; return node;};

function rememberSelection() {
  const url = new URL(location.href);
  if (selected) url.searchParams.set('task', selected); else url.searchParams.delete('task');
  history.replaceState(null, '', url);
}

function announce(text) { if ($('announcer').textContent !== text) setText('announcer', text); }

function selectTask(id) {
  if (selected === id) return;
  selected = id; generation += 1; journal = emptyEvents(); eventRenderKey = '';
  controller?.abort(); rememberSelection();
  $('detail-content').hidden = true; $('detail-badge').hidden = true;
  $('detail-empty').hidden = false; setText('detail-empty', '작업 상세를 불러옵니다.');
  $('timeline').replaceChildren(); setText('event-count', '0개');
  $('event-limit').hidden = true;
  $('event-empty').hidden = false; setText('event-empty', '실행 기록을 불러옵니다.');
  renderTasks(); requestRefresh();
}

function renderTasks() {
  const previousFocus = document.activeElement;
  const query = $('search').value.trim().toLocaleLowerCase();
  const group = $('filter').value;
  const visible = tasks.filter(task => (group === 'all' || stateGroup(task.state) === group) &&
    [task.request_snapshot, task.project, task.task_id, task.developer_engine, task.reviewer_engine].some(value => String(value ?? '').toLocaleLowerCase().includes(query)));
  const wanted = new Set(visible.map(task => task.task_id));
  for (const [id, node] of taskNodes) {
    if (!wanted.has(id)) {
      const focused = node.contains(document.activeElement);
      node.remove(); taskNodes.delete(id);
      if (focused) $('tasks-heading').focus({preventScroll:true});
    }
  }
  for (let index=0; index<visible.length; index++) {
    const task = visible[index];
    let li = taskNodes.get(task.task_id);
    if (!li) {
      li = document.createElement('li');
      const button = textNode('button', 'task-button'); button.type = 'button'; button.dataset.taskId = task.task_id;
      const top = textNode('span', 'task-top'); top.append(textNode('span', 'badge'), textNode('span', 'mono muted'));
      button.append(top, textNode('p', 'task-title'), textNode('span', 'task-meta'));
      button.addEventListener('click', () => selectTask(task.task_id));
      li.append(button); taskNodes.set(task.task_id, li);
    }
    const button = li.firstElementChild;
    button.setAttribute('aria-pressed', String(task.task_id === selected));
    const badge = button.querySelector('.badge'); badge.textContent = stateLabel(task.state); badge.dataset.group = stateGroup(task.state);
    button.querySelector('.mono').textContent = task.task_id.slice(0,8);
    button.querySelector('.task-title').textContent = (task.request_snapshot || '요청 요약 없음').slice(0,120);
    button.querySelector('.task-meta').textContent = `${task.developer_engine} · ${task.attempts}/${task.max_attempts}회 · ${timeLabel(task.updated_at)}`;
    const atPosition = $('task-list').children[index];
    if (atPosition !== li) $('task-list').insertBefore(li, atPosition ?? null);
  }
  if (previousFocus?.dataset.taskId && previousFocus.isConnected && document.activeElement !== previousFocus) {
    previousFocus.focus({preventScroll:true});
  }
  $('list-empty').hidden = visible.length > 0;
  setText('list-empty', tasks.length ? '검색 조건에 맞는 작업이 없습니다.' : '아직 저장된 작업이 없습니다. 작업이 기록되면 이곳에 나타납니다.');
  setText('list-count', `${visible.length}건`);
  setText('count-total', tasks.length);
  for (const group of ['active','attention','failed']) setText(`count-${group}`, tasks.filter(task => stateGroup(task.state) === group).length);
}

function renderDetail(task) {
  $('detail-empty').hidden = true; $('detail-content').hidden = false; $('detail-badge').hidden = false;
  setText('detail-badge', stateLabel(task.state)); $('detail-badge').dataset.group = stateGroup(task.state);
  for (const [id, value] of Object.entries({request:task.request_snapshot || '요청 요약 없음', developer:task.developer_engine,
    reviewer:task.reviewer_engine, attempts:`${task.attempts} / ${task.max_attempts}회`, outcome:task.outcome,
    'task-id':task.task_id, 'run-id':task.current_run_id, project:task.project, workspace:task.workspace,
    branch:task.branch, created:timeLabel(task.created_at), updated:timeLabel(task.updated_at)})) setText(id, value);
  $('plan-note').hidden = task.state !== 'PLAN_ONLY';
  $('recovery-note').hidden = task.state !== 'RECOVERY_REQUIRED';
}

const eventTitles = {RUN_CREATED:'작업 접수', WORKSPACE_READY:'작업 공간 준비', STAGE_STARTED:'단계 시작',
  STAGE_COMPLETED:'단계 종료', STAGE_FAILED:'단계 실패', RETRY_SCHEDULED:'재시도 예정', RUN_FINISHED:'실행 종료', RECOVERY_REQUIRED:'중단 상태 확인'};
const phaseTitles = {developer:'개발', test:'테스트', review:'리뷰', run:'실행', workspace:'작업 공간'};

function renderEvents() {
  const key = `${journal.runId}:${journal.cursor}:${journal.events.length}`;
  if (eventRenderKey === key) return;
  eventRenderKey = key;
  const fragment = document.createDocumentFragment();
  for (const event of journal.events) {
    const payload = event.payload || {};
    const li = document.createElement('li');
    li.append(textNode('p', 'event-title', `${phaseTitles[payload.phase] ?? '실행'} · ${eventTitles[event.event_type] ?? event.event_type}`));
    const bits = [`#${event.seq}`];
    if (payload.engine) bits.push(payload.engine);
    if (payload.attempt) bits.push(`${payload.attempt}번째 시도`);
    if (payload.state) bits.push(stateLabel(payload.state));
    if (payload.returncode != null) bits.push(`종료 코드 ${payload.returncode}`);
    li.append(textNode('p', 'event-meta', bits.join(' · ')));
    const timestamp = textNode('time', '', timeLabel(event.timestamp)); timestamp.dateTime = event.timestamp; li.append(timestamp);
    fragment.append(li);
  }
  $('timeline').replaceChildren(fragment);
  setText('event-count', `${journal.events.length}개`);
  $('event-empty').hidden = journal.events.length > 0;
  setText('event-empty', '아직 등록된 실행 기록이 없습니다.');
  $('event-limit').hidden = journal.cursor <= journal.events.length;
}

async function getJSON(path) {
  const request = new AbortController(); controller = request;
  const timeout = setTimeout(() => request.abort(), 10000);
  try {
    const response = await fetch(path, {signal:request.signal, cache:'no-store', credentials:'omit', redirect:'error'});
    const data = await response.json();
    if (!response.ok) {const error = new Error('Request failed'); error.code = data.error?.code; throw error;}
    return data;
  } finally {
    clearTimeout(timeout); if (controller === request) controller = null;
  }
}

function requestRefresh() {
  clearTimeout(timer);
  if (document.hidden) return;
  if (busy) {queued = true; return;}
  void poll();
}

async function poll() {
  busy = true; queued = false;
  const token = generation;
  const current = () => token === generation && !document.hidden;
  let stateKey = null, stateNotice = '';
  try {
    if (!metaLoaded) {
      const meta = await getJSON('/api/meta'); if (!current()) return;
      setText('database', `연결된 기록 · ${meta.database}`); metaLoaded = true;
    }
    const result = await getJSON('/api/tasks'); if (!current()) return;
    tasks = result.tasks;
    if (!selected && tasks.length) {selected = tasks[0].task_id; rememberSelection();}
    renderTasks();
    if (selected) {
      const detail = await getJSON(`/api/tasks/${encodeURIComponent(selected)}`); if (!current()) return;
      const task = detail.task;
      if (task.task_id !== selected) throw new Error('Mismatched task');
      if (journal.runId !== task.current_run_id) {
        journal = emptyEvents(); eventRenderKey = '';
        $('timeline').replaceChildren(); setText('event-count', '0개'); $('event-limit').hidden = true;
        $('event-empty').hidden = false; setText('event-empty', '이번 실행의 기록을 불러옵니다.');
      }
      renderDetail(task);
      stateKey = `${task.task_id}:${task.current_run_id}:${task.state}:${task.outcome}`;
      stateNotice = `선택한 작업: ${stateLabel(task.state)}`;
      for (let page=0; page<10; page++) {
        const params = new URLSearchParams({run_id:task.current_run_id, after_seq:String(journal.cursor), limit:'100'});
        const data = await getJSON(`/api/tasks/${task.task_id}/events?${params}`); if (!current()) return;
        if (data.task_id !== selected || data.run_id !== task.current_run_id) throw new Error('Mismatched run');
        journal = mergeEvents(journal, data, task.current_run_id);
        if (!data.has_more || !data.events.length) break;
      }
      renderEvents();
    }
    lastSuccess = new Date(); $('error').hidden = true;
    $('connection').classList.remove('stale'); document.querySelector('.workspace').classList.remove('stale');
    setText('connection', '기록 연결됨 · 자동 갱신');
    setText('last-update', `최근 확인 ${timeLabel(lastSuccess)}`);
    if (hadError) announce(`기록 연결이 복구되었습니다. ${stateNotice}`);
    else if (stateKey && stateKey !== lastAnnouncedState) announce(stateNotice);
    lastAnnouncedState = stateKey; hadError = false;
  } catch (error) {
    if (!current()) return;
    hadError = true;
    const messages = {NOT_FOUND:'선택한 작업이나 실행 기록을 찾을 수 없습니다.', DB_NOT_FOUND:'연결된 기록 파일을 찾을 수 없습니다.',
      SCHEMA_UNSUPPORTED:'현재 기록 형식을 읽을 수 없습니다.', DB_BUSY:'기록을 사용 중입니다. 잠시 후 다시 확인합니다.'};
    const message = messages[error.code] ?? '기록을 갱신하지 못했습니다. 서버 연결을 확인해 주세요.';
    if ($('error').textContent !== message) setText('error', message);
    $('error').hidden = false; $('connection').classList.add('stale'); document.querySelector('.workspace').classList.add('stale');
    setText('connection', lastSuccess ? '갱신 중단 · 이전 기록 표시' : '기록 연결 안 됨');
    if (error.code === 'NOT_FOUND') {
      $('detail-content').hidden = true; $('detail-badge').hidden = true; $('detail-empty').hidden = false;
      setText('detail-empty', '선택한 작업을 찾을 수 없습니다. 목록에서 다른 작업을 선택해 주세요.');
      $('timeline').replaceChildren(); $('event-empty').hidden = false; setText('event-empty', '표시할 기록이 없습니다.');
      eventRenderKey = ''; setText('event-count', '0개'); $('event-limit').hidden = true;
    }
  } finally {
    busy = false;
    if (!document.hidden) timer = setTimeout(requestRefresh, queued ? 0 : 3000);
  }
}

$('search').addEventListener('input', renderTasks);
$('filter').addEventListener('change', renderTasks);
$('refresh').addEventListener('click', requestRefresh);
document.addEventListener('visibilitychange', () => {
  if (document.hidden) {generation++; controller?.abort(); clearTimeout(timer);}
  else requestRefresh();
});
window.addEventListener('popstate', () => {
  const next = new URL(location.href).searchParams.get('task');
  if (validId(next)) selectTask(next);
});
if (initial && !validId(initial)) {announce('잘못된 작업 주소입니다. 최근 작업을 표시합니다.'); rememberSelection();}
requestRefresh();
