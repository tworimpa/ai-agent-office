import {usageActions} from './workspace-profile.js';

const stages = {start: '착수', reproduce: '재현', implement: '구현', verify: '검증', review: '검토'};
const checks = {quick: '관련 테스트', full: '전체 회귀', journey: '사용자 여정'};
const checkStatus = {passed: '통과', failed: '실패', unverified: '미검증', skipped: '생략'};
const roles = {coordinator: '조정자', developer: '담당자', reviewer: '검토자', tester: '테스트 실행기', operator: '사용자'};
const who = (role, owner) => [...new Set([roles[role] || role, owner].filter(Boolean))].join(' · ') || '담당 미확인';
const runningStates = new Set(['RUNNING','TESTING','REVIEWING','STOP_REQUESTED']);
const idOK = id => typeof id === 'string' && /^[a-f0-9]{32}$/.test(id);
const stageName = value => stages[value] || value || '단계 미확인';
const config = row => ({priority: row?.priority ?? 0, areas: row?.areas ?? [], dependencies: row?.dependencies ?? []});
const when = value => {
  const date = new Date(value);
  return !value || Number.isNaN(date.getTime()) ? '시각 미확인' : date.toLocaleString('ko-KR');
};

// Both forms produce the same coordination document. Inputs survive background polling.
function buildFields({root, prefix, make, sources, requests}) {
  let dependencies = [], target = null, readOnly = false;
  const id = key => prefix + '-' + key;
  function input(parent, key, title, type, choices) {
    const label = make('label', title), node = make(type);
    node.id = id(key);
    label.htmlFor = node.id;
    parent.append(label, node);
    if (choices) options(node, choices);
    return node;
  }
  function options(node, entries) {
    node.replaceChildren(...entries.map(([value, text]) => {
      const option = make('option', text);
      option.value = value;
      return option;
    }));
  }
  const priority = input(root, 'priority', '우선순위 (−9~9)', 'input');
  priority.type = 'number'; priority.min = '-9'; priority.max = '9'; priority.step = '1';
  const areas = input(root, 'areas', '예상 변경 영역 (한 줄에 하나씩, 최대 16개)', 'textarea');
  areas.rows = 2;
  const deps = make('fieldset', '', 'dependency-fields');
  deps.append(make('legend', '선행 조건'));
  const list = make('ul', '', 'dependency-list');
  deps.append(list);
  const source = input(deps, 'source', '선행 목표', 'select');
  const stage = input(deps, 'stage', '이 목표에서 기다릴 단계', 'select');
  const condition = input(deps, 'condition', '기다릴 조건', 'select', [['completion', '선행 목표 완료'], ['product_decision', '제품 결정 답변']]);
  const product = make('div', '', 'dependency-fields');
  const request = input(product, 'request', '제품 질문', 'select');
  const question = input(product, 'question', '아직 접수되지 않은 제품 질문 원문', 'textarea');
  question.rows = 2;
  deps.append(product);
  const reason = input(deps, 'reason', '이 조건이 필요한 이유', 'input');
  const add = make('button', '조건 추가', 'secondary');
  add.id = id('add'); add.type = 'button';
  deps.append(add);
  root.append(deps);
  const preview = make('p', '', 'coordination-preview');
  preview.id = id('preview'); preview.setAttribute('aria-live', 'polite');
  root.append(preview);
  function alreadyStarted(value) {
    const order = Object.keys(stages);
    return target?.active_stage && order.includes(target.stage) && order.indexOf(value) <= order.indexOf(target.stage);
  }
  function sourceName(runId) {
    return sources().find(row => row.run_id === runId)?.task || runId;
  }
  function describe(dep) {
    const query = requests(dep.source_run).find(row => row.request_id === dep.request_id)?.prompt;
    const wait = dep.condition === 'completion' ? '완료' : '제품 결정 답변 · ' + (query || dep.question || dep.request_id);
    return stageName(dep.stage) + ' 전 · ' + sourceName(dep.source_run) + ' · ' + wait + ' · 이유 ' + dep.reason;
  }
  function renderList() {
    list.replaceChildren(...dependencies.map((dep, index) => {
      const item = make('li'), remove = make('button', '조건 삭제', 'secondary');
      remove.type = 'button';
      if (alreadyStarted(dep.stage)) {
        item.append(make('span', describe(dep) + ' · 시작한 단계의 조건은 고정됩니다.'));
        return item;
      }
      remove.addEventListener('click', () => {
        dependencies.splice(index, 1); renderList(); effects();
        (list.querySelectorAll('button')[index] || add).focus();
      });
      item.append(make('span', describe(dep)), remove);
      return item;
    }));
  }
  function products() {
    options(request, [['', '질문 원문으로 지정'], ...requests(source.value).map(row => [row.request_id, row.prompt])]);
    details();
  }
  function details() {
    product.hidden = condition.value !== 'product_decision';
    question.hidden = question.labels[0].hidden = Boolean(request.value);
  }
  function effects(message = '') {
    const priorityText = '우선순위 ' + (priority.value || '0') + ' · 실행 가능한 목표 중 높은 값이 먼저 시작합니다.';
    const active = target?.active_stage ? ' 이미 시작한 단계는 계속 진행합니다.' : '';
    const competing = sources().filter(row => row.run_id !== target?.run_id && row.state === 'QUEUED' && !row.wait_reasons?.length);
    const ahead = competing.filter(row => row.priority > Number(priority.value));
    const ordering = competing.length ? (ahead.length ? ' 더 높은 순위로 대기 중 · ' + ahead.map(row => row.task).join(', ') + '.' :
      ' 현재 대기열에 우선순위가 더 높은 목표가 없습니다.') : '';
    const gated = dependencies.length ? ' 지정한 ' + dependencies.length + '개 조건이 충족될 때까지 해당 단계 앞에서 기다립니다.' : ' 선행 조건이 없습니다.';
    const areaValues = areaLines();
    const overlaps = sources().filter(row => row.run_id !== target?.run_id && row.areas?.some(area => areaValues.includes(area)));
    const overlap = overlaps.length ? ' 변경 영역 겹침 가능 · ' + overlaps.map(row => row.task).join(', ') + '. 영역 겹침만으로 단계를 보류하지 않습니다.' : '';
    const mode = readOnly ? '읽기 전용 작업의 선행 조건은 착수 전에 확인합니다. ' : '';
    const invalid = readOnly && dependencies.some(dep => dep.stage !== 'start') ? ' 착수 이후로 지정한 조건을 삭제하고 다시 추가하세요.' : '';
    preview.textContent = (message ? message + ' ' : '') + mode + priorityText + ordering + gated + active + overlap + invalid;
  }
  function areaLines() { return areas.value.split(/\r?\n/).map(line => line.trim()).filter(Boolean); }
  function pending() {
    if (!source.value && !reason.value.trim() && !question.value.trim()) return null;
    if (!stage.value || alreadyStarted(stage.value)) throw Error('이미 시작한 단계에는 선행 조건을 추가할 수 없습니다.');
    if (!source.value || !reason.value.trim()) throw Error('선행 목표와 조건이 필요한 이유를 입력하세요.');
    const dep = {source_run: source.value, stage: stage.value, condition: condition.value, reason: reason.value.trim()};
    if (condition.value === 'product_decision') {
      if (request.value) dep.request_id = request.value;
      else if (question.value.trim()) dep.question = question.value.trim();
      else throw Error('제품 질문을 선택하거나 앞으로 접수될 질문 원문을 입력하세요.');
    }
    return dep;
  }
  add.addEventListener('click', () => {
    try {
      const dep = pending();
      if (!dep) throw Error('먼저 선행 목표를 선택하세요.');
      if (dependencies.length >= 16) throw Error('선행 조건은 최대 16개까지 추가할 수 있습니다.');
      if (dependencies.some(old => ['source_run','stage','condition','request_id','question'].every(key => old[key] === dep[key]))) {
        throw Error('이미 추가한 선행 조건입니다.');
      }
      dependencies.push(dep);
      source.value = reason.value = question.value = ''; products();
      renderList(); effects();
    } catch (error) { effects(error.message); }
  });
  source.addEventListener('change', products);
  for (const node of [request, condition]) node.addEventListener('change', details);
  for (const node of [priority, areas]) node.addEventListener('input', () => effects());
  function stageOptions() {
    const selected = stage.value;
    options(stage, readOnly ? [['start', stages.start]] : Object.entries(stages));
    for (const opt of stage.options) opt.disabled = Boolean(alreadyStarted(opt.value));
    const available = [...stage.options].filter(opt => !opt.disabled);
    stage.value = available.find(opt => opt.value === selected)?.value || available[0]?.value || '';
  }
  function mode(value) {
    if (readOnly === value) return;
    readOnly = value;
    stageOptions(); effects();
  }
  function load(row, values, readOnlyTask = row?.allow_edits === false) {
    target = row;
    readOnly = readOnlyTask;
    const baseline = config(values);
    priority.value = baseline.priority;
    areas.value = baseline.areas.join('\n');
    dependencies = [...baseline.dependencies];
    options(source, [['', '선행 목표 선택'], ...sources().filter(row => row.run_id !== target?.run_id).map(row => [row.run_id, row.task])]);
    stage.value = ''; stageOptions();
    condition.value = 'completion'; reason.value = question.value = '';
    products(); renderList(); effects();
  }
  function value() {
    const numeric = Number(priority.value || 0);
    if (!Number.isInteger(numeric) || numeric < -9 || numeric > 9) throw Error('우선순위는 −9에서 9 사이의 정수로 입력하세요.');
    if (pending()) throw Error('작성 중인 선행 조건을 먼저 추가하거나 비워 주세요.');
    if (readOnly && dependencies.some(dep => dep.stage !== 'start')) throw Error('읽기 전용 작업에서는 착수 전 조건만 지정할 수 있습니다.');
    if (areaLines().length > 16) throw Error('변경 영역은 최대 16개까지 입력할 수 있습니다.');
    return {priority: numeric, areas: areaLines(), dependencies: [...dependencies]};
  }
  return {load, value, mode};
}

export function mountBriefing({win, get, make, api, context, refresh, openAction, taskTitle, stateName, uuid}) {
  let latest = null, read = 0, fresh = false, loadingFailed = false;
  let visitKey = null, previousVisit = null;
  let visitRequest = null;
  const lastSeen = new Map(), pendingVisits = new Map();
  let editing = null, correcting = null, saving = false;
  const keys = new Map();
  const path = () => '/rooms/' + context().workspaceId;
  const writable = () => fresh && context().writable;
  function sources() {
    const items = new Map(context().runs.map(row => [row.id, {run_id: row.id, task: taskTitle(row.task)}]));
    for (const row of latest?.work || []) items.set(row.run_id, row);
    return [...items.values()];
  }
  function requests(run) {
    const rows = new Map();
    for (const row of latest?.decision_options || []) if (row.source_run === run) rows.set(row.request_id, row);
    for (const row of latest?.decisions || []) if (row.run_id === run && row.kind === 'product') rows.set(row.request_id, row);
    for (const work of latest?.work || []) {
      for (const row of work.decisions || []) if (row.source_run === run) rows.set(row.request_id, row);
    }
    return [...rows.values()];
  }
  const fields = prefix => buildFields({make, sources, requests, prefix,
    root: get(prefix === 'assignment' ? 'assignment-coordination' : 'coordination-fields')});
  const intake = fields('assignment'), editor = fields('coordination');
  const section = get('workspace-briefing');
  function reset() {
    ++read; fresh = false; latest = editing = correcting = null; saving = loadingFailed = false; keys.clear();
    visitKey = previousVisit = null;
    visitRequest = null;
    get('brief-visit-note').textContent = '';
    get('brief-visit-retry').hidden = true;
    get('brief-visit').hidden = true;
    get('brief-visit').open = false;
    get('brief-visit-rows').replaceChildren();
    for (const id of ['brief-questions','brief-permissions','brief-review','brief-work','brief-overlaps']) get(id).replaceChildren();
    for (const id of ['brief-question-count','brief-permission-count','brief-review-count','brief-work-count','brief-time','brief-note']) get(id).textContent = '';
    section.hidden = true; get('brief-progress').open = false;
  }
  function note(message) { get('brief-note').textContent = message; }
  function controls() {
    const {supported, writable: roomOK} = context();
    section.hidden = !supported || (latest && !latest.work.length && !latest.decisions.length && !latest.candidates.length && !context().runs.length);
    if (!supported) return;
    if (!roomOK) { fresh = false; ++read; }
    for (const node of section.querySelectorAll('button')) node.disabled = !writable();
    for (const id of ['coordination-form','decision-revision-form']) {
      for (const node of get(id).elements) node.disabled = saving || !writable();
    }
    const observed = get('brief-time');
    observed.dateTime = latest?.observed_at ?? '';
    observed.textContent = latest ? '마지막 확인 · ' + when(latest.observed_at) : '';
    if (!writable()) note(latest ? '최신 상태를 확인할 수 없어 마지막 기록을 표시합니다.' :
      loadingFailed || !roomOK ? '브리핑을 불러올 수 없습니다.' : '목표 기록을 확인하고 있습니다.');
  }
  function action(row, kind, title, callback) {
    const button = make('button', title, 'brief-action');
    button.type = 'button';
    button.dataset.action = kind; button.dataset.run = row.run_id;
    if (row.request_id) button.dataset.request = row.request_id;
    if (row.attempt) button.dataset.attempt = String(row.attempt);
    button.addEventListener('click', async () => {
      if (!writable()) return;
      if (callback) callback();
      else if (!await openAction(kind, row) && writable()) note('연결된 기록이 바뀌었습니다. 최신 브리핑을 확인하세요.');
    });
    return button;
  }
  function card(title, meta) {
    const item = make('article', '', 'brief-card');
    item.append(make('strong', title));
    if (meta) item.append(make('p', meta));
    return item;
  }
  function decision(row) {
    const kind = row.kind === 'plan' ? 'plan' : row.kind === 'permission' ? 'permission' : 'answer';
    const labels = {product: '제품 결정', plan: '계획 확인', question: '기술 질문', permission: '이번 요청 1회'};
    const owner = who(row.role, row.owner);
    const item = card(row.prompt, [labels[row.kind], owner, taskTitle(row.task)].join(' · '));
    if (row.kind !== 'permission' && row.impacts?.length) {
      const list = make('ul', '', 'brief-impact');
      for (const impact of row.impacts) {
        const target = make('li', taskTitle(impact.task) + ' · ' + stageName(impact.stage));
        target.dataset.impactRun = impact.run_id;
        if (impact.proof) target.append(make('span', impact.proof, 'brief-proof'));
        else if (impact.progress) target.append(make('span', impact.progress, 'brief-impact-progress'));
        list.append(target);
      }
      item.append(make('small', '영향받는 목표와 단계'), list);
    }
    item.append(action(row, kind, {answer: '질문에 답하기', plan: '계획 확인하기', permission: '권한 범위 확인'}[kind]));
    return item;
  }
  function candidate(row) {
    const item = card(taskTitle(row.task), row.attempt + '차 후보 · 독립 검토 승인');
    item.append(action(row, 'candidate', '후보와 검증 근거 보기'));
    if (row.summary) item.append(make('p', row.summary));
    const evidence = (row.checks || []).map(check => (checks[check.stage] || stageName(check.stage)) + ' ' + (checkStatus[check.status] || check.status));
    if (Number.isInteger(row.manual_count)) evidence.push('수동 확인 ' + row.manual_count + '건');
    if (row.digest) evidence.push('변경 ' + row.digest.slice(0, 12));
    item.append(make('p', evidence.join(' · ')));
    return item;
  }
  function workCard(row) {
    const profile = context().runs.find(run => run.id === row.run_id)?.assignment?.roles?.[row.role];
    const owner = who(row.role, row.owner || profile?.name);
    const item = card(taskTitle(row.task), [owner, stateName(row.state), stageName(row.stage), '우선순위 ' + row.priority].join(' · '));
    const waiting = {quota:'공급자 계정 한도 대기',user:'사용자 결정 대기',dependency:'선행 작업 대기',host:'실행 호스트의 작업 시작 대기'};
    if (waiting[row.wait_kind]) item.append(make('p', waiting[row.wait_kind], 'brief-wait'));
    item.append(make('p', [row.progress, '마지막 진전 ' + when(row.last_progress_at)].filter(Boolean).join(' · ')));
    if (row.proof) item.append(make('p', row.proof, 'brief-proof'));
    if (row.usage_hold) item.append(make('p', [usageActions[row.usage_hold.kind] || '한도 조치', row.usage_hold.reason].join(' · '),
      'brief-wait'), action(row, 'usage', '한도와 재개 기록'));
    if (row.last_resume_at) item.append(make('p', '마지막 재개 · ' + when(row.last_resume_at)));
    if (row.assignment_reason) item.append(make('p', '배정 근거 · ' + row.assignment_reason));
    if (row.wait_reasons?.length) item.append(make('p', '대기 사유 · ' + row.wait_reasons.join(' / '), 'brief-wait'));
    if (row.areas?.length) item.append(make('p', '예상 변경 영역 · ' + row.areas.join(', ')));
    for (const dep of row.dependencies || []) {
      const src = sources().find(other => other.run_id === dep.source_run);
      item.append(make('p', '선행 조건 · ' + stageName(dep.stage) + ' 전 · ' + (src?.task || dep.source_run) +
        ' · ' + (dep.condition === 'completion' ? '완료' : '제품 결정') + ' · ' + dep.reason));
    }
    for (const record of row.decisions || []) {
      const evidence = make('details');
      evidence.dataset.fold = row.run_id + ':' + record.request_id;
      const source = sources().find(other => other.run_id === record.source_run);
      evidence.append(make('summary', '이 단계에서 사용한 결정 · ' + record.prompt), make('p', record.answer),
        make('p', '출처 · ' + (source?.task || record.source_run) + ' · 사용한 버전 ' + record.revision));
      const current = latest.decision_options?.find(option => option.source_run === record.source_run && option.request_id === record.request_id);
      if (current?.revision > record.revision) evidence.append(make('p', '현재 결정 ' + current.revision + ' · ' + current.answer));
      if (current?.answer && idOK(record.request_id)) {
        evidence.append(action({run_id: source?.run_id || record.source_run, request_id: record.request_id}, 'source', '원본 결정 열기', () => {
          const origin = [...get('brief-work').querySelectorAll('[data-source-request]')].find(node => node.dataset.sourceRequest === record.request_id);
          if (origin) { origin.open = true; origin.querySelector('summary').focus(); }
        }));
      } else if (source) evidence.append(action({run_id: source.run_id}, 'work', '출처 목표 열기'));
      item.append(evidence);
    }
    for (const record of row.sourceDecisions) {
      const evidence = make('details');
      evidence.dataset.fold = row.run_id + ':source:' + record.request_id;
      evidence.dataset.sourceRequest = record.request_id;
      evidence.append(make('summary', '제품 결정 기록 · ' + record.prompt), make('p', '현재 결정 ' + record.revision + ' · ' + record.answer));
      if (record.reason) evidence.append(make('p', '수정 이유 · ' + record.reason));
      evidence.append(action({...record, run_id: row.run_id}, 'revise', '결정 수정', () => editRevision(record)));
      item.append(evidence);
    }
    item.append(action(row, 'work', '작업 기록 열기'));
    if (row.editable) item.append(action(row, 'coordinate', '우선순위·선행 조건 조정', () => edit(row.run_id)));
    return item;
  }
  function drawGroup(id, rows, renderer, empty, limit = Infinity) {
    const key = JSON.stringify(rows);
    if (keys.get(id) === key) return;
    keys.set(id, key);
    const root = get(id), active = root.ownerDocument.activeElement;
    const focus = root.contains(active) ? active : null;
    const folds = new Set([...root.querySelectorAll('details[open]')].map(node => node.dataset.fold));
    const visible = rows.slice(0, limit).map(renderer);
    if (rows.length > limit) {
      const more = make('details', '', 'brief-more');
      more.dataset.fold = 'more';
      more.append(make('summary', '나머지 ' + (rows.length - limit) + '건 보기'), ...rows.slice(limit).map(renderer));
      visible.push(more);
    }
    root.replaceChildren(...(visible.length ? visible : [make('p', empty, 'brief-empty')]));
    for (const detail of root.querySelectorAll('details')) detail.open = folds.has(detail.dataset.fold);
    if (focus) {
      const match = [...root.querySelectorAll('button, summary')].find(node =>
        focus.tagName === 'SUMMARY' ? node.tagName === 'SUMMARY' && node.parentNode.dataset.fold === focus.parentNode.dataset.fold :
        node.tagName === 'BUTTON' && JSON.stringify(node.dataset) === JSON.stringify(focus.dataset));
      (match || section.querySelector('h2')).focus({preventScroll: true});
    }
  }
  function render() {
    const questions = latest.decisions.filter(row => row.kind !== 'permission')
      .sort((a, b) => Number(a.kind === 'question') - Number(b.kind === 'question'));
    const permits = latest.decisions.filter(row => row.kind === 'permission');
    for (const [id, rows, renderer, empty] of [
      ['question', questions, decision, '답변을 기다리는 질문이 없습니다.'],
      ['permission', permits, decision, '확인할 권한 요청이 없습니다.'],
      ['review', latest.candidates, candidate, '검증과 독립 검토를 마친 후보가 없습니다.'],
    ]) {
      get('brief-' + id + '-count').textContent = rows.length + '건';
      drawGroup('brief-' + ({question: 'questions', permission: 'permissions'}[id] || id), rows, renderer, empty, 1);
    }
    const resolved = (latest.decision_options || []).filter(record => idOK(record.request_id) && record.answer && Number.isInteger(record.revision));
    const work = latest.work.map(row => ({...row, sourceDecisions: resolved.filter(record => record.source_run === row.run_id)}));
    const pending = new Set(latest.decisions.map(row => row.run_id));
    const waiting = work.filter(row => row.wait_reasons?.length || pending.has(row.run_id) || ['QUEUED','WAITING_USER'].includes(row.state));
    const waitingIds = new Set(waiting.map(row => row.run_id));
    const running = work.filter(row => runningStates.has(row.state) && !waitingIds.has(row.run_id)).length;
    get('brief-work-count').textContent = ['진행 ' + running, '대기 ' + waiting.length, '종료 ' + (work.length - running - waiting.length)].join(' · ');
    drawGroup('brief-work', work, workCard, '접수된 목표가 없습니다.');
    const label = id => sources().find(row => row.run_id === id)?.task || id;
    const overlaps = latest.overlaps.map(row => [label(row.left), label(row.right), row.area, row.reason]);
    drawGroup('brief-overlaps', overlaps, ([left,right,area,reason]) =>
      make('p', '변경 영역 겹침 가능 · ' + left + ' / ' + right + ' · ' + area + (reason ? ' · ' + reason : '') +
        ' · 자동 보류 없음', 'brief-wait'), '');
    get('brief-overlaps').hidden = !overlaps.length;
    renderVisit();
    controls();
  }
  function sharedVisit(id) {
    const key = visitKey, room = context().workspaceId;
    const operation = {key, id};
    visitRequest = operation;
    pendingVisits.set(key, id);
    try { win.sessionStorage.setItem(key + ':pending', id); } catch { /* Keep id in this tab. */ }
    get('brief-visit-retry').hidden = true;
    get('brief-visit-note').textContent = '실행 호스트의 이전 방문 시각을 확인하고 있습니다.';
    api(path() + '/visit', {id}).then(result => {
      if (operation !== visitRequest || key !== visitKey) return;
      if (result.id !== id || result.workspace_id !== room || !Number.isFinite(Date.parse(result.visited_at)) ||
          (result.previous_visit !== null && !Number.isFinite(Date.parse(result.previous_visit)))) throw Error();
      pendingVisits.delete(key);
      try { win.sessionStorage.removeItem(key + ':pending'); } catch { /* Already acknowledged in memory. */ }
      previousVisit = result.previous_visit;
      get('brief-visit-note').textContent = '브라우저와 설치형 앱이 공유하는 방문 기록입니다.';
      if (latest) renderVisit();
    }).catch(() => {
      if (operation !== visitRequest || key !== visitKey) return;
      get('brief-visit-note').textContent = '공유 방문 시각을 확인하지 못해 이 브라우저의 기록을 표시합니다.';
      get('brief-visit-retry').hidden = false;
    });
  }
  get('brief-visit-retry').addEventListener('click', () => {
    const id = pendingVisits.get(visitKey);
    if (id && writable() && !get('brief-visit-retry').hidden) sharedVisit(id);
  });
  function renderVisit() {
    const scope = context().visitKey;
    get('brief-visit').hidden = !scope;
    if (!scope || win.document.hidden || !Number.isFinite(Date.parse(latest.observed_at))) return;
    const key = 'office-last-visit:' + scope;
    if (visitKey !== key) {
      visitKey = key;
      let saved = lastSeen.get(key);
      try { saved = win.localStorage.getItem(key) || saved; } catch { /* This visit still works in memory. */ }
      previousVisit = Number.isFinite(Date.parse(saved)) ? saved : null;
      if (context().sharedVisit) {
        let pending = pendingVisits.get(key);
        try { pending = win.sessionStorage.getItem(key + ':pending') || pending; } catch { /* Use the in-memory operation. */ }
        sharedVisit(idOK(pending) ? pending : uuid());
      }
    }
    if (!win.document.hidden) {
      const observed = latest.observed_at;
      lastSeen.set(key, observed);
      try {
        const saved = win.localStorage.getItem(key);
        if (!saved || !(Date.parse(saved) >= Date.parse(observed))) win.localStorage.setItem(key, observed);
      } catch { /* Storage can be unavailable in a private browser. */ }
    }
    get('brief-visit-window').textContent = previousVisit ? when(previousVisit) + ' 이후 작업실 기록' : '이전 방문 시각이 없습니다.';
    const rows = [];
    const add = (row, at, text, kind, target) => {
      if (!previousVisit || !idOK(row.run_id) || !(Date.parse(at) > Date.parse(previousVisit)) || Date.parse(at) > Date.parse(latest.observed_at)) return;
      rows.push({row, at, text, kind, target});
    };
    for (const row of latest.decisions) {
      const perm = row.kind === 'permission', plan = row.kind === 'plan';
      add(row, row.created_at, (perm ? '새 권한 요청' : plan ? '계획 확인 요청' : '새 질문') + ' · ' + row.prompt,
        perm ? 'permission' : plan ? 'plan' : 'answer', perm ? '범위 확인' : plan ? '계획 확인' : '질문에 답하기');
    }
    for (const row of latest.candidates) add(row, row.reviewed_at, row.attempt + '차 후보 독립 검토 승인', 'candidate', '후보 검증 근거');
    for (const row of latest.work) {
      if (row.usage_hold) add(row, row.usage_hold.at, [usageActions[row.usage_hold.kind] || '한도 조치', row.usage_hold.reason].join(' · '), 'usage', '한도 기록');
      add(row, row.last_resume_at, '작업 재개', 'usage', '재개 기록');
      if (row.progress) add(row, row.last_progress_at, row.progress, 'work', '진행 기록');
    }
    rows.sort((a,b) => Date.parse(b.at) - Date.parse(a.at));
    get('brief-visit-count').textContent = previousVisit ? rows.length + '건' : '';
    drawGroup('brief-visit-rows', rows, ({row, at, text, kind, target}) => {
      const item = card(taskTitle(row.task), text);
      item.append(make('small', when(at)), action(row, kind, target));
      return item;
    }, previousVisit ? '이후에 확인된 변경이 없습니다.' : '', 4);
  }
  async function load(readSection = api) {
    if (!context().supported) return;
    const seq = ++read, room = context().workspaceId;
    try {
      const result = await readSection(path() + '/briefing');
      if (seq !== read || context().workspaceId !== room) return;
      if (result.workspace_id !== room || !['decisions','work','candidates','overlaps'].every(key => Array.isArray(result[key]))) throw Error();
      latest = result; fresh = true; loadingFailed = false; note(''); render();
    } catch {
      if (seq === read && context().workspaceId === room) { fresh = false; loadingFailed = true; controls(); }
    }
  }
  function edit(id) {
    if (!writable() || saving) return;
    const row = latest.work.find(item => item.run_id === id && item.editable);
    if (!row) return;
    editing = row;
    get('coordination-task').textContent = taskTitle(row.task);
    get('coordination-note').textContent = '';
    editor.load(row, row); get('coordination-dialog').showModal(); get('coordination-priority').focus();
  }
  function editRevision(record) {
    if (!writable() || saving) return;
    correcting = {...record};
    get('revision-prompt').textContent = record.prompt;
    get('revision-before').textContent = '기존 결정 ' + record.revision + ' · ' + record.answer;
    const impacts = latest.work.flatMap(row => (row.dependencies || []).filter(dep => dep.source_run === record.source_run &&
      dep.condition === 'product_decision' && (dep.request_id === record.request_id || dep.question === record.prompt))
      .map(dep => taskTitle(row.task) + ' · ' + stageName(dep.stage)));
    get('revision-impact').textContent = impacts.length ? '선행 조건으로 연결된 단계 · ' + impacts.join(', ') :
      '현재 이 답변을 선행 조건으로 지정한 목표가 없습니다.';
    get('revision-answer').value = record.answer; get('revision-reason').value = ''; get('revision-note').textContent = '';
    get('decision-revision-dialog').showModal(); get('revision-answer').focus();
  }
  async function save(dialogId, noteId, endpoint, body, accepted) {
    const identity = context().identity;
    saving = true; controls();
    try {
      const response = await api(path() + endpoint, body);
      if (identity !== context().identity) return;
      if (!accepted(response)) throw Error('저장 결과를 확인할 수 없습니다.');
      get(dialogId).close();
      await refresh();
    } catch (error) {
      if (identity === context().identity) get(noteId).textContent = error.message || '저장 결과를 확인할 수 없습니다.';
    } finally {
      if (identity === context().identity) { saving = false; controls(); }
    }
  }
  get('coordination-form').addEventListener('submit', event => {
    event.preventDefault();
    if (!editing || saving || !writable()) return;
    let values;
    try { values = editor.value(); } catch (error) { get('coordination-note').textContent = error.message; return; }
    const row = editing;
    save('coordination-dialog', 'coordination-note', '/coordination/' + row.run_id,
      {...values, revision: row.coordination_revision}, result => result.run_id === row.run_id && result.workspace_id === context().workspaceId);
  });
  get('decision-revision-form').addEventListener('submit', event => {
    event.preventDefault();
    if (!correcting || saving || !writable()) return;
    const record = correcting, answer = get('revision-answer').value.trim(), reason = get('revision-reason').value.trim();
    if (!answer || !reason) { get('revision-note').textContent = '답변과 수정 이유를 입력하세요.'; return; }
    if (answer === record.answer) { get('revision-note').textContent = '기존 답변과 달라지는 내용을 입력하세요.'; return; }
    if (record.operation?.answer !== answer || record.operation.reason !== reason) {
      record.operation = {id: uuid(), revision: record.revision, answer, reason};
    }
    save('decision-revision-dialog', 'revision-note', '/decision-revisions/' + record.request_id,
      record.operation, result => result.workspace_id === context().workspaceId && result.decision?.source_run === record.source_run &&
        result.decision.request_id === record.request_id && result.decision.revision === record.revision + 1 &&
        result.decision.answer === answer && result.decision.reason === reason);
  });
  return {reset, load, controls, edit, intake, canEdit: id => writable() && latest.work.some(row => row.run_id === id && row.editable)};
}
