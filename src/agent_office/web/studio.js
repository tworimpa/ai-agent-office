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
const checkNote = row => row.delegation_reason ?? (row.delegation_in_progress ?
  '전문가가 근거를 확인하고 있습니다. 지금 직접 답할 수 있습니다.' : '');
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
const engineNames = new Map([
  ['codex', 'Codex'], ['antigravity', 'Antigravity'], ['claude', 'Claude Code'],
  ['gemini', 'Gemini CLI'], ['cline', 'Cline CLI'],
]);
const roles = [['developer', '개발자', 'codex'], ['reviewer', '리뷰어', 'antigravity']];
const delegationRoles = [['specialist', '전문가', 'codex'], ['verifier', '독립 검증자', 'antigravity']];
const topics = [
  ['test_command', '이 프로젝트의 테스트 명령은 무엇인가요?', 'README.md'],
  ['python_version', '이 프로젝트가 요구하는 Python 버전은 무엇인가요?', 'pyproject.toml'],
  ['node_version', '이 프로젝트가 요구하는 Node.js 버전은 무엇인가요?', 'package.json'],
];
const capabilityLabels = [['read', '읽기'], ['edit', '수정'], ['review', '리뷰']];
const inboxStates = {pending: '응답 대기', decided: '결정 접수', delivered: '작업자에게 전달됨', expired: '만료됨'};
const requestLabels = {question: '기술 질문', product_decision: '제품 동작 결정', permission: '도구 권한 요청'};
const choicePurposes = {implementation: '기존 구현', diagnosis: '진단', validation: '검증'};
const decisionLabels = {answer: '답변', approve: '이번 요청 승인', reject: '거절'};
const inboxMode = run => run.use_inbox === true ? '협업 요청함 사용' : '협업 요청함 꺼짐';
const inboxPath = id => '/runs/' + id + '/inbox';
const requestKey = row => row.run_id + '/' + row.id;

const sameList = (left, right) => Array.isArray(left) && Array.isArray(right) &&
  left.length === right.length && left.every((value, i) => value === right[i]);
const sameSkills = (left, right) => [...roles, ...delegationRoles].every(([role]) =>
  sameList(left?.[role] ?? [], right?.[role] ?? []));

function decisionLabel(row) {
  const action = decisionLabels[row.decision];
  return [inboxStates[row.status] ?? '상태 미확인', row.shared_decision && '선행 목표의 사용자 결정 적용', row.source === 'delegate' && '대리 결정', action,
    row.decision === 'answer' && row.answer].filter(Boolean).join(' · ');
}

function samePolicy(expected, received) {
  if (!expected || !received) return !expected && !received;
  return Object.keys(received).length === 3 && expected.specialist_engine === received.specialist_engine &&
    expected.verifier_engine === received.verifier_engine && Array.isArray(expected.rules) && Array.isArray(received.rules) &&
    received.rules.length === expected.rules.length && expected.rules.every(rule =>
      received.rules.some(item => item && Object.keys(item).length === Object.keys(rule).length &&
        item.verification === rule.verification && item.topic === rule.topic && item.path === rule.path));
}
const checkedTime = new Intl.DateTimeFormat('ko-KR', {dateStyle: 'short', timeStyle: 'medium'});
function timeLabel(value) {
  const stamp = typeof value === 'string' ? Date.parse(value) : NaN;
  return Number.isNaN(stamp) ? '' : checkedTime.format(stamp);
}

function engineMetadata(row) {
  const installation = row.installed === true ? '설치 확인' : row.installed === false ? '실행 파일 없음' : '설치 미확인';
  const auth = {authenticated: '로그인 확인', not_authenticated: '로그인 안 됨'}[row.auth_status] ?? '로그인 미확인';
  const probe = {pending: '진단 중', timeout: '진단 시간 초과', error: '진단 오류', unsupported: '필수 CLI 옵션 미지원',
    unknown_version: 'CLI 버전 확인 실패', unsupported_version: '작업실에서 지원하지 않는 버전'};
  const options = row.headless_support === true ? '비대화형 옵션 확인' : '비대화형 옵션 미확인';
  const protocol = row.protocol ? '프로토콜 ' + row.protocol : '';
  return [installation, row.version || '버전 미확인', auth, protocol, probe[row.probe_status] ?? options].filter(Boolean).join(' · ');
}
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
  if (validId(hostId)) {
    get('versions-link').href = '/versions?host=' + hostId;
    get('office-link').href = '/office?host=' + hostId;
    get('improvements-link').href = '/improvements?host=' + hostId;
  }
  const pendingKey = hostId === 'local' ? 'agent-office.studio.pending' : 'agent-office.studio.pending.' + hostId;
  let host = null, hosts = [], online = false, registering = false;
  let selected = validId(fromAddress()) ? fromAddress() : null;
  let workspaces = [];
  let runs = [];
  let listSequence = 0, detailSequence = 0, catalogSequence = 0;
  let timer, closed = false, starting = false, restoring = true;
  let engineSnapshot = null, engineRequest = null, enginesStale = true;
  const stopping = new Set();
  const endingDelegation = new Set();
  let returnReport = null, reportAvailable = false, reportRequest = null;
  let detailPolicy = null, detailDelegationActive = false;
  const deciding = new Set(), unconfirmed = new Map();
  let pendingRows = [], historySnapshot = null, inboxAvailable = false;
  let pending = null;
  let skillCatalog = new Map(), skillCursor = null, skillsAvailable = false, catalogBusy = false;
  let savingSkill = false, importSequence = 0, skillReadSequence = 0;
  let subtaskNumber = 0;
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
  const selectedIndices = () => [Number(get('primary').value), ...checkedExtras()];
  const skillRoles = [...roles, ...delegationRoles];
  const skillLabel = item => item.name + ' · v' + item.version;
  const assigned = new Map(skillRoles.map(([role]) => [role, new Set(pending?.role_skills?.[role] ?? [])]));
  const chosenSkills = () => Object.fromEntries([...assigned].filter(([, ids]) => ids.size).map(([role, ids]) => [role, [...ids]]));
  const hasSkills = () => [...assigned.values()].some(ids => ids.size);
  const bytes = value => new TextEncoder().encode(value).length;

  function renderSkillCatalog() {
    const inspect = get('inspect-skill'), previous = inspect.value;
    inspect.replaceChildren(option('', '버전을 선택하세요'), ...[...skillCatalog.values()].map(item => option(item.id, skillLabel(item))));
    inspect.value = previous;
    get('older-skills').hidden = !skillCursor;
    get('role-skills').replaceChildren(...skillRoles.map(([role, title]) => {
      const group = make('fieldset'), legend = make('legend'), choices = make('div');
      group.dataset.role = role;
      legend.textContent = title;
      choices.className = 'checklist';
      const picked = assigned.get(role);
      for (const id of new Set([...skillCatalog.keys(), ...picked])) {
        const label = make('label'), box = make('input');
        box.type = 'checkbox';
        box.name = role + '_skill';
        box.value = id;
        box.checked = picked.has(id);
        label.className = 'check';
        label.append(box, skillCatalog.has(id) ? skillLabel(skillCatalog.get(id)) : '선택한 버전 ' + id);
        box.addEventListener('change', () => {
          if (box.checked) picked.add(id);
          else picked.delete(id);
          controls();
        });
        choices.append(label);
      }
      if (!choices.children.length) choices.textContent = '등록된 버전이 없습니다.';
      group.append(legend, choices);
      return group;
    }));
  }

  async function loadSkills(older = false) {
    if (catalogBusy || (older && !skillCursor)) return;
    catalogBusy = true;
    get('older-skills').disabled = true;
    try {
      const page = await api(older ? '/skills/before/' + skillCursor : '/skills');
      if (closed || hostId !== addressHost()) return;
      if (!Array.isArray(page.skills) || page.skills.some(item => !validId(item?.id) ||
          typeof item.name !== 'string' || !Number.isInteger(item.version)) ||
          (page.next_cursor != null && !validId(page.next_cursor))) throw new Error('스킬 목록 형식 오류');
      // Keep the loaded tail, including selections, when polling brings new versions to the front.
      const map = new Map((older ? [...skillCatalog.values(), ...page.skills] : [...page.skills, ...skillCatalog.values()]).map(item => [item.id, item]));
      const changed = JSON.stringify([...map]) !== JSON.stringify([...skillCatalog]);
      if (older || !skillCatalog.size || (page.next_cursor && !skillCatalog.has(page.next_cursor))) {
        skillCursor = page.next_cursor ?? null;
      }
      skillCatalog = map;
      if (changed || !get('role-skills').children.length) renderSkillCatalog();
      get('older-skills').hidden = !skillCursor;
      skillsAvailable = true;
      say('skill-status', host.name + ' · 불러온 버전 ' + map.size + '개');
    } catch {
      if (!closed) {
        skillsAvailable = false;
        say('skill-status', '이 호스트의 스킬 목록을 확인할 수 없습니다.');
      }
    } finally {
      catalogBusy = false;
      get('older-skills').disabled = !skillsAvailable;
      controls();
    }
  }

  get('inspect-skill').addEventListener('change', async () => {
    const id = get('inspect-skill').value, sequence = ++skillReadSequence;
    get('skill-preview').hidden = !id;
    if (!id) return;
    say('skill-preview', '지침을 불러오는 중입니다.');
    try {
      const {skill} = await api('/skills/' + id);
      if (closed || sequence !== skillReadSequence) return;
      if (skill?.id !== id || typeof skill.instructions !== 'string') throw new Error();
      say('skill-preview', skill.instructions);
    } catch {
      if (!closed && sequence === skillReadSequence) say('skill-preview', '선택한 버전의 지침을 확인할 수 없습니다.');
    }
  });
  get('older-skills').addEventListener('click', () => loadSkills(true));
  get('skill-file').addEventListener('change', async () => {
    const file = get('skill-file').files[0], sequence = ++importSequence;
    if (!file) return;
    if (file.size > 8192) return say('skill-note', '지침 파일은 8,192바이트 이하로 선택하세요.');
    try {
      const instructions = await file.text();
      if (closed || sequence !== importSequence) return;
      get('skill-text').value = instructions;
      if (!get('skill-name').value) get('skill-name').value = file.name.replace(/\.(md|markdown)$/i, '');
      say('skill-note', '파일에서 지침을 가져왔습니다. 내용을 확인하고 등록하세요.');
    } catch { if (sequence === importSequence) say('skill-note', '파일을 읽을 수 없습니다.'); }
  });
  get('skill-text').addEventListener('input', () => { ++importSequence; });
  get('skill-form').addEventListener('submit', async event => {
    event.preventDefault();
    if (get('skill-entry').disabled) return;
    const name = get('skill-name').value.trim(), instructions = get('skill-text').value;
    if (!name || bytes(name) > 128 || !instructions.trim() || bytes(instructions) > 8192) {
      return say('skill-note', '이름은 128바이트, 지침은 8,192바이트 안에서 입력하세요.');
    }
    savingSkill = true;
    ++importSequence;
    controls();
    try {
      const {skill} = await api('/skills', {name, instructions});
      if (closed) return;
      if (!validId(skill?.id) || skill.name !== name || !Number.isInteger(skill.version)) throw new Error();
      skillCatalog = new Map([[skill.id, skill], ...skillCatalog]);
      renderSkillCatalog();
      get('skill-form').reset();
      say('skill-note', skillLabel(skill) + ' 등록을 확인했습니다.');
    } catch (error) {
      say('skill-note', error.status ? error.message : '등록 결과를 확인하지 못했습니다. 호스트의 버전 목록을 새로고침하세요.');
    } finally { savingSkill = false; controls(); }
  });

  function renderSubtaskFolders(group, picked) {
    const choices = group.querySelector('.subtask-folders');
    const available = workspace() ? selectedIndices() : picked;
    choices.replaceChildren(...available.map(index => {
      const label = make('label'), box = make('input');
      label.className = 'check';
      box.type = 'checkbox';
      box.name = group.id + '-folder';
      box.value = String(index);
      box.checked = picked.includes(index);
      label.append(box, workspace()?.folders[index] ?? ('원래 폴더 번호 ' + (index + 1)));
      return label;
    }));
  }

  function updateSubtaskFolders(clear = false) {
    for (const group of get('subtasks').children) {
      const checked = [...group.querySelectorAll('.subtask-folders :checked')].map(box => Number(box.value));
      renderSubtaskFolders(group, clear ? [] : checked);
    }
  }

  function updateSubtaskNumbers() {
    const groups = [...get('subtasks').children];
    groups.forEach((group, index) => {
      group.firstChild.textContent = '하위 작업 ' + (index + 1);
      group.querySelector('button').disabled = groups.length <= 2;
    });
    get('add-subtask').disabled = groups.length >= 4;
  }

  function addSubtask(draft) {
    const group = make('fieldset'), legend = make('legend');
    group.id = 'subtask-' + ++subtaskNumber;
    group.className = 'subtask';
    const task = make('textarea'), label = make('label');
    task.id = task.name = group.id + '-task';
    task.required = true;
    task.rows = 2;
    task.value = draft?.task ?? '';
    label.htmlFor = task.id;
    label.textContent = '독립 작업 요청 (필수)';
    const folders = make('fieldset'), folderTitle = make('legend'), choices = make('div');
    folderTitle.textContent = '담당 폴더 (하나 이상)';
    choices.className = 'subtask-folders checklist';
    folders.append(folderTitle, choices);
    const engines = make('div');
    engines.className = 'role-pickers';
    for (const [role, title] of roles) {
      const field = make('div'), caption = make('label'), picker = make('select');
      picker.id = picker.name = group.id + '-' + role;
      picker.dataset.role = role;
      picker.required = true;
      caption.htmlFor = picker.id;
      caption.textContent = title + ' 엔진';
      picker.replaceChildren(...Array.from(engineNames, ([id, name]) => option(id, name)));
      picker.value = draft?.[role + '_engine'] ?? get(role + '-engine').value;
      picker.addEventListener('change', updateEngineWarning);
      field.append(caption, picker);
      engines.append(field);
    }
    const remove = make('button');
    remove.type = 'button';
    remove.className = 'link-button';
    remove.textContent = '이 하위 작업 빼기';
    remove.addEventListener('click', () => {
      group.remove();
      updateSubtaskNumbers();
      updateEngineWarning();
      get('add-subtask').focus();
    });
    group.append(legend, label, task, folders, engines, remove);
    get('subtasks').append(group);
    renderSubtaskFolders(group, draft?.indices ?? [Number(get('primary').value)]);
    updateSubtaskNumbers();
    updateEngineWarning();
    return task;
  }

  function readSubtasks() {
    return [...get('subtasks').children].map(group => ({
      task: group.querySelector('textarea').value.trim(),
      indices: [...group.querySelectorAll('.subtask-folders :checked')].map(box => Number(box.value)),
      developer_engine: group.querySelector('[data-role=developer]').value,
      reviewer_engine: group.querySelector('[data-role=reviewer]').value,
    }));
  }
  get('add-subtask').addEventListener('click', () => { if (!get('add-subtask').disabled) addSubtask().focus(); });
  get('use-parallel').addEventListener('change', () => {
    if (get('use-parallel').checked && !get('subtasks').children.length) { addSubtask(); addSubtask(); }
    controls();
    updateEngineWarning();
  });
  get('extras').addEventListener('change', () => updateSubtaskFolders());
  renderSkillCatalog();

  function updateEngineWarning() {
    const warnings = [];
    const pickers = get('use-parallel').checked ? [...get('subtasks').children].flatMap((group, i) =>
      roles.map(([role, title]) => ['하위 작업 ' + (i + 1) + ' ' + title, group.querySelector('[data-role=' + role + ']').value])) :
      roles.map(([role, title]) => [title, get(role + '-engine').value]);
    if (get('use-delegation').checked && get('independent-facts').checked) {
      pickers.push(...delegationRoles.map(([role, title]) => [title, get(role + '-engine').value]));
    }
    for (const [title, id] of pickers) {
      const row = engineSnapshot?.engines.find(item => item.engine === id);
      const roleName = title + ' ' + engineNames.get(id);
      if (row?.installed === false) warnings.push(roleName + ' 실행 파일을 찾지 못했습니다.');
      else if (row?.probe_status === 'unsupported_version') {
        warnings.push(roleName + '의 설치 버전을 작업실에서 지원하지 않습니다.');
      }
    }
    say('engine-warning', enginesStale ? '이 호스트의 최신 엔진 진단을 확인할 수 없습니다.' :
      warnings.join(' '));
  }

  for (const [role, , defaultId] of [...roles, ...delegationRoles]) {
    const picker = get(role + '-engine');
    picker.replaceChildren(...Array.from(engineNames, ([id, name]) => option(id, name)));
    picker.value = pending?.delegation?.[role + '_engine'] ?? pending?.[role + '_engine'] ?? defaultId;
    picker.addEventListener('change', updateEngineWarning);
  }

  for (const [topic, prompt, path] of topics) {
    const group = make('div'), label = make('label'), box = make('input');
    group.className = 'delegation-topic';
    box.type = 'checkbox';
    box.id = 'topic-' + topic;
    box.name = 'delegation_topic';
    box.value = topic;
    label.htmlFor = box.id;
    label.className = 'check';
    label.append(box, doc.createTextNode(prompt));
    const entry = make('div'), pathLabel = make('label'), input = make('input');
    entry.className = 'evidence-path';
    input.type = 'text';
    input.id = 'path-' + topic;
    input.name = 'path_' + topic;
    input.required = true;
    input.spellcheck = false;
    input.maxLength = 240;
    input.autocapitalize = 'none';
    pathLabel.htmlFor = input.id;
    pathLabel.textContent = '근거 파일 경로 (필수 · ' + (topic === 'test_command' ? 'README.md 또는 AGENTS.md' : path) + ')';
    const saved = pending?.delegation?.rules?.find(item => item.topic === topic);
    box.checked = Boolean(saved);
    input.value = saved?.path ?? path;
    const showPath = () => { entry.hidden = input.disabled = !box.checked; };
    showPath();
    box.addEventListener('change', showPath);
    entry.append(pathLabel, input);
    group.append(label, entry);
    get('delegation-topics').append(group);
  }
  get('use-delegation').checked = Boolean(pending?.delegation);

  function resetDelegation() {
    get('independent-facts').checked = false;
    get('use-delegation').checked = false;
    for (const [topic, , path] of topics) {
      const box = get('topic-' + topic), input = get('path-' + topic);
      box.checked = false;
      input.value = path;
      input.parentElement.hidden = input.disabled = true;
    }
    for (const [role, , engine] of delegationRoles) get(role + '-engine').value = engine;
    updateEngineWarning();
  }

  function renderEngineRows(snapshot) {
    get('engine-list').replaceChildren(...Array.from(engineNames, ([id, name]) => {
      const row = snapshot.engines.find(item => item.engine === id) ?? {};
      const li = make('li'), title = make('strong'), metadata = make('p'), checks = make('p');
      title.textContent = name;
      metadata.textContent = 'CLI 진단 · ' + engineMetadata(row);
      const results = capabilityLabels.map(([key, label]) => {
        const result = row.capabilities?.[key];
        const time = timeLabel(result?.checked_at);
        const status = {passed: '통과', failed: '실패'}[result?.status];
        return label + ' ' + (status && time ? status + ' (' + time + ')' : '미검증');
      });
      checks.textContent = '예제 검증 · ' + results.join(' / ');
      li.append(title, metadata, checks);
      return li;
    }));
  }

  function engineUnavailable() {
    enginesStale = true;
    const time = timeLabel(engineSnapshot?.checked_at);
    say('engine-status', time ? (host?.name ?? '선택한 호스트') + ' · 마지막으로 받은 진단 (' + time + '). 최신 진단을 확인할 수 없습니다.' :
      '선택한 호스트의 엔진 진단을 불러오지 못했습니다.');
    updateEngineWarning();
  }

  async function loadEngines() {
    if (engineRequest) return;
    if (!engineSnapshot) say('engine-status', host.name + '의 엔진 진단을 불러오는 중입니다.');
    const request = engineRequest = api('/engines');
    try {
      const snapshot = await request;
      if (closed || request !== engineRequest) return;
      if (!Array.isArray(snapshot?.engines)) throw new Error('엔진 진단 형식 오류');
      if (JSON.stringify(snapshot) !== JSON.stringify(engineSnapshot)) renderEngineRows(snapshot);
      engineSnapshot = snapshot;
      enginesStale = false;
      const checking = snapshot.engines.some(item => item.probe_status === 'pending');
      const time = timeLabel(snapshot.checked_at);
      say('engine-status', [host.name, snapshot.platform, time ? '진단 시각 ' + time : '진단 시각 미확인',
        checking && 'CLI 진단 중'].filter(Boolean).join(' · '));
      updateEngineWarning();
    } catch {
      if (!closed && request === engineRequest) engineUnavailable();
    } finally {
      if (request === engineRequest) engineRequest = null;
    }
  }

  function controls() {
    const unavailable = !host || !online;
    const parallel = get('use-parallel').checked, needsTeam = parallel || hasSkills() || Boolean(pending?.subtasks || pending?.role_skills);
    get('skill-entry').disabled = unavailable || !skillsAvailable || savingSkill;
    get('inspect-skill').disabled = unavailable || !skillsAvailable;
    get('use-parallel').disabled = !skillsAvailable && !parallel;
    get('single-run-engines').hidden = parallel;
    get('parallel-fields').hidden = get('parallel-fields').disabled = !parallel;
    for (const group of get('role-skills').children) {
      const picked = assigned.get(group.dataset.role);
      for (const box of group.querySelectorAll('input')) box.disabled = !box.checked && (!skillsAvailable || picked.size >= 4);
    }
    say('team-availability', skillsAvailable ? '' : '이 호스트의 스킬·병렬 작업 지원을 확인할 수 없습니다.');
    get('register').disabled = unavailable || registering;
    get('run-fields').disabled = unavailable || !workspace() || starting || Boolean(pending);
    const needsInbox = pending ? pending.use_inbox === true : get('use-inbox').checked;
    const delegated = get('use-delegation').checked;
    get('use-inbox').disabled = delegated;
    get('use-delegation').disabled = !delegated && (!needsInbox || !reportAvailable || !inboxAvailable);
    get('delegation-fields').hidden = get('delegation-fields').disabled = !delegated;
    say('delegation-availability', !needsInbox ? '위임하려면 협업 요청함을 켜세요.' :
      !reportAvailable ? '이 호스트의 복귀 보고를 확인해야 위임할 수 있습니다.' : '');
    get('start').disabled = unavailable || starting || (!pending && !workspace()) ||
      (needsInbox && !inboxAvailable) || (delegated && !reportAvailable) || (needsTeam && !skillsAvailable);
    for (const button of get('delegation-active').querySelectorAll('button')) {
      button.disabled = unavailable || !reportAvailable || endingDelegation.has(button.dataset.runId);
    }
    get('run-delegation-stop').disabled = unavailable || !detailDelegationActive || endingDelegation.has(selected);
    get('stop').disabled = unavailable || stopping.has(selected);
    for (const card of get('inbox-list').children) {
      card.querySelector('fieldset').disabled = unavailable || !inboxAvailable || deciding.has(card.dataset.key);
    }
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
    updateSubtaskFolders();
    controls();
  }

  function renderCatalog(preferred) {
    const previous = get('workspace').value;
    const indices = [Number(get('primary').value), ...checkedExtras()];
    const wanted = pending?.workspace_id ?? preferred ?? previous;
    get('workspace').replaceChildren(...workspaces.map(item => option(item.id, item.name)));
    if (workspaces.some(item => item.id === wanted)) get('workspace').value = wanted;
    if (pending && !workspaces.some(item => item.id === wanted)) {
      get('workspace').add(option(wanted, '원래 작업공간 · ' + wanted + ' (목록에 없음)'));
      get('workspace').value = wanted;
    } else if (!workspaces.length) {
      get('workspace').add(option('', '작업공간을 등록하세요'));
      get('registration').open = true;
    }
    renderFolders(previous && previous === get('workspace').value ? indices : [0]);
    if (previous && previous !== get('workspace').value && !pending) updateSubtaskFolders(true);
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
          get('use-inbox').checked = pending.use_inbox === true;
          renderFolders(pending.indices);
          if (Array.isArray(pending.subtasks)) {
            get('use-parallel').checked = true;
            for (const draft of pending.subtasks) addSubtask(draft);
            controls();
          }
        }
        renderRuns();
      }
      restoring = false;
      say('catalog-error', '');
    } catch {
      if (!closed && sequence === catalogSequence) say('catalog-error', '작업공간 목록을 불러오지 못했습니다.');
    }
  }

  async function requestHistory(id) {
    const {requests} = await api(inboxPath(id));
    if (!Array.isArray(requests) || requests.some(row => row.run_id !== id || !validId(row.id))) {
      throw new Error('요청 내역 형식 오류');
    }
    return requests;
  }

  function runLink(id, task) {
    const link = make('a'), url = new URL('/', win.location.href);
    if (hostId !== 'local') url.searchParams.set('host', hostId);
    url.searchParams.set('run', id);
    url.hash = 'detail';
    link.href = url.pathname + url.search + url.hash;
    link.className = 'inbox-link';
    link.textContent = (task ? task.slice(0, 100) + ' · ' : '') + '실행 상세 ' + id.slice(0, 8);
    link.addEventListener('click', event => {
      if (event.ctrlKey || event.metaKey || event.shiftKey || event.altKey || event.button !== 0) return;
      event.preventDefault();
      selectRun(id);
      get('detail-title').focus();
    });
    return link;
  }

  function delegationEvidence(row) {
    const meta = make('p'), citation = make('p'), quote = make('pre');
    meta.className = citation.className = 'inbox-meta';
    meta.textContent = row.evidence?.method === 'committed_fact' ? '기준 커밋에서 확인 · 모델 호출 0회' :
      delegationRoles.map(([role, name]) => name + ' ' + (engineNames.get(row[role + '_engine']) ?? '미확인')).join(' · ');
    const source = row.evidence;
    const choice = row.topic === 'technical_choice' && Object.hasOwn(choicePurposes, source?.purpose) &&
      Number.isSafeInteger(source.option) ? choicePurposes[source.purpose] + ' 선택지 ' + source.option + '번 · ' : '';
    citation.textContent = source ? choice + '근거 · ' + source.path + ' ' + source.line + '행 · 기준 커밋 ' + source.revision : '근거 미확인';
    quote.textContent = source?.quote ?? '';
    return [meta, citation, quote];
  }

  function delegatedCard(row) {
    const li = make('li'), details = make('details'), title = make('summary'), answer = make('p');
    const delivery = make('p'), outcome = make('p');
    title.textContent = row.prompt;
    answer.textContent = '대리 답변 · ' + row.answer;
    delivery.className = 'inbox-meta';
    delivery.textContent = [inboxStates[row.status] ?? '전달 상태 미확인', timeLabel(row.decided_at)].filter(Boolean).join(' · ');
    outcome.textContent = ['작업 상태 · ' + (states[row.run_state]?.[0] ?? '미확인'), outcomes[row.outcome] ?? row.outcome].filter(Boolean).join(' · ');
    details.append(title, answer, delivery, ...delegationEvidence(row), runLink(row.run_id, row.task), outcome);
    li.append(details);
    return li;
  }

  function activeDelegation(row) {
    const li = make('li'), button = make('button');
    button.type = 'button';
    button.className = 'stop';
    button.textContent = '이 실행의 위임 종료';
    button.dataset.runId = row.run_id;
    button.addEventListener('click', () => stopDelegation(row.run_id));
    li.append(runLink(row.run_id, row.task), button);
    return li;
  }

  async function stopDelegation(id) {
    const active = (id === selected && detailDelegationActive) || (reportAvailable && returnReport?.active.some(row => row.run_id === id));
    if (!online || endingDelegation.has(id) || !active) return;
    endingDelegation.add(id);
    controls();
    const inform = message => {
      say('delegation-stop-note', message);
      if (id === selected) say('run-delegation-note', message);
    };
    try {
      const receipt = await api('/runs/' + id + '/delegation/stop', {});
      if (receipt?.run_id !== id || receipt.active !== false) throw new Error();
      if (closed) return;
      inform('실행 ' + id.slice(0, 8) + '의 위임을 종료했습니다. 이후 질문은 직접 처리합니다.');
      if (id === selected) {
        ++detailSequence;
        detailDelegationActive = false;
        get('run-delegation-stop').hidden = true;
        say('run-delegation-status', '위임 종료');
      }
      if (reportAvailable && returnReport.active.some(row => row.run_id === id)) {
        const counts = {...returnReport.counts};
        if (counts.active > 0) counts.active--;
        renderReport({...returnReport, counts, active: returnReport.active.filter(row => row.run_id !== id)});
      }
    } catch {
      if (!closed) inform('실행 ' + id.slice(0, 8) + '의 위임 종료 여부를 확인하지 못했습니다. 실행 상세에서 위임 상태를 다시 확인하세요.');
    } finally {
      endingDelegation.delete(id);
      if (!closed && online) {
        reportRequest = null;
        loadReturnReport();
        if (id === selected) loadDetail();
        controls();
      }
    }
  }

  function renderReport(report) {
    const {delegated, active, counts} = report;
    if (JSON.stringify(active) !== JSON.stringify(returnReport?.active)) {
      get('delegation-active').replaceChildren(...active.map(activeDelegation));
    }
    if (JSON.stringify(delegated) !== JSON.stringify(returnReport?.delegated)) {
      const list = get('delegation-decisions');
      const previous = new Map([...list.children].map(li => [li.dataset.key, li]));
      for (const [i, row] of delegated.entries()) {
        const key = requestKey(row), snapshot = JSON.stringify(row);
        let li = previous.get(key);
        previous.delete(key);
        if (li?.dataset.snapshot !== snapshot) {
          const next = delegatedCard(row);
          next.firstChild.open = Boolean(li?.firstChild.open);
          next.dataset.key = key;
          next.dataset.snapshot = snapshot;
          li?.replaceWith(next);
          li = next;
        }
        if (list.children[i] !== li) list.insertBefore(li, list.children[i] ?? null);
      }
      for (const li of previous.values()) li.remove();
    }
    returnReport = report;
    const shown = counts.delegated > delegated.length ? ' (최근 ' + delegated.length + '건 표시)' : '';
    say('report-note', host.name + ' · 대리 답변 ' + counts.delegated + '건' + shown + ' · 답변 대기 ' + counts.questions +
      '건 · 권한 승인 대기 ' + counts.permissions + '건' +
      (counts.product_decisions ? ' · 제품 결정 대기 ' + counts.product_decisions + '건' : '') +
      (counts.active > active.length ? ' · 위임 중인 실행 ' + counts.active + '개 중 최근 ' + active.length + '개 표시' : ''));
  }

  function reportUnavailable() {
    reportAvailable = false;
    say('report-note', returnReport ? '마지막으로 받은 복귀 보고입니다. 최신 위임 상태를 확인할 수 없습니다.' :
      '이 호스트의 복귀 보고를 확인할 수 없습니다.');
  }

  async function loadReturnReport() {
    if (reportRequest) return;
    const request = reportRequest = api('/return-report');
    try {
      const report = await request;
      if (closed || request !== reportRequest) return;
      if (!['delegated', 'questions', 'permissions'].every(key => Number.isSafeInteger(report.counts?.[key]) && report.counts[key] >= 0) ||
          (Object.hasOwn(report.counts, 'product_decisions') &&
            !(Number.isSafeInteger(report.counts.product_decisions) && report.counts.product_decisions >= 0)) ||
          !Array.isArray(report.delegated) || report.delegated.some(row => !validId(row?.run_id) || !validId(row.id)) ||
          !Array.isArray(report.active) || report.active.some(row => !validId(row?.run_id))) throw new Error();
      renderReport(report);
      reportAvailable = true;
    } catch {
      if (!closed && request === reportRequest) reportUnavailable();
    } finally {
      if (!closed && request === reportRequest) { reportRequest = null; controls(); }
    }
  }

  function requestCard(row, actionable) {
    const card = make('li'), title = make('h3'), meta = make('p'), prompt = make('pre');
    const acceptsAnswer = row.kind === 'question' || row.kind === 'product_decision';
    card.dataset.key = requestKey(row);
    card.dataset.kind = row.kind;
    title.textContent = Object.hasOwn(requestLabels, row.kind) ? requestLabels[row.kind] : '요청 종류 미확인';
    meta.className = 'inbox-meta';
    const role = roles.find(([id]) => id === row.role)?.[1] ?? '역할 미확인';
    meta.textContent = [role, engineNames.get(row.engine) ?? '엔진 미확인',
      Number.isInteger(row.attempt) && row.attempt > 0 && row.attempt + '번째 시도', actionable && '응답 대기',
      timeLabel(row.created_at)].filter(Boolean).join(' · ');
    prompt.textContent = row.prompt;
    card.append(title, meta, prompt);
    const reason = make('p');
    reason.className = 'inbox-meta delegation-reason';
    reason.textContent = checkNote(row);
    card.append(reason);
    if (!actionable) {
      const status = make('p');
      status.className = 'inbox-meta inbox-status';
      status.textContent = decisionLabel(row);
      card.append(status);
      if (row.shared_decision) {
        const source = make('p'), record = row.shared_decision;
        source.textContent = '출처 목표 ' + record.source_run + ' · 원본 요청 ' + record.request_id + ' · 버전 ' + record.revision;
        card.append(source);
      }
      if (row.source === 'delegate' && row.delegation_details) card.append(...delegationEvidence(row.delegation_details));
      const dates = [['decided_at', '결정'], ['delivered_at', '전달'], ['expired_at', '만료']]
        .filter(([field]) => timeLabel(row[field])).map(([field, label]) => label + ' ' + timeLabel(row[field]));
      if (dates.length) {
        const times = make('small');
        times.textContent = dates.join(' · ');
        card.append(times);
      }
      return card;
    }
    const fields = make('fieldset'), legend = make('legend'), actions = make('div');
    const task = runs.find(run => run.id === row.run_id)?.task;
    const link = runLink(row.run_id, task);
    legend.className = 'sr-only';
    legend.textContent = title.textContent + ' 응답';
    fields.append(legend);
    let answer;
    if (acceptsAnswer) {
      const label = make('label');
      answer = make('textarea');
      answer.id = 'answer-' + row.run_id + '-' + row.id;
      answer.rows = 2;
      label.htmlFor = answer.id;
      label.textContent = '답변 (최대 8,192바이트)';
      fields.append(label, answer);
    }
    actions.className = 'inbox-actions';
    for (const action of acceptsAnswer ? ['answer'] : ['approve', 'reject']) {
      const button = make('button');
      button.type = 'button';
      button.className = action === 'reject' ? 'stop' : 'secondary';
      button.textContent = action === 'answer' ? '답변 보내기' : action === 'approve' ? '이번 요청만 승인' : '거절';
      button.addEventListener('click', () => submitDecision(row, action, answer));
      actions.append(button);
    }
    fields.append(actions);
    card.append(link, fields);
    return card;
  }

  function renderInbox(rows) {
    pendingRows = rows.filter(row => validId(row.id) && validId(row.run_id) && row.status === 'pending' &&
      Object.hasOwn(requestLabels, row.kind));
    const list = get('inbox-list');
    const previous = new Map([...list.children].map(card => [card.dataset.key, card]));
    for (const [index, row] of pendingRows.entries()) {
      const key = requestKey(row);
      const card = previous.get(key) ?? requestCard(row, true);
      const reason = card.querySelector('.delegation-reason');
      if (reason.textContent !== checkNote(row)) reason.textContent = checkNote(row);
      previous.delete(key);
      if (list.children[index] !== card) list.insertBefore(card, list.children[index] ?? null);
    }
    for (const card of previous.values()) card.remove();
    say('inbox-count', String(pendingRows.length));
    say('inbox-note', '이 호스트에 대기 중인 요청이 없습니다.');
    get('inbox-note').hidden = pendingRows.length > 0;
    controls();
  }

  function receipt(row) {
    say('decision-note', '실행 ' + row.run_id.slice(0, 8) + ' · 요청 ' + row.id.slice(0, 8) + ' · ' + decisionLabel(row));
  }

  async function readDecision(row) {
    const match = (await requestHistory(row.run_id)).find(item => item.id === row.id);
    if (!match) throw new Error('요청을 확인할 수 없습니다.');
    return match;
  }

  async function reconcileUnconfirmed(sequence) {
    for (const [key, row] of unconfirmed) {
      if (deciding.has(key)) continue;
      let match;
      try {
        match = await readDecision(row);
      } catch {
        if (!closed && sequence === listSequence) say('decision-note', '결정 내역을 다시 확인하고 있습니다.');
        continue;
      }
      if (closed || sequence !== listSequence || !unconfirmed.delete(key)) continue;
      if (match.status === 'pending') say('decision-note', '아직 결정 기록이 없습니다. 요청을 확인한 뒤 다시 응답할 수 있습니다.');
      else receipt(match);
    }
  }

  async function submitDecision(row, action, answer) {
    const key = requestKey(row);
    if (!host || !online || !inboxAvailable || closed || deciding.has(key) || !pendingRows.some(item => requestKey(item) === key)) return;
    const body = {action};
    if (action === 'answer') {
      body.answer = answer.value;
      if (!body.answer.trim() || new TextEncoder().encode(body.answer).length > 8192) {
        say('decision-note', '답변을 UTF-8 기준 1~8,192바이트로 입력하세요.');
        answer.focus();
        return;
      }
    }
    deciding.add(key);
    controls();
    say('decision-note', '요청 상태를 확인하고 있습니다.');
    try {
      // Re-read the owning run before each explicit submission, including after a lost receipt or reload.
      const current = await readDecision(row);
      if (closed || !online || !inboxAvailable) return;
      if (current.status !== 'pending') { receipt(current); return; }
      if (current.kind !== row.kind || current.prompt !== row.prompt || current.engine !== row.engine || current.role !== row.role) {
        say('decision-note', '요청 내용이 달라졌습니다. 화면을 다시 열어 확인하세요.');
        return;
      }
      unconfirmed.set(key, row);
      const {request} = await api(inboxPath(row.run_id) + '/' + row.id + '/decision', body);
      if (closed) return;
      if (request?.id !== row.id || request.run_id !== row.run_id || request.status === 'pending') throw new Error('접수 결과 미확인');
      unconfirmed.delete(key);
      receipt(request);
    } catch (error) {
      if (closed) return;
      if (error.status === 400) {
        unconfirmed.delete(key);
        say('decision-note', error.message);
      } else say('decision-note', '결정 결과를 확인하지 못했습니다. 연결 후 요청 내역을 다시 확인합니다.');
    } finally {
      deciding.delete(key);
      if (!closed) { controls(); refresh(); }
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
      const info = [workspaceName(run.workspace_id), run.id.slice(0, 8), inboxMode(run)].join(' · ');
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
      historySnapshot = null;
      detailPolicy = null;
      detailDelegationActive = false;
      say('run-delegation-note', '');
      get('history-inbox-list').replaceChildren();
      say('history-inbox-note', '요청 내역을 불러오는 중입니다.');
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

  function renderRunPolicy(run) {
    const policy = run.delegation;
    const assignmentDelegation = run.assignment?.delegation === true;
    const visible = Boolean(policy || assignmentDelegation || run.delegation_active === true);
    get('run-delegation').hidden = !visible;
    detailDelegationActive = visible && run.kind !== 'parallel' && run.delegation_active === true;
    get('run-delegation-stop').hidden = !detailDelegationActive;
    get('run-delegation-stop').disabled = !online || endingDelegation.has(run.id);
    if (!visible) { detailPolicy = null; return; }
    if (!policy) {
      say('run-delegation-roles', '기준 커밋에서 확인 가능한 기술 선택지를 검토합니다.');
      get('run-delegation-rules').replaceChildren();
      detailPolicy = null;
    } else if (!Array.isArray(policy.rules) || policy.rules.some(rule => !rule || typeof rule.path !== 'string' || typeof rule.topic !== 'string')) {
      say('run-delegation-roles', '접수 당시의 위임 범위를 확인할 수 없습니다.');
      get('run-delegation-rules').replaceChildren();
      detailPolicy = null;
    } else if (!samePolicy(policy, detailPolicy)) {
      say('run-delegation-roles', delegationRoles.map(([role, name]) =>
        name + ' ' + (engineNames.get(policy[role + '_engine']) ?? '미확인')).join(' · '));
      get('run-delegation-rules').replaceChildren(...policy.rules.map(rule => {
        const li = make('li'), path = make('code');
        path.textContent = rule.path;
        li.append((topics.find(([key]) => key === rule.topic)?.[1] ?? rule.topic) + ' · ', path);
        return li;
      }));
      detailPolicy = policy;
    }
    say('run-delegation-status', run.kind === 'parallel' ? '각 하위 작업에서 위임 상태를 확인하고 종료할 수 있습니다.' :
      detailDelegationActive ? '위임 중' : run.delegation_active === false ? '위임 종료' : '위임 상태 미확인');
  }

  function skillDescription(run) {
    return skillRoles.flatMap(([role, title]) => {
      const ids = run.role_skills?.[role];
      if (!Array.isArray(ids) || !ids.length) return [];
      const snapshots = run.role_skill_snapshots?.[role] ?? [];
      return title + ' · ' + ids.map(id => {
        const skill = snapshots.find(item => item.id === id) ?? skillCatalog.get(id);
        return skill ? skillLabel(skill) : id;
      }).join(', ');
    }).join('\n');
  }

  function childCard(item, parent) {
    const card = make('details'), title = make('summary'), meta = make('p'), skillInfo = make('p');
    const diagnostic = make('p'), note = make('p'), evidenceBox = make('div');
    title.textContent = item.task;
    meta.className = skillInfo.className = 'assignments';
    note.className = 'assignments';
    note.setAttribute('role', 'status');
    diagnostic.className = 'notice error';
    const open = runLink(item.id);
    open.textContent = '하위 실행 상세 및 협업 요청 보기';
    card.className = 'child-result';
    card.dataset.childId = item.id;
    card.append(title, meta, skillInfo, diagnostic, note, evidenceBox, open);
    let reading = false;
    async function updateEvidence() {
      if (!card.open || reading) return;
      reading = true;
      if (!evidenceBox.children.length) note.textContent = '하위 실행의 근거를 불러오는 중입니다.';
      try {
        const {run} = await api('/runs/' + item.id);
        if (closed || selected !== parent.id || !card.isConnected) return;
        if (run?.id !== item.id || run.parent_id !== parent.id || run.kind !== 'subtask' ||
            !parent.base_revision || run.base_revision !== parent.base_revision ||
            !['workspace_id', 'allow_edits', 'use_inbox'].every(key => run[key] === parent[key]) ||
            !['task', 'developer_engine', 'reviewer_engine'].every(key => run[key] === item[key]) ||
            !sameList(run.indices, item.indices) || !sameSkills(run.role_skills, parent.role_skills)) {
          evidenceBox.replaceChildren();
          skillInfo.textContent = diagnostic.textContent = '';
          note.textContent = '하위 실행의 배정이 원래 요청과 달라 근거를 표시할 수 없습니다.';
          return;
        }
        skillInfo.textContent = '배정 스킬 · ' + (skillDescription(run) || '없음');
        diagnostic.textContent = run.diagnostic ?? '';
        note.textContent = [run.workspace, run.branch].filter(Boolean).join(' · ');
        const labels = ['개발자 응답', '테스트 결과', '리뷰 의견', '변경 파일', '변경 내용 diff'];
        evidence.forEach((key, index) => {
          let panel = evidenceBox.children[index];
          if (!panel) {
            panel = make('details');
            const summary = make('summary');
            summary.textContent = labels[index];
            panel.append(summary, make('pre'));
            evidenceBox.append(panel);
          }
          const value = run[key] || '기록된 내용이 없습니다.';
          if (panel.lastChild.textContent !== value) panel.lastChild.textContent = value;
        });
      } catch {
        note.textContent = '하위 실행의 최신 근거를 확인할 수 없습니다.';
      } finally { reading = false; }
    }
    card.addEventListener('toggle', updateEvidence);
    return {card, title, meta, updateEvidence};
  }

  const childrenById = new Map();
  function renderTeam(run) {
    const group = run.kind === 'parallel';
    get('group-result').hidden = !group;
    for (const detail of doc.querySelectorAll('.outputs > details')) detail.hidden = group;
    const parentLink = get('parent-link');
    if (validId(run.parent_id) && parentLink.dataset.parent !== run.parent_id) {
      const link = runLink(run.parent_id);
      link.textContent = '← 원래 병렬 작업 보기';
      parentLink.replaceChildren(link);
      parentLink.dataset.parent = run.parent_id;
    }
    parentLink.hidden = !validId(run.parent_id);
    say('assigned-skills', skillDescription(run));
    get('assigned-skills').hidden = !get('assigned-skills').textContent;
    const children = group && Array.isArray(run.subtasks) ? run.subtasks : [];
    for (const [id, node] of childrenById) {
      if (!children.some(child => child.id === id)) { node.card.remove(); childrenById.delete(id); }
    }
    if (!group) return;
    say('group-revision', '기준 커밋 · ' + (run.base_revision ?? '미확인'));
    for (const [index, child] of children.entries()) {
      if (!validId(child?.id)) continue;
      let node = childrenById.get(child.id);
      if (!node) {
        node = childCard(child, run);
        childrenById.set(child.id, node);
        get('child-results').append(node.card);
      }
      const state = states[child.state]?.[0] ?? child.state ?? '상태 미확인';
      const heading = (index + 1) + '. ' + child.task + ' · ' + state;
      if (node.title.textContent !== heading) node.title.textContent = heading;
      const folders = child.indices?.map(i => run.folders?.[run.indices?.indexOf(i)] ??
        workspaces.find(item => item.id === run.workspace_id)?.folders[i] ?? ('폴더 ' + (i + 1)));
      node.meta.textContent = [...roles.map(([role, name]) => name + ' ' + (engineNames.get(child[role + '_engine']) ?? '미확인')),
        folders?.join(', '), outcomes[child.outcome] ?? child.outcome].filter(Boolean).join(' · ');
      node.updateEvidence();
    }
  }

  function renderDetail(run) {
    get('detail-body').hidden = false;
    get('detail-note').hidden = true;
    get('run-state').hidden = false;
    badge(get('run-state'), run.state);
    say('request', run.task);
    const assignments = roles.map(([role, title, defaultId]) => {
      const id = run[role + '_engine'] ?? defaultId;
      return title + ' ' + (engineNames.get(id) ?? '미확인');
    });
    say('run-engines', assignments.join(' · '));
    say('run-inbox-mode', inboxMode(run));
    renderRunPolicy(run);
    renderTeam(run);
    const group = run.kind === 'parallel';
    get('run-engines').hidden = group;
    say('detail-workspace', workspaceName(run.workspace_id));
    say('run-id', run.id);
    get('attempts').hidden = group || !Number.isInteger(run.attempts);
    say('attempts', Number.isInteger(run.attempts) ? '개발 시도 ' + run.attempts + '회' : '');
    get('retry-note').hidden = !(run.attempts > 1);
    get('outcome').hidden = group || !run.outcome;
    say('outcome', outcomes[run.outcome] ?? (run.outcome ? '종료 결과를 확인할 수 없습니다.' : ''));
    get('diagnostic').hidden = !run.diagnostic;
    say('diagnostic', run.diagnostic || '');
    const tested = Boolean(run.test_output) || testedOutcomes.has(run.outcome) || ['TESTING', 'REVIEWING'].includes(run.state);
    get('test-command').hidden = !run.allow_edits || !tested;
    get('run-context').hidden = group || !Array.isArray(run.folders);
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
      if (closed || sequence !== detailSequence) return;
      renderDetail(run);
      if (run.kind === 'parallel') return;
      try {
        const history = await requestHistory(id);
        if (closed || sequence !== detailSequence) return;
        const snapshot = JSON.stringify(history);
        if (snapshot !== historySnapshot) {
          get('history-inbox-list').replaceChildren(...history.map(row => requestCard(row, false)));
          historySnapshot = snapshot;
        }
        say('history-inbox-note', history.length ? '' : '이 실행에 기록된 협업 요청이 없습니다.');
      } catch {
        if (!closed && sequence === detailSequence) say('history-inbox-note', '최신 요청 내역을 확인할 수 없습니다.');
      }
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
    engineRequest = null;
    engineUnavailable();
    reportRequest = null;
    reportUnavailable();
    online = false;
    inboxAvailable = false;
    controls();
    say('connection', message);
    say('inbox-note', '호스트에 연결하면 대기 요청을 확인하고 응답할 수 있습니다.');
    get('inbox-note').hidden = false;
    get('connection').dataset.online = 'false';
    get('list-note').hidden = false;
    say('list-note', '실행 기록을 갱신하지 못했습니다. 새로고침하면 다시 연결합니다.');
    if (selected) {
      get('detail-note').hidden = false;
      say('detail-note', get('detail-body').hidden ? '연결 후 실행 상태를 확인할 수 있습니다.' :
        '마지막으로 확인한 상태와 결과입니다. 연결 후 최신 내용을 표시합니다.');
    }
  }

  async function loadInbox(sequence) {
    try {
      const {requests} = await api('/inbox');
      if (closed || !online || sequence !== listSequence) return;
      if (!Array.isArray(requests)) throw new Error('요청 목록 형식 오류');
      inboxAvailable = true;
      renderInbox(requests);
    } catch {
      if (closed || !online || sequence !== listSequence) return;
      inboxAvailable = false;
      controls();
      say('inbox-note', '이 호스트의 협업 요청함을 확인할 수 없습니다. 요청함을 끄면 새 작업을 시작할 수 있습니다.');
      get('inbox-note').hidden = false;
      return;
    }
    await reconcileUnconfirmed(sequence);
  }

  async function refresh() {
    if (doc.hidden || closed) return;
    win.clearTimeout(timer);
    const sequence = ++listSequence;
    let catalog, inbox;
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
      loadEngines();
      const {runs: list} = await api('/runs');
      if (closed || sequence !== listSequence) return;
      runs = list;
      renderRuns();
      online = true;
      inbox = loadInbox(sequence);
      loadSkills();
      loadReturnReport();
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
      await Promise.all([catalog, inbox]);
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
    if (pending) {
      const sent = pending.subtasks ?? [], got = run.subtasks ?? [];
      const sameTasks = Boolean(sent.length) === (run.kind === 'parallel') && Array.isArray(got) &&
        sent.length === got.length && sent.every((child, index) => {
          const receipt = got[index];
          // The journal masks sensitive text before exposing task snapshots.
          return receipt && validId(receipt.id) && receipt.parent_id === pending.id && typeof receipt.task === 'string' &&
            (child.task === receipt.task || receipt.task.includes('[redacted]') || receipt.task.endsWith('...[truncated]')) &&
            ['developer_engine', 'reviewer_engine'].every(key => child[key] === receipt[key]) &&
            sameList(child.indices, receipt.indices);
        });
      const changedSettings = (sent.length || pending.role_skills) &&
        (pending.workspace_id !== run.workspace_id || pending.allow_edits !== run.allow_edits ||
          (sent.length && !sameList(pending.indices, run.indices)));
      if (changedSettings || !sameSkills(pending.role_skills, run.role_skills) || !sameTasks) {
        say('start-note', '접수 기록의 역할별 스킬 또는 하위 작업 배정이 요청과 다릅니다. 원래 요청을 보관했습니다.');
        return;
      }
    }
    if (!samePolicy(pending?.delegation, run.delegation)) {
      keepPending(null);
      say('start-note', '접수한 실행의 위임 범위가 요청과 다릅니다. 복귀 보고에서 이 실행의 위임 상태를 확인하세요.');
      return;
    }
    if (pending?.use_inbox === true && run.use_inbox !== true) {
      keepPending(null);
      say('start-note', '접수한 실행에서 선택한 협업 요청함 설정을 확인할 수 없습니다. 실행 상세를 확인하세요.');
      return;
    }
    if (pending && !Object.hasOwn(pending, 'use_inbox')) get('use-inbox').checked = true;
    keepPending(null);
    resetDelegation();
    for (const ids of assigned.values()) ids.clear();
    renderSkillCatalog();
    get('use-parallel').checked = false;
    get('subtasks').replaceChildren();
    updateEngineWarning();
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
        indices: selectedIndices(),
        task, allow_edits: get('edit-mode').checked,
        use_inbox: get('use-inbox').checked,
        developer_engine: get('developer-engine').value,
        reviewer_engine: get('reviewer-engine').value,
      };
      if (hasSkills()) request.role_skills = chosenSkills();
      if (get('use-parallel').checked) {
        const children = readSubtasks();
        const invalid = children.findIndex(child => !child.task || bytes(child.task) > 8192 || !child.indices.length);
        if (invalid !== -1) {
          get('subtasks').children[invalid].querySelector(children[invalid].indices.length ? 'textarea' : 'input').focus();
          return say('start-note', '하위 작업 ' + (invalid + 1) + '에 8,192바이트 이내의 요청과 담당 폴더를 지정하세요.');
        }
        request.subtasks = children;
      }
      if (get('use-delegation').checked) {
        const rules = topics.filter(([topic]) => get('topic-' + topic).checked)
          .map(([topic]) => ({topic, path: get('path-' + topic).value}));
        if (!rules.length) {
          get('topic-' + topics[0][0]).focus();
          return say('start-note', '위임할 질문을 하나 이상 선택하세요.');
        }
        if (get('independent-facts').checked) for (const rule of rules) rule.verification = 'independent';
        request.delegation = {rules, specialist_engine: get('specialist-engine').value, verifier_engine: get('verifier-engine').value};
      }
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
        // Stage 2 peers accept the original payload when the inbox is off.
        ({run} = await api('/runs', {...request, use_inbox: request.use_inbox || undefined}));
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
  get('run-delegation-stop').addEventListener('click', () => {
    if (!get('run-delegation-stop').disabled && !get('run-delegation-stop').hidden) stopDelegation(selected);
  });

  get('workspace').addEventListener('change', () => { renderFolders(); updateSubtaskFolders(true); });
  get('use-inbox').addEventListener('change', controls);
  get('use-delegation').addEventListener('change', () => { updateEngineWarning(); controls(); });
  get('independent-facts').addEventListener('change', updateEngineWarning);
  get('primary').addEventListener('change', () => {
    renderExtras(checkedExtras());
    updateSubtaskFolders();
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
