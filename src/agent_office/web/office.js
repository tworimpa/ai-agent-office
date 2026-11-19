const idPattern = /^[0-9a-f]{32}$/;
const roles = {developer: '개발자', reviewer: '리뷰어', specialist: '전문가', verifier: '독립 검증자'};
const engines = {codex: 'Codex', antigravity: 'Antigravity', claude: 'Claude Code', gemini: 'Gemini CLI', cline: 'Cline CLI'};
const activities = {
  queued: '차례 대기', working: '작업 중', testing: '저장소 테스트 중', waiting: '응답 대기',
  done: '담당 단계 완료', failed: '실패', stopped: '중단됨', stopping: '중지 중', skipped: '미실행',
  recovery: '복구 확인 필요', checking: '위임 질문 확인 중',
};
const activeStates = new Set(['QUEUED', 'RUNNING', 'TESTING', 'REVIEWING', 'STOP_REQUESTED']);
const outcomes = {
  STOP_REQUESTED: '실행 중지 요청 중',
  PLAN_ONLY: '계획 작성 완료', READY_FOR_HUMAN_REVIEW: '사람 검토 대기', WAITING_USER: '사용자 확인 필요',
  FAILED: '실행 실패', CANCELED: '실행 중지', RECOVERY_REQUIRED: '복구 필요',
};

export function mountOffice(win) {
  const doc = win.document;
  const get = id => doc.getElementById(id);
  const selected = new URL(win.location.href).searchParams.getAll('host');
  const hostURL = id => id ? '/office?host=' + id : '/office';
  const nodes = new Map();
  let cycle = 0, timer, disposed = false;
  const controllers = new Set();

  function element(tag, className, text) {
    const node = doc.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function notice(message) {
    if (get('notice').textContent !== message) get('notice').textContent = message;
  }
  function showSummary() {
    const hosts = [...nodes.values()];
    const connected = hosts.filter(host => host.page && !host.stale);
    for (const [id, key] of [['active-count', 'active_runs'], ['question-count', 'questions'], ['permission-count', 'permissions']]) {
      get(id).textContent = connected.length ? connected.reduce((sum, host) => sum + host.page.counts[key], 0) : '—';
    }
    if (hosts.some(host => host.stale)) notice('상단 집계에는 연결을 확인한 호스트만 포함됩니다.');
    else if (hosts.some(host => !host.page)) notice('호스트에서 업무 현황을 불러오는 중입니다.');
    else notice(hosts.length ? '' : '선택한 호스트를 찾을 수 없습니다.');
  }

  function station(agent, hostId) {
    const link = element('a', 'station');
    const prefix = hostId === 'local' ? '/?' : '/?host=' + hostId + '&';
    link.href = prefix + 'run=' + agent.run_id + '#detail';
    link.dataset.key = agent.run_id + '-' + agent.role;
    link.dataset.activity = agent.activity;
    const portrait = element('span', 'portrait');
    portrait.dataset.role = agent.role;
    portrait.setAttribute('aria-hidden', 'true');
    const assignment = element('div', 'assignment');
    assignment.append(element('span', 'role', roles[agent.role] + ' · ' + (engines[agent.engine] ?? agent.engine)),
      element('h4', '', agent.task));
    if (agent.parent_id) assignment.append(element('p', 'parent', '상위 작업 · ' + (agent.parent_task || agent.parent_id)));
    assignment.append(element('span', 'state', activities[agent.activity] ?? '상태 확인 필요'));
    assignment.append(element('span', 'run-ref', [outcomes[agent.run_state], '#' + agent.run_id.slice(0, 8)].filter(Boolean).join(' · ')));
    const identity = element('div', 'identity');
    identity.append(portrait, assignment);
    link.append(identity);
    for (const request of agent.requests) {
      const bubble = element('p', 'speech');
      bubble.dataset.kind = request.kind;
      bubble.append(element('strong', '', request.kind === 'permission' ? '권한 요청' : '기술 질문'),
        doc.createTextNode(request.prompt));
      link.append(bubble);
    }
    if (agent.request_count > agent.requests.length) {
      link.append(element('p', 'more', '외 ' + (agent.request_count - agent.requests.length) + '건 · 상세에서 보기'));
    }
    return link;
  }

  function replaceBody(host, page) {
    const focusKey = host.el.contains(doc.activeElement) && doc.activeElement.dataset.key;
    const body = element('div', 'host-body');
    const groups = {requests: [], work: [], history: []};
    const titles = {requests: '응답이 필요한 자리', work: '업무 자리', history: '최근 실행 기록'};
    for (const agent of page.agents) {
      const group = agent.request_count ? 'requests' : activeStates.has(agent.run_state) ? 'work' : 'history';
      groups[group].push(agent);
    }
    if (!page.agents.length) body.append(element('p', 'empty',
      page.offset ? '이 페이지에 표시할 실행이 없습니다.' : '아직 배정된 작업이 없습니다.'));
    else if (!page.counts.active_runs) body.append(element('p', 'empty', '현재 진행 중인 실행이 없습니다.'));
    for (const [group, agents] of Object.entries(groups)) {
      if (!agents.length) continue;
      const section = element('section', 'zone'), grid = element('div', 'stations');
      section.dataset.zone = group;
      grid.append(...agents.map(agent => station(agent, host.id)));
      section.append(element('h3', '', titles[group] + ' · ' + agents.length), grid);
      body.append(section);
    }
    host.body.replaceWith(body);
    host.body = body;
    host.pager.replaceChildren();
    host.pager.hidden = !(page.offset || page.next_offset !== null);
    if (!host.pager.hidden) {
      const changePage = (label, offset, key) => {
        const button = element('button', '', label);
        button.disabled = offset === null;
        button.dataset.key = key;
        button.addEventListener('click', () => refreshHost(host, offset));
        return button;
      };
      const pageStart = page.total_runs ? page.offset + 1 : 0;
      const previous = host.previous.get(page.offset) ?? 0;
      host.pager.append(element('span', '', '전체 ' + page.total_runs + '건 · ' + pageStart + '번째부터'),
        changePage('이전', page.offset ? previous : null, 'prev'), changePage('다음', page.next_offset, 'next'));
    }
    if (focusKey) {
      const target = [...host.el.querySelectorAll('[data-key]')].find(node => node.dataset.key === focusKey && !node.disabled);
      (target ?? host.heading).focus({preventScroll: true});
    }
  }

  function createHost(info) {
    const el = element('section', 'host');
    el.dataset.host = info.id;
    const head = element('header', 'host-head'), heading = element('h2', '', info.name);
    heading.tabIndex = -1;
    const meta = element('p', 'host-meta', '연결 확인 중');
    head.append(heading, meta);
    const body = element('div', 'host-body'), pager = element('div', 'pager');
    pager.hidden = true;
    el.append(head, body, pager);
    return {id: info.id, el, body, pager, meta, heading, previous: new Map(), seq: 0};
  }

  async function json(path) {
    const controller = new win.AbortController();
    controllers.add(controller);
    const timeout = win.setTimeout(() => controller.abort(), 15000);
    try {
      const response = await win.fetch(path, {signal: controller.signal, mode: 'same-origin', redirect: 'error', cache: 'no-store'});
      if (!response.ok) throw new Error('HTTP ' + response.status);
      return await response.json();
    } finally {
      win.clearTimeout(timeout);
      controllers.delete(controller);
    }
  }

  function markStale(host) {
    host.stale = true;
    host.el.dataset.stale = 'true';
    host.meta.textContent = host.page ? '연결 확인 실패 · 마지막 기록 ' + host.stamp : '연결 확인 실패 · 수신한 기록 없음';
    if (!host.page) host.body.textContent = '이 호스트의 업무 현황을 불러올 수 없습니다.';
    showSummary();
  }

  async function refreshHost(host, offset = host.offset ?? 0) {
    host.offset = offset;
    const ticket = ++host.seq, revision = cycle;
    const current = () => !disposed && !doc.hidden && revision === cycle && host.seq === ticket && nodes.get(host.id) === host;
    const prefix = host.id === 'local' ? '/api/office' : '/api/hosts/' + host.id + '/office';
    try {
      const page = await json(prefix + (offset ? '/page/' + offset : ''));
      if (!current()) return;
      const validCount = n => Number.isSafeInteger(n) && n >= 0;
      if (page.offset !== offset || !validCount(page.total_runs) || (page.next_offset !== null &&
          !(validCount(page.next_offset) && page.next_offset > offset)) || !Array.isArray(page.agents) ||
          !['active_runs', 'questions', 'permissions'].every(key => validCount(page.counts?.[key])) ||
          !page.agents.every(agent => idPattern.test(agent.run_id) && Object.hasOwn(roles, agent.role) &&
            Array.isArray(agent.requests) && validCount(agent.request_count)) ||
          !Number.isFinite(Date.parse(page.generated_at))) throw new Error('invalid office data');
      if (offset > 0 && offset >= page.total_runs) {
        return refreshHost(host, 0);
      }
      const signature = JSON.stringify([page.offset, page.next_offset, page.total_runs, page.counts.active_runs, page.agents]);
      if (page.next_offset !== null) host.previous.set(page.next_offset, offset);
      if (signature !== host.signature) replaceBody(host, page);
      host.signature = signature;
      host.page = page;
      host.stale = false;
      host.el.dataset.stale = 'false';
      host.stamp = new Date(page.generated_at).toLocaleString('ko-KR');
      host.meta.textContent = '마지막 확인 ' + host.stamp;
      showSummary();
    } catch {
      if (current()) markStale(host);
    }
  }

  let filterSignature;
  function renderFilters(hosts) {
    const signature = JSON.stringify(hosts);
    if (signature === filterSignature) return;
    filterSignature = signature;
    get('host-filters').replaceChildren(...[{id: '', name: '전체 호스트'}, ...hosts].map(info => {
      const link = element('a', '', info.name);
      link.href = hostURL(info.id);
      if ((!selected.length && !info.id) || (selected.length === 1 && selected[0] === info.id)) link.setAttribute('aria-current', 'page');
      return link;
    }));
  }

  function cancel() {
    cycle++;
    win.clearTimeout(timer);
    for (const controller of controllers) controller.abort();
  }
  async function refresh() {
    cancel();
    if (disposed || doc.hidden) return;
    const revision = cycle;
    try {
      const {hosts} = await json('/api/hosts');
      if (revision !== cycle) return;
      if (!Array.isArray(hosts) || !hosts.some(h => h.id === 'local') ||
          !hosts.every(h => (h.id === 'local' || idPattern.test(h.id)) && typeof h.name === 'string') ||
          new Set(hosts.map(h => h.id)).size !== hosts.length) throw new Error('invalid hosts');
      renderFilters(hosts);
      const visible = selected.length ? hosts.filter(h => selected.length === 1 && h.id === selected[0]) : hosts;
      for (const [id, host] of nodes) {
        if (!visible.some(info => info.id === id)) { host.el.remove(); nodes.delete(id); }
      }
      for (const info of visible) {
        if (!nodes.has(info.id)) {
          const host = createHost(info);
          nodes.set(info.id, host);
          get('hosts').append(host.el);
        }
        nodes.get(info.id).heading.textContent = info.name;
      }
      get('studio-link').href = selected.length === 1 && visible.length && idPattern.test(selected[0]) ? '/?host=' + selected[0] : '/';
      showSummary();
      // Keep a large host directory from launching an SSH process for every host at once.
      const queue = visible.values();
      async function refreshNextHost() {
        for (const info of queue) {
          if (revision !== cycle) return;
          await refreshHost(nodes.get(info.id));
        }
      }
      await Promise.all(Array.from({length: Math.min(4, visible.length)}, refreshNextHost));
    } catch {
      if (revision !== cycle) return;
      for (const host of nodes.values()) markStale(host);
      notice('호스트 목록을 확인할 수 없습니다. 새로고침으로 다시 연결하세요.');
    } finally {
      if (!disposed && revision === cycle && !doc.hidden) timer = win.setTimeout(refresh, 6000);
    }
  }
  const visibility = () => doc.hidden ? cancel() : refresh();
  get('refresh').addEventListener('click', refresh);
  doc.addEventListener('visibilitychange', visibility);
  refresh();
  return () => {
    disposed = true;
    cancel();
    doc.removeEventListener('visibilitychange', visibility);
    get('refresh').removeEventListener('click', refresh);
  };
}

if (typeof window !== 'undefined') mountOffice(window);
