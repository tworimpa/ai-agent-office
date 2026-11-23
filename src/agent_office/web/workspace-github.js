const names = {
  pending: '대기', linked: '목표 연결됨', open: '열림', closed: '닫힘', reopened: '다시 열림',
  skipped: '건너뜀', failed: '실패', published: '게시됨', queued: '대기열', delivered: '전달됨',
  posted: '게시됨', passed: '통과', running: '실행 중', none: '없음',
  idle: '대기', waiting: '접수 대기', observing: '관찰 중', ready_for_human_review: '검토 대기',
  waiting_for_user: '사용자 응답 대기', restart_required: '사용자 재시작 대기', comment_pending: '댓글 게시 대기',
  comment_ack_pending: '댓글 게시 확인 필요', label_actor_denied: '접수 라벨 권한 확인 필요', label_missing: '접수 라벨 없음',
  scope_accepted_restart_required: '범위 변경 후 재시작 대기', success: '통과', failure: '실패',
  pull_closed: 'PR 닫힘', merged: '병합됨', canceled: '중지됨', recovery_required: '복구 확인 필요',
  stale: '이전 후보', infrastructure_or_cancelled: '인프라 실패 또는 취소', untrusted_or_stale: '권한 또는 커밋 불일치',
  checkout_mismatch: '작업 폴더의 origin과 브랜치 확인', goal_intake_blocked: '실행 호스트의 엔진·폴더·검증 설정 확인',
  manual_workflow_review: 'CI 실행 정의는 별도 게시 검토 필요', manual_pr_publication: '사용자가 연결한 PR의 게시 확인 필요',
  base_branch_changed: '검증 뒤 대상 브랜치 변경', permission_denied: '저장소 권한 부족', rate_limited: 'API 요청 한도 대기',
  tls_verification_failed: '서버 인증서 및 사내 CA 확인', authentication_rejected: 'GitHub App 인증 확인',
  issue_comment: '이슈 댓글', issue_comment_edited: '이슈 댓글 수정',
  goal_changed: '목표 변경', goal_restarted: '목표 재시작', review_observed: 'PR 리뷰',
  ci_result: 'CI 결과', pull_linked: 'PR 연결', feedback_obsolete: '이전 피드백',
  question_answered: '질문에 답변', comment_retry: '댓글 게시 재시도',
  observed: '확인됨', accepted: '접수됨', needs_decision: '결정 필요',
};
const label = value => names[value] ?? value;
const localTime = value => {
  const date = new Date(typeof value === 'number' ? value * 1000 : value);
  return Number.isNaN(date.getTime()) ? '시간 미확인' : date.toLocaleString('ko-KR');
};

export function mountGithub({get, make, api, context, openGoal, uuid, desktop, briefing, pollSoon}) {
  const field = key => get('github-' + key);
  const note = (key, value) => { field(key).textContent = value; };
  const roomKey = () => JSON.stringify([context().host, context().id]);
  const path = () => '/rooms/' + context().id + '/github';
  let latest = null, viewKey = '', read = 0, formRead = 0, repoRead = 0, busy = false;
  let syncPending = false;
  let repositories = [];
  let cursor = null, previousPages = [], changingPage = false, selectedIssue = null, eventCursor = null, previousEvents = [];
  let detail = null, detailError = '', detailRead = 0;
  const cursorPath = value => value == null ? '' : '/page/' + encodeURIComponent(value);
  const option = (value, title) => {
    const node = make('option', title);
    node.value = String(value);
    return node;
  };
  function reset() {
    ++read; ++formRead; ++repoRead;
    latest = null;
    syncPending = false;
    cursor = null; previousPages = []; changingPage = false; resetDetail();
    busy = false;
    viewKey = '';
    field('issues').replaceChildren();
    field('inbox').hidden = field('settings').hidden = true;
    field('settings').open = false;
    note('action-note', '');
    note('settings-note', '');
    note('diagnostic', '');
  }
  function resetDetail() {
    ++detailRead;
    selectedIssue = eventCursor = detail = null;
    previousEvents = [];
    detailError = '';
  }
  async function loadDetail() {
    const id = selectedIssue, serial = ++detailRead, identity = roomKey();
    if (!id) return;
    try {
      const result = await api(path() + '/issues/' + id + cursorPath(eventCursor));
      if (serial !== detailRead || identity !== roomKey()) return;
      if (result.issue?.issue_id !== id) throw Error('이슈 ID가 일치하지 않습니다.');
      detail = result;
      detailError = '';
    } catch {
      if (serial !== detailRead || identity !== roomKey()) return;
      detail = null;
      detailError = '이슈 기록을 불러올 수 없습니다.';
    }
    if (latest) render();
  }
  async function changePage(next, backwards) {
    if (changingPage || !context().writable || (backwards ? !previousPages.length : next == null)) return;
    const identity = roomKey();
    changingPage = true;
    if (backwards) cursor = previousPages.pop();
    else { previousPages.push(cursor); cursor = next; }
    resetDetail();
    controls();
    try { await load(); }
    finally { if (identity === roomKey()) { changingPage = false; controls(); } }
  }
  function selectIssue(id) {
    const closing = selectedIssue === id;
    resetDetail();
    if (!closing) selectedIssue = id;
    render();
    if (!closing) loadDetail();
  }
  function changeEvents(next, backwards) {
    if (backwards) eventCursor = previousEvents.pop();
    else { previousEvents.push(eventCursor); eventCursor = next; }
    detail = null;
    render();
    loadDetail();
  }
  function addButton(parent, title, callback) {
    const button = make('button', title, 'secondary');
    button.type = 'button';
    button.addEventListener('click', callback);
    parent.append(button);
    return button;
  }
  async function action(endpoint, body, message) {
    if (busy || !context().writable || !latest) return;
    const identity = roomKey();
    busy = true;
    controls();
    try {
      await api(path() + endpoint, body);
      if (identity === roomKey()) {
        if (endpoint === '/sync') syncPending = true;
        note('action-note', message);
        await load();
        if (endpoint === '/sync' && needsStatus() && context().writable) pollSoon();
      }
    } catch (err) {
      if (identity === roomKey()) note('action-note', err.message);
    } finally {
      if (identity === roomKey()) { busy = false; controls(); }
    }
  }
  function pullForm(row) {
    const details = make('details'), form = make('form', '', 'settings-form');
    details.append(make('summary', row.pr_number ? '연결된 PR의 새 후보 확인' : '기존 PR 연결'));
    const input = (key, title, type) => {
      const node = make('input'), labelNode = make('label', title);
      node.type = type; node.id = 'issue-' + row.issue_id + '-' + key; node.name = key;
      node.required = true; labelNode.htmlFor = node.id; form.append(labelNode, node); return node;
    };
    const number = input('number', 'PR 번호', 'number'), sha = input('sha', '확인할 후보 커밋 SHA (선택, 40자리)', 'text');
    number.min = '1'; sha.required = false; sha.pattern = '[0-9a-f]{40}'; sha.spellcheck = false;
    if (row.pr_number) { number.value = String(row.pr_number); number.readOnly = true; }
    const link = make('button', row.pr_number ? '연결된 PR 커밋 확인' : '후보 커밋과 PR 연결', 'secondary');
    link.type = 'submit'; form.method = 'post'; form.setAttribute('aria-label', '이슈 #' + row.number + '에 PR 연결');
    form.append(link);
    form.addEventListener('submit', event => {
      event.preventDefault();
      action('/pull', {issue_id: row.issue_id, number: Number(number.value), ...(sha.value && {candidate_sha: sha.value})}, '기존 PR을 연결했습니다.');
    });
    details.append(form);
    return details;
  }
  function issueCard(row, connection, desk) {
    const card = make('article');
    card.dataset.issue = row.issue_id;
    const heading = make('h3', '#' + row.number + ' ' + row.title);
    card.append(heading);
    // The browser only navigates to an issue on the connection's configured origin.
    for (const [url, title] of [[row.url, '원본 이슈'], [row.pr_url, 'PR #' + row.pr_number]]) {
      try {
        const target = new URL(url), origin = new URL(connection.web_url);
        if (target.protocol !== 'https:' || target.origin !== origin.origin || target.username || target.password) continue;
        if (desktop) {
          const address = make('details'), summary = make('summary', title + ' 주소 보기'), input = make('input');
          input.readOnly = true;
          input.value = target.href;
          input.setAttribute('aria-label', title + ' 주소');
          input.addEventListener('focus', () => input.select());
          address.append(summary, input);
          card.append(address);
          continue;
        }
        const link = make('a', title, 'quiet');
        link.href = target.href;
        link.target = '_blank';
        link.rel = 'noopener noreferrer';
        card.append(link);
      } catch { /* An absent or invalid URL does not hide the local issue record. */ }
    }
    card.append(make('p', [label(row.state), label(row.status), row.publish_state && '게시 ' + label(row.publish_state),
      row.ci_state && 'CI ' + label(row.ci_state), row.last_error && label(row.last_error)].filter(Boolean).join(' · ')));
    if (row.run_id) {
      if (desk) {
        const owner = make('p', '', 'github-owner'), portrait = make('span', '', 'portrait');
        owner.dataset.person = desk.role;
        portrait.setAttribute('aria-hidden', 'true');
        owner.append(portrait, make('span', desk.label));
        card.append(owner);
      }
      addButton(card, '연결된 목표 열기', () => openGoal(row.run_id));
      const adjust = addButton(card, '목표 조정', () => briefing.edit(row.run_id));
      adjust.dataset.coordinate = row.run_id;
      card.append(pullForm(row));
    }
    if (row.publish_state === 'comment_ack_pending') {
      const details = make('details');
      details.append(make('summary', '상태 댓글의 게시 여부 확인'), make('p', 'GitHub가 게시 응답을 보내기 전에 연결이 끊겼습니다. 원본 이슈에서 기존 댓글을 확인하세요. 재시도하면 댓글이 중복될 수 있습니다.'));
      addButton(details, '원본 확인 후 댓글 게시 재시도', () => action('/retry-post', {issue_id: row.issue_id, id: uuid()}, '댓글 게시 재확인을 요청했습니다.'));
      card.append(details);
    }
    const reviewing = selectedIssue === row.issue_id;
    const loaded = reviewing && detail?.issue;
    addButton(card, reviewing ? '이슈 기록 접기' : '이슈 기록 및 최초 요청', () => selectIssue(row.issue_id));
    if (reviewing && !loaded) card.append(make('p', detailError || '이슈 기록을 불러오는 중입니다.'));
    if (loaded?.admission) {
      card.append(make('strong', '최초 요청'), make('pre', [loaded.admission.title, loaded.admission.body].filter(Boolean).join('\n')));
      if (loaded.initial_base) card.append(make('p', '최초 기준 커밋 · ' + loaded.initial_base));
    }
    if (row.scope_pending) {
      card.append(make('p', '목표 범위 변경을 검토하세요.'));
    }
    if (row.scope_pending && loaded?.scope_pending === row.scope_pending && loaded.previous_scope && loaded.proposed_scope) {
      for (const [title, scope] of [['현재 범위', loaded.previous_scope], ['제안된 범위', loaded.proposed_scope]]) {
        if (scope) card.append(make('strong', title), make('pre', [scope.title, scope.body].filter(Boolean).join('\n')));
      }
      for (const [value, title] of [['accept', '범위 변경 수락'], ['reject', '기존 범위 유지']]) {
        addButton(card, title, () => action('/decision', {issue_id: row.issue_id, event_id: row.scope_pending, action: value}, '범위 결정을 기록했습니다.'));
      }
    }
    if (row.state === 'open' && (row.restart_required || ['failed', 'canceled', 'recovery_required', 'plan_only'].includes(row.status))) {
      addButton(card, '새 목표로 다시 시작', () => action('/restart', {issue_id: row.issue_id, id: uuid()}, '새 목표를 요청했습니다.'));
    }
    if (loaded) {
      const history = make('section');
      history.append(make('strong', '이슈 기록'));
      const list = make('ol');
      for (const event of loaded.events ?? []) list.append(make('li', [event.at, label(event.kind), event.detail, label(event.status)].filter(Boolean).join(' · ')));
      history.append(list);
      if (!list.children.length) history.append(make('p', '기록이 없습니다.'));
      if (previousEvents.length) addButton(history, '이전 기록 페이지', () => changeEvents(null, true));
      if (detail.next_cursor != null) addButton(history, '다음 기록 페이지', () => changeEvents(detail.next_cursor, false));
      card.append(history);
    }
    return card;
  }
  function controls() {
    const blocked = busy || changingPage || !context().writable || !latest;
    const retryAt = latest?.status?.retry_at;
    const retryTime = typeof retryAt === 'number' ? retryAt * 1000 : Date.parse(retryAt);
    field('sync').disabled = blocked || !latest?.config || latest.status?.queued || retryTime > Date.now();
    field('prev').disabled = changingPage || !context().writable || !previousPages.length;
    field('next').disabled = blocked || latest?.next_cursor == null;
    for (const node of field('issues').querySelectorAll('button')) node.disabled = blocked;
    for (const node of field('issues').querySelectorAll('[data-coordinate]')) {
      node.hidden = !briefing.canEdit(node.dataset.coordinate);
      node.disabled = blocked || node.hidden;
    }
  }
  function render() {
    const {config, status = {}, issues = [], connections = []} = latest;
    field('settings').hidden = false;
    field('inbox').hidden = !config && !issues.length;
    field('pages').hidden = !previousPages.length && latest.next_cursor == null;
    note('status', [config?.full_name || config?.repository, config?.enabled ? '목표 자동 접수 켜짐' : '이슈 관찰 중 · 자동 접수 꺼짐',
      status.queued && '가져오기 요청 처리 중', status.last_sync ? '최근 동기화 ' + localTime(status.last_sync) : '동기화 기록 없음',
      status.retry_at && '다시 시도할 시각 ' + localTime(status.retry_at), status.last_error && '최근 오류 ' + label(status.last_error)].filter(Boolean).join(' · '));
    const desks = new Map(context().runs.map(run => {
      const role = run.state === 'REVIEWING' ? 'reviewer' : 'developer';
      const profile = run.assignment?.roles?.[role];
      const name = [role === 'reviewer' ? '검토자' : '담당자', profile?.name, run[role + '_engine'] ?? profile?.engine].filter(Boolean).join(' · ');
      return [run.id, {role, label: name}];
    }));
    const key = JSON.stringify([issues, config?.connection_id, issues.map(row => desks.get(row.run_id)), selectedIssue, detail, detailError]);
    if (key !== viewKey) {
      const connection = connections.find(item => item.id === config?.connection_id);
      const list = field('issues');
      const old = new Map([...list.children].map(card => [card.dataset.issue, card]));
      for (const row of issues) {
        const desk = desks.get(row.run_id);
        const stamp = JSON.stringify([row, connection?.web_url, desk, selectedIssue === row.issue_id ? [detail, detailError] : false]);
        const previous = old.get(String(row.issue_id));
        old.delete(String(row.issue_id));
        if (previous?.githubStamp === stamp) continue;
        const node = issueCard(row, connection, desk);
        node.githubStamp = stamp;
        if (previous) {
          const forms = previous.querySelectorAll('details');
          for (const detail of node.querySelectorAll('details')) {
            const match = [...forms].find(old => old.firstElementChild?.textContent === detail.firstElementChild?.textContent);
            if (match?.open) detail.open = true;
            for (const input of detail.querySelectorAll('input')) {
              const oldInput = match?.querySelector('[name="' + input.name + '"]');
              if (oldInput) input.value = oldInput.value;
            }
          }
          const focused = previous.ownerDocument.activeElement;
          const restoreInput = previous.contains(focused) && ['number', 'sha'].includes(focused.name) ? focused.name : null;
          previous.replaceWith(node);
          if (restoreInput) node.querySelector('[name="' + restoreInput + '"]')?.focus({preventScroll: true});
        } else list.append(node);
      }
      for (const card of old.values()) card.remove();
      viewKey = key;
    }
    controls();
  }
  async function load(readSection = api) {
    if (!context().supported) return;
    const sequence = ++read, identity = roomKey();
    try {
      const result = await readSection(path() + cursorPath(cursor));
      if (identity !== roomKey() || sequence !== read) return;
      latest = result;
      syncPending = latest.status?.queued === true;
      if (selectedIssue && !result.issues.some(row => row.issue_id === selectedIssue)) resetDetail();
      render();
      if (selectedIssue) await loadDetail();
    } catch {
      if (identity !== roomKey() || sequence !== read) return;
      field('settings').hidden = false;
      field('inbox').hidden = false;
      note('status', 'GitHub 동기화 상태를 불러올 수 없습니다.');
      latest = null;
      controls();
    }
  }
  function repoChoice() {
    const repo = repositories.find(item => String(item.id) === field('repo').value);
    field('branch').value = repo?.default_branch ?? '';
    note('diagnostic', '');
  }
  async function loadRepos(saved) {
    const serial = ++repoRead, identity = roomKey(), connection = field('connection').value;
    repositories = [];
    field('repo').replaceChildren(option('', '저장소 선택'));
    if (saved) field('repo').append(option(saved.repository_id, saved.repository || '#' + saved.repository_id));
    field('repo').value = saved ? String(saved.repository_id) : '';
    field('branch').value = saved?.branch ?? '';
    note('diagnostic', '');
    if (!connection) return;
    try {
      const result = await api(path() + '/repos/' + encodeURIComponent(connection));
      if (identity !== roomKey() || repoRead !== serial) return;
      repositories = result.repositories;
      const options = repositories.map(item => option(item.id, item.full_name));
      if (saved && !repositories.some(repo => repo.id === saved.repository_id)) {
        options.push(option(saved.repository_id, (saved.repository || '#' + saved.repository_id) + ' · 이전 연결'));
      }
      field('repo').replaceChildren(option('', '저장소 선택'), ...options);
      if (saved) {
        field('repo').value = String(saved.repository_id);
      }
      if (!repositories.length) note('diagnostic', '이 연결에서 선택할 저장소가 없습니다.');
    } catch (err) {
      if (identity === roomKey() && repoRead === serial) note('diagnostic', '저장소를 불러올 수 없습니다. ' + err.message);
    }
  }
  async function settings() {
    field('settings').hidden = !context().supported;
    if (!context().supported || !field('settings').open) return;
    const serial = ++formRead, identity = roomKey();
    field('form').hidden = true;
    note('settings-note', '연결 정보를 불러오는 중입니다.');
    await load();
    if (roomKey() !== identity || formRead !== serial) return;
    if (!latest) { note('settings-note', '연결 정보를 불러올 수 없습니다.'); return; }
    const {config, connections = []} = latest;
    if (!connections.length) {
      note('settings-note', '이 실행 호스트에 GitHub 연결이 없습니다. 호스트의 GitHub App 연결 설정을 확인하세요.' +
        (latest.status?.last_error ? ' · ' + label(latest.status.last_error) : ''));
      return;
    }
    const choice = field('connection');
    choice.replaceChildren(option('', '연결 선택'), ...connections.map(item => option(item.id, item.name + ' · ' + item.web_url)));
    choice.value = config?.connection_id ?? (connections.length === 1 ? connections[0].id : '');
    field('folder').replaceChildren(...context().folders.map((item, i) => option(i, typeof item === 'string' ? item : item.path)));
    field('folder').value = String(config?.folder_index ?? 0);
    field('label').value = config?.label ?? 'office:ready';
    for (const [key, prop] of [['actors', 'allowed_actors'], ['answers', 'answer_actors'], ['products', 'product_actors']]) {
      field(key).value = (config?.[prop] ?? []).join(', ');
    }
    field('enabled').checked = config?.enabled === true;
    field('draft').checked = config?.draft_pr === true;
    field('checks').value = (config?.ci_checks ?? []).join('\n');
    field('retries').value = String(config?.ci_retries ?? 0);
    note('host-note', '실행 호스트 · ' + context().name);
    note('settings-note', '');
    field('form').hidden = false;
    await loadRepos(config);
  }
  const binding = () => ({
    connection_id: field('connection').value, repository_id: Number(field('repo').value),
    folder_index: Number(field('folder').value), branch: field('branch').value.trim(),
  });
  async function submit(testOnly) {
    if (!context().writable || !latest || busy) return;
    for (const key of ['connection', 'repo', 'folder', 'branch']) if (!field(key).reportValidity()) return;
    const body = binding(), serial = formRead, identity = roomKey();
    const current = () => identity === roomKey() && formRead === serial;
    const target = testOnly ? 'diagnostic' : 'settings-note';
    if (!testOnly) {
      for (const [key, prop] of [['actors', 'allowed_actors'], ['answers', 'answer_actors'], ['products', 'product_actors']]) {
        const ids = field(key).value.trim().split(/[\s,]+/).filter(Boolean);
        if (ids.some(id => !/^[1-9]\d*$/.test(id) || !Number.isSafeInteger(Number(id)))) {
          note(target, '사용자 ID는 양의 정수로 입력하세요.');
          field(key).focus(); return;
        }
        body[prop] = [...new Set(ids.map(Number))];
      }
      Object.assign(body, {label: field('label').value.trim(), enabled: field('enabled').checked,
        draft_pr: field('draft').checked, ci_checks: field('checks').value.split('\n').map(s=>s.trim()).filter(Boolean),
        ci_retries: Number(field('retries').value)});
    }
    busy = true;
    const button = field(testOnly ? 'test' : 'save');
    button.disabled = true;
    note(target, testOnly ? '선택한 연결과 저장소를 검사하는 중입니다.' : '저장 중입니다.');
    try {
      // A verified binding can fetch a missing branch commit on the selected host (up to 120s).
      const result = await api(path() + (testOnly ? '/test' : ''), body, 150000);
      if (!current()) return;
      if (testOnly) {
        if (JSON.stringify(body) !== JSON.stringify(binding())) { note(target, '변경한 설정으로 다시 테스트하세요.'); return; }
        const d = result.diagnostic;
        note(target, [d?.ok ? '연결 확인됨' : '연결 실패', d?.message || d?.code, d?.kind, d?.version, d?.api_url,
          d?.repository, d?.base_sha,
          d?.authentication && '인증 ' + d.authentication, d?.proxy && '프록시 ' + d.proxy, d?.ca && '인증기관 ' + d.ca].filter(Boolean).join(' · '));
      } else {
        note(target, 'GitHub 연결을 저장했습니다.');
        await load();
      }
    } catch (err) { if (current()) note(target, err.message); }
    finally { if (roomKey() === identity) { busy = false; controls(); } button.disabled = false; }
  }
  field('connection').addEventListener('change', () => loadRepos(null));
  field('settings').addEventListener('toggle', () => { if (field('settings').open) settings(); });
  field('repo').addEventListener('change', repoChoice);
  for (const key of ['branch', 'folder']) field(key).addEventListener('input', () => note('diagnostic', ''));
  field('form').addEventListener('submit', event => { event.preventDefault(); submit(false); });
  field('test').addEventListener('click', () => submit(true));
  field('sync').addEventListener('click', () => action('/sync', {}, '가져오기를 요청했습니다.'));
  field('prev').addEventListener('click', () => changePage(null, true));
  field('next').addEventListener('click', () => changePage(latest?.next_cursor, false));
  function needsStatus() {
    return context().supported && (syncPending || !latest);
  }
  return {load, settings, reset, controls, needsStatus};
}
