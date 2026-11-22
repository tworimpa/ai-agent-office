import {modes, usageActions} from './workspace-profile.js';

const groupNames = {reproduction: '재현·수정', development: '일반 개발', read_only: '읽기 전용'};
const roleNames = {developer: '담당자', reviewer: '검토자', coordinator:'조정자'};
const unknown = '미확인';
const metric = (number, unit = '') => Number.isFinite(number) ? number.toLocaleString('ko-KR') + unit : unknown;
const safeId = id => typeof id === 'string' && /^[0-9a-f]{32}$/.test(id);
const fmtDate = value => value && Number.isFinite(Date.parse(value)) ? new Date(value).toLocaleString('ko-KR') : unknown;

function provenance(source, at) {
  const ago = (Date.now() - Date.parse(at)) / 60000;
  const age = !Number.isFinite(ago) || ago < 0 ? '' : ago >= 1440 ? Math.floor(ago / 1440) + '일 전' :
    ago >= 60 ? Math.floor(ago / 60) + '시간 전' : ago >= 1 ? Math.floor(ago) + '분 전' : '방금';
  return ['출처 ' + (source || unknown), '관측 ' + fmtDate(at), age].filter(Boolean).join(' · ');
}

// Missing evidence has no radial position. Adjacent scored axes alone receive a connecting line.
function radar(doc, axes) {
  const ns = 'http://www.w3.org/2000/svg';
  function shape(tag, attrs) {
    const node = doc.createElementNS(ns, tag);
    for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, value);
    return node;
  }
  const svg = shape('svg', {viewBox:'0 0 240 196', 'aria-hidden':'true', focusable:'false', class:'character-radar'});
  const point = (i, r) => [120 + r * Math.sin(i * Math.PI / 3), 94 - r * Math.cos(i * Math.PI / 3)];
  function line(a, b, cls) {
    svg.append(shape('line', {x1:a[0], y1:a[1], x2:b[0], y2:b[1], class:cls}));
  }
  for (let level = 1; level <= 5; level++) svg.append(shape('polygon', {
    points:axes.map((_, i) => point(i, 13 * level).join(',')).join(' '), class:'radar-grid'}));
  const known = axes.map((axis, i) => Number.isInteger(axis.level) && axis.level >= 1 && axis.level <= 5 ? point(i, axis.level * 13) : null);
  axes.forEach((axis, i) => {
    line(point(i, 0), point(i, 65), 'radar-grid');
    const [x, y] = point(i, 86);
    const text = shape('text', {x, y:y + 4, 'text-anchor':'middle'});
    text.textContent = axis.label + (known[i] ? '' : ' ?');
    svg.append(text);
  });
  known.forEach((p, i) => {
    if (!p) return;
    const next = known[(i + 1) % known.length];
    if (next) line(p, next, 'radar-edge');
    svg.append(shape('circle', {cx:p[0], cy:p[1], r:3, class:'radar-value'}));
  });
  return svg;
}

function usageView(make, data) {
  const root = make('div', '', 'usage-grid'), profile = data.profile;
  if (profile) {
    const block = make('section');
    block.append(make('h3', '이번 작업 방식'), make('p', [modes[profile.mode] || profile.mode, profile.reason].filter(Boolean).join(' · ')));
    for (const [role, prefs] of Object.entries(profile.roles || {})) {
      if (data.active_roles && !data.active_roles.includes(role)) continue;
      block.append(make('p', [roleNames[role] || role, prefs.engine, prefs.model || 'CLI 기본 모델', prefs.effort || 'CLI 기본 강도'].filter(Boolean).join(' · ')));
      if (profile.role_reasons?.[role]) block.append(make('small', profile.role_reasons[role]));
    }
    root.append(block);
  }
  const usage = data.usage || {}, budget = data.budget || {}, calls = usage.calls || [];
  function section(title) {
    const block = make('section');
    block.append(make('h3', title));
    root.append(block);
    return block;
  }
  function row(block, value, source, at) {
    const group = make('div', '', 'usage-item');
    group.append(make('p', value), make('small', provenance(source, at)));
    block.append(group);
  }
  const tokens = section('작업에서 소비한 토큰');
  tokens.append(make('p', '기록된 합계 ' + metric(usage.total_tokens, ' 토큰') + ' · 사용량 미확인 호출 ' + metric(usage.unknown_calls, '건')));
  if (usage.usage_coverage === 'partial') tokens.append(make('small', '일부 호출에서만 소비량을 보고했습니다.'));
  const context = section('호출별 컨텍스트 점유');
  if (!calls.length) {
    tokens.append(make('small', '호출별 토큰 근거 미확인'));
    context.append(make('p', '컨텍스트 사용량과 창 크기 미확인'));
  }
  for (const call of calls) {
    const who = [roleNames[call.role] || call.role, call.engine, call.stage, call.attempt == null ? null : call.attempt + '차'].filter(Boolean).join(' · ');
    row(tokens, who + ' · 요청 ' + [call.requested_model || 'CLI 기본 모델', call.requested_effort || 'CLI 기본 강도'].join(' / ') +
      ' · 관측 ' + [call.actual_model || unknown, call.actual_effort || unknown].join(' / ') +
      ' · 입력 ' + metric(call.input_tokens) + ' (캐시 ' + metric(call.cached_input_tokens) + ') · 출력 ' + metric(call.output_tokens) +
      ' (추론 ' + metric(call.reasoning_output_tokens) + ') · 합계 ' + metric(call.total_tokens), call.source, call.observed_at);
    const estimate = call.context_basis === 'last_response_total';
    const occupancy = estimate ? '직전 응답 기준 추정 점유 ' : '점유 ';
    const remaining = estimate && Number.isFinite(call.context_window) && Number.isFinite(call.context_used) ?
      ' · 추정 여유 ' + metric(Math.max(0, call.context_window - call.context_used)) : '';
    row(context, who + ' · ' + occupancy + metric(call.context_used) + ' / 창 크기 ' + metric(call.context_window) + remaining,
      call.source, call.observed_at);
  }
  const quota = section('공급자 사용 한도');
  if (!usage.rate_limits?.length) quota.append(make('p', '공급자가 보고한 한도와 재설정 시각 미확인'));
  for (const limit of usage.rate_limits || []) row(quota,
    [limit.engine, limit.host, limit.account_alias || '계정 별칭 미확인', limit.name || limit.limit_id, '사용 ' + metric(limit.used_percent, '%'),
      '집계 창 ' + metric(limit.window_minutes, '분'), '재설정 ' + fmtDate(limit.resets_at),
      limit.limit_reached ? '한도 도달' : '', limit.spend_control_reached ? '지출 한도 도달' : '',
      limit.ordinary_usage_allowed == null ? '' : (limit.ordinary_usage_allowed ? '일반 호출 허용' : '일반 호출 제한')].filter(Boolean).join(' · '),
    limit.source, limit.observed_at);
  const limits = section('밤샘 목표 예산');
  limits.append(make('p', '실제 작업 ' + metric(budget.active_used, '초') + ' / ' + metric(budget.active_seconds, '초') +
    ' · 시도 ' + metric(budget.attempts_used, '회') + ' / ' + metric(budget.max_attempts, '회') + ' · 토큰 한도 ' + metric(budget.token_budget)));
  if (Number.isFinite(budget.active_seconds) && Number.isFinite(budget.max_attempts)) {
    const remaining = (limit, used, unit) => Number.isFinite(used) ? metric(Math.max(0,limit-used),unit) : unknown;
    const tokensLeft = budget.token_budget == null ? '한도 미설정' : usage.usage_coverage === 'complete' ?
      remaining(budget.token_budget,usage.total_tokens,'개') : unknown;
    limits.append(make('p', '남은 예산 · 작업 ' + remaining(budget.active_seconds,budget.active_used,'초') +
      ' · 시도 ' + remaining(budget.max_attempts,budget.attempts_used,'회') + ' · 토큰 ' + tokensLeft));
  }
  if (usage.hold) limits.append(make('p', [usageActions[usage.hold.kind] || '한도 조치', usage.hold.stage, usage.hold.reason,
    usage.hold.resume_condition, fmtDate(usage.hold.at)].filter(Boolean).join(' · ')));
  for (const entry of usage.decisions || []) limits.append(make('p', [usageActions[entry.action] || entry.action, roleNames[entry.role] || entry.role,
    entry.attempt == null ? null : entry.attempt + '차', entry.reason, entry.stage, fmtDate(entry.at)].filter(Boolean).join(' · ')));
  return root;
}

export function mountCharacterRecords({get, make, api, context, openEvidence}) {
  let characterRole, charRead = 0, usageRead = 0, selectedKey, usageKey;
  const char = get('character-sheet'), usage = get('work-usage');
  function portrait(role) {
    ++charRead;
    characterRole = role;
    char.open = false;
    char.hidden = !context().supported || !Object.hasOwn(roleNames, role);
    get('character-record').replaceChildren(); get('character-note').textContent = '';
  }
  async function loadCharacter() {
    if (char.hidden || !char.open) return;
    const ticket = ++charRead, {identity, workspaceId} = context(), role = characterRole;
    const current = () => ticket === charRead && identity === context().identity && char.open;
    const record = get('character-record'), note = get('character-note');
    note.textContent = '작업 근거를 불러오는 중입니다.';
    try {
      const data = await api('/rooms/' + workspaceId + '/characters/' + role);
      if (!current()) return;
      const person = data.character;
      if (data.workspace_id !== workspaceId || person?.role !== role || person.axes?.length !== 6) throw Error();
      const description = make('p', [person.name, person.specialty, '최근 ' + metric(person.days, '일'),
        person.run_limit ? '최근 배정 최대 ' + metric(person.run_limit, '건') : ''].filter(Boolean).join(' · '));
      const group = make('div', '', 'character-grid');
      const list = make('ul', '', 'character-axes');
      for (const axis of person.axes) {
        const item = make('li');
        const facts = (axis.evidence || []).filter(fact => safeId(fact.run_id));
        const fold = facts.length ? make('details') : null;
        const label = fold ? make('summary') : item;
        label.append(make('strong', axis.label + ' · ' + (axis.level == null ? '자료 부족' : metric(axis.level) + '/5')),
          make('small', '표본 ' + metric(axis.samples, '건')));
        if (fold) { fold.append(label); item.append(fold); }
        for (const fact of facts) {
          const link = make('button', [fact.task, fact.label || '작업 근거 보기'].filter(Boolean).join(' · '), 'brief-action');
          link.type = 'button';
          link.dataset.run = fact.run_id;
          link.dataset.section = fact.section;
          link.addEventListener('click', () => openEvidence(fact));
          fold.append(link);
        }
        list.append(item);
      }
      group.append(radar(record.ownerDocument, person.axes), list);
      const similar = make('details');
      similar.append(make('summary', '유사 작업의 엔진·모델별 기록'));
      for (const row of person.comparisons || []) {
        const average = (value, unit) => Number.isFinite(value) && row.samples > 0 ? metric(Math.round(value / row.samples), unit) : unknown;
        similar.append(make('p', [groupNames[row.group] || row.group,
          row.engine, row.model || '모델 미확인', row.effort || '강도 미확인', '표본 ' + metric(row.samples, '건'), '채택 ' + metric(row.adopted, '건'),
          '재작업 ' + metric(row.rework, '건'), '목표당 평균 작업 ' + average(row.active_seconds, '초'),
          '목표당 평균 역할 토큰 ' + average(row.tokens, '개')].filter(Boolean).join(' · ')));
      }
      if (!person.comparisons?.length) similar.append(make('p', '비교할 작업 기록이 없습니다.'));
      const reason = context().assignment?.work_profile?.role_reasons?.[role];
      record.replaceChildren(description, ...(reason ? [make('p', '이번 작업 배정 근거 · ' + reason)] : []), group, similar);
      note.textContent = '';
    } catch { if (current()) { record.replaceChildren(); note.textContent = '작업 근거를 확인하지 못했습니다. 닫았다가 다시 열어 주세요.'; } }
  }
  async function loadUsage() {
    if (usage.hidden || !usage.open) return;
    const {workspaceId, runId, identity} = context(), ticket = ++usageRead;
    const current = () => ticket === usageRead && context().identity === identity && context().runId === runId && usage.open;
    try {
      const result = await api('/rooms/' + workspaceId + '/usage/' + runId);
      if (!current()) return;
      if (result.workspace_id !== workspaceId || result.run_id !== runId || !result.usage) throw Error();
      const key = JSON.stringify(result) + Math.floor(Date.now() / 60000);
      if (key !== usageKey) {
        get('usage-record').replaceChildren(usageView(make, result));
        usageKey = key;
      }
      get('usage-note').textContent = '';
    } catch { if (current()) get('usage-note').textContent = '최신 사용량을 확인할 수 없습니다.'; }
  }
  function controls() {
    const {supported, identity, runId} = context();
    const key = supported && safeId(runId) ? identity + ':' + runId : '';
    usage.hidden = !key;
    if (key !== selectedKey) {
      ++usageRead; selectedKey = key; usageKey = null; usage.open = false;
      get('usage-record').replaceChildren(); get('usage-note').textContent = '사용량을 불러오는 중입니다.';
    }
  }
  char.addEventListener('toggle', loadCharacter);
  usage.addEventListener('toggle', loadUsage);
  return {portrait, controls, loadUsage};
}
