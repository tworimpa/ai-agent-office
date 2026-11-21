const roleNames = {developer: '담당자', reviewer: '검토자'};
export const modes = {auto: '자동', quick: '빠른 확인', balanced: '균형 개발', deep: '심층 검토'};
export const usageActions = {quota_wait: '공급자 한도 대기', quota_stop: '공급자 한도로 중지', budget_stop: '목표 예산으로 중지',
  fallback: '대체 엔진·모델로 전환', resumed: '재개', retry_effort:'다음 시도의 추론 강도 변경', retry_unchanged:'재작업 조합 유지'};
const engines = {codex: 'Codex', antigravity: 'Antigravity', claude: 'Claude Code', gemini: 'Gemini CLI', cline: 'Cline CLI'};
const capability = {supported: '지원', unknown: '미확인', unsupported: '미지원'};

function choices(node, values, selected = '') {
  const doc = node.ownerDocument;
  node.replaceChildren(...values.map(([value, text]) => {
    const item = doc.createElement('option');
    item.value = value;
    item.textContent = text;
    return item;
  }));
  node.value = selected;
}

function control(make, parent, id, title, tag) {
  const label = make('label', title);
  const input = make(tag);
  label.htmlFor = input.id = id;
  parent.append(label, input);
  return input;
}

function editor({get, make, prefix, roleEngine, refreshModels}) {
  const intake = prefix === 'assignment';
  const modeRoot = get(prefix + '-work-mode');
  const fields = get(intake ? 'assignment-profile-fields' : 'policy-fields');
  const mode = control(make, modeRoot, prefix + '-mode', '작업 방식', 'select');
  const blank = intake ? '작업실 기본값' : '자동 선택';
  choices(mode, [...(intake ? [['', '작업실 기본값']] : []), ...Object.entries(modes)], intake ? '' : 'auto');
  const catalogNote = make('p', '', 'field-help');
  const refresh = make('button', '호스트에서 모델 목록 갱신', 'secondary');
  refresh.type = 'button';
  refresh.addEventListener('click', refreshModels);
  fields.append(catalogNote, refresh);
  let hostModels = null, serial = 0;
  const roleInputs = {}, fallbackInputs = {};

  function modelFields(parent, id, engine, saved = {}) {
    const model = control(make, parent, id + '-model', '모델', 'input');
    const suggestions = make('datalist');
    suggestions.id = id + '-models';
    model.setAttribute('list', suggestions.id);
    model.autocapitalize = 'none';
    model.spellcheck = false;
    model.maxLength = 128;
    model.placeholder = blank;
    const effort = control(make, parent, id + '-effort', '추론 강도', 'select');
    const alias = control(make, parent, id + '-alias', '사용량 계정 별칭 (선택)', 'input');
    alias.maxLength = 64;
    alias.placeholder = '호스트에서 쓰는 식별 이름';
    const support = make('p', '', 'field-help');
    parent.append(suggestions, support);
    function options() {
      const caps = hostModels?.capabilities?.[engine()] || {};
      const catalog = hostModels?.catalog && hostModels.catalog.engine === engine() ? hostModels.catalog.models || [] : [];
      suggestions.replaceChildren(...catalog.map(row => {
        const item = make('option', row.label || row.id);
        item.value = row.id;
        return item;
      }));
      const selected = catalog.find(row => row.id === model.value) || (!model.value && catalog.find(row => row.is_default));
      const current = effort.value;
      const allowed = selected?.efforts || (['claude', 'antigravity'].includes(engine()) && caps.effort === 'supported' ?
        ['low', 'medium', 'high', 'xhigh', 'max'] : []);
      const levels = [...new Set([...allowed, ...(current ? [current] : [])])];
      choices(effort, [['', blank], ...levels.map(value => [value, value])], current);
      model.disabled = caps.model !== 'supported';
      effort.disabled = caps.effort !== 'supported' || !levels.length;
      support.textContent = [engines[engine()], ...Object.entries({model:'모델', effort:'추론 강도', usage:'사용량', limits:'한도'}).map(
        ([key, label]) => label + ' ' + (capability[caps[key]] || '미확인'))].join(' · ');
    }
    function load(value = {}) {
      model.value = value.model || '';
      choices(effort, value.effort ? [[value.effort, value.effort]] : [['', blank]], value.effort || '');
      alias.value = value.account_alias || '';
      options();
    }
    function value() {
      return Object.fromEntries(Object.entries({model: model.value.trim(), effort: effort.value, account_alias: alias.value.trim()})
        .filter(([, field]) => field !== ''));
    }
    model.addEventListener('input', () => { effort.value = ''; options(); });
    load(saved);
    return {options, load, value};
  }

  for (const [role, name] of Object.entries(roleNames)) {
    const group = make('fieldset', '', 'work-fields');
    group.append(make('legend', name));
    fields.append(group);
    const engine = () => roleEngine(role, intake);
    roleInputs[role] = modelFields(group, prefix + '-' + role, engine);
    if (intake) get('assignment-' + role + '-engine').addEventListener('change', () => roleInputs[role].load());
  }
  const goals = make('fieldset', '', 'work-fields');
  goals.append(make('legend', '밤샘 목표 예산과 한도'));
  fields.append(goals);
  const budget = control(make, goals, prefix + '-budget', '목표 전체 토큰 예산', 'input');
  budget.type = 'number'; budget.min = '1'; budget.max = '1000000000000'; budget.step = '1';
  budget.placeholder = intake ? '작업실 기본값' : '미지정';
  const onLimit = control(make, goals, prefix + '-on-limit', '한도에 도달하면', 'select');
  choices(onLimit, [...(intake ? [['', blank]] : []), ['wait','대기'], ['stop','중지']], intake ? '' : 'wait');
  const retry = control(make, goals, prefix + '-retry-deep', '같은 검사 실패에 새 가설이 기록된 다음 개발 시도', 'select');
  choices(retry, [...(intake ? [['', blank]] : []), ['keep','지정한 추론 강도 유지'], ['deep','Codex의 검증된 심층 추론 1회 허용']], intake ? '' : 'keep');
  for (const [role, name] of Object.entries(roleNames)) {
    const group = make('fieldset', '', 'work-fields');
    group.append(make('legend', name + ' 대체 순서'));
    goals.append(group);
    let override;
    if (intake) {
      override = control(make, group, prefix + '-' + role + '-override', '이 목표에 적용할 대체 순서', 'select');
      choices(override, [['', blank], ['custom', '직접 지정 (빈 목록은 대체 없음)']]);
    }
    const rows = make('ol', '', 'fallback-list'), add = make('button', '대체 엔진 추가', 'secondary');
    add.type = 'button';
    group.append(rows, add);
    const records = new Map();
    function show() { rows.hidden = add.hidden = intake && !override.value; }
    function append(saved = {}) {
      const row = make('li');
      const id = prefix + '-fallback-' + (++serial);
      const engine = control(make, row, id + '-engine', '대체 엔진', 'select');
      choices(engine, Object.entries(engines), saved.engine || 'codex');
      const inputs = modelFields(row, id, () => engine.value, saved);
      engine.addEventListener('change', () => inputs.load());
      const remove = make('button', '목록에서 제거', 'secondary');
      remove.type = 'button';
      remove.addEventListener('click', () => { records.delete(row); row.remove(); add.disabled = false; add.focus(); });
      row.append(remove);
      rows.append(row);
      records.set(row, {engine, inputs});
      add.disabled = records.size >= 4;
      return engine;
    }
    add.addEventListener('click', () => { if (records.size < 4) append().focus(); });
    override?.addEventListener('change', show);
    fallbackInputs[role] = {
      load(values) {
        records.clear(); rows.replaceChildren();
        add.disabled = false;
        if (intake) override.value = values === undefined ? '' : 'custom';
        for (const row of values || []) append(row);
        show();
      },
      options() { for (const {inputs} of records.values()) inputs.options(); },
      value() {
        if (intake && !override.value) return undefined;
        return [...records.values()].map(({engine, inputs}) => ({engine:engine.value, ...inputs.value()}));
      }
    };
  }
  function load(data = {}) {
    mode.value = data.mode || (intake ? '' : 'auto');
    budget.value = data.token_budget ?? '';
    onLimit.value = data.on_limit || (intake ? '' : 'wait');
    retry.value = data.retry_deep == null && intake ? '' : data.retry_deep ? 'deep' : 'keep';
    for (const role of Object.keys(roleNames)) {
      roleInputs[role].load(data.roles?.[role]);
      fallbackInputs[role].load(data.fallbacks?.[role]);
    }
  }
  function value() {
    const result = {}, roles = {}, fallbacks = {};
    if (mode.value) result.mode = mode.value;
    for (const role of Object.keys(roleNames)) {
      const prefs = roleInputs[role].value();
      if (!intake || Object.keys(prefs).length) roles[role] = prefs;
      const list = fallbackInputs[role].value();
      if (list !== undefined) fallbacks[role] = list;
    }
    if (Object.keys(roles).length) result.roles = roles;
    if (!goals.hidden) {
      if (Object.keys(fallbacks).length) result.fallbacks = fallbacks;
      if (onLimit.value) result.on_limit = onLimit.value;
      if (retry.value === 'deep' || intake && retry.value === 'keep') result.retry_deep = retry.value === 'deep';
      if (budget.value || !intake) result.token_budget = budget.value ? Number(budget.value) : null;
      if (budget.validity.badInput || (budget.value && !(Number.isSafeInteger(result.token_budget) && result.token_budget > 0))) {
        throw Error('토큰 예산을 1 이상의 정수로 입력하세요.');
      }
    }
    return result;
  }
  function catalog(data) {
    hostModels = data;
    const info = data?.catalog;
    catalogNote.textContent = !info ? '호스트 모델 목록 미확인' :
      ['호스트 모델 목록', info.status, info.refreshed_at ? new Date(info.refreshed_at).toLocaleString('ko-KR') : '조회 시각 미확인'].join(' · ');
    for (const role of Object.keys(roleNames)) { roleInputs[role].options(); fallbackInputs[role].options(); }
  }
  return {load, value, catalog, controls(visible, locked, goal) {
    fields.hidden = modeRoot.hidden = !visible;
    fields.disabled = mode.disabled = locked;
    goals.hidden = goals.disabled = !goal;
  }, inherited(policy) {
    if (intake) mode.options[0].textContent = '작업실 기본값' + (modes[policy?.mode] ? ' · ' + modes[policy.mode] : '');
  }};
}

export function mountWorkProfiles({get, make, api, context}) {
  const path = () => '/rooms/' + context().workspaceId;
  const roleEngine = (role, intake) => (intake && get('assignment-' + role + '-engine').value) || context().settings?.[role]?.engine;
  let version = 0, catalogVersion = 0, loaded = false, saving = false;
  let active = null;
  const settings = editor({get, make, prefix: 'policy', roleEngine, refreshModels: () => models(settings, true)});
  const intake = editor({get, make, prefix: 'assignment', roleEngine, refreshModels: () => models(intake, true)});
  async function models(target, update) {
    const ticket = ++catalogVersion, identity = context().identity;
    try {
      const response = await api(path() + '/models', update ? {} : undefined);
      if (ticket === catalogVersion && identity === context().identity && response.workspace_id === context().workspaceId) target.catalog(response);
    } catch {
      if (ticket === catalogVersion && identity === context().identity) target.catalog(null);
    }
  }
  function start(target, existing) {
    ++version;
    active = target;
    loaded = saving = false;
    get('policy-note').textContent = '';
    get('policy-save').disabled = true;
    if (!context().supported) return;
    const seq = version, identity = context().identity;
    target.load(existing);
    target.catalog(null);
    target.inherited(null);
    models(target, false);
    if (target === settings) get('policy-note').textContent = '작업실 기본값을 불러오는 중입니다.';
    api(path() + '/work-policy').then(response => {
      if (seq !== version || identity !== context().identity) return;
      if (!response.policy || response.workspace_id !== context().workspaceId) throw Error();
      if (target === intake) { intake.inherited(response.policy); return; }
      settings.load(response.policy);
      loaded = true;
      get('policy-note').textContent = '';
      controls();
    }).catch(() => {
      if (seq === version && target === settings) get('policy-note').textContent = '작업실 기본값을 불러오지 못했습니다. 설정을 다시 열어 주세요.';
    });
  }
  function controls() {
    if (active === settings) {
      const enabled = context().supported && context().writable && loaded && !saving;
      settings.controls(context().supported, !enabled, true);
      get('policy-save').disabled = !enabled;
    }
  }
  get('work-policy-form').addEventListener('submit', async event => {
    event.preventDefault();
    if (active !== settings || !context().writable || !loaded || saving) return;
    const seq = version, identity = context().identity;
    try {
      const payload = settings.value();
      saving = true; controls();
      const response = await api(path() + '/work-policy', payload);
      if (seq !== version || identity !== context().identity) return;
      if (!response.policy || response.workspace_id !== context().workspaceId) throw Error('저장 결과를 확인할 수 없습니다.');
      settings.load(response.policy);
      get('policy-note').textContent = '다음 작업에 적용할 기본값을 저장했습니다.';
    } catch (error) {
      if (seq === version && identity === context().identity) get('policy-note').textContent = error.message;
    } finally { if (seq === version) { saving = false; controls(); } }
  });
  return {
    settings() {
      get('work-policy').hidden = !context().supported;
      get('work-policy').open = false;
      start(settings);
      controls();
    },
    intake(saved) { start(intake, saved); },
    controls,
    intakeControls(assignment, locked, goal) { intake.controls(context().supported && assignment, locked, goal); },
    value() { return context().supported ? intake.value() : {}; }
  };
}
