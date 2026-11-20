const stages = {quick: '관련 테스트', full: '전체 회귀', journey: '사용자 여정'};
const statusText = {
  passed: '통과',
  failed: '실패',
  timeout: '시간 초과',
  unverified: '미검증',
  running: '실행 중',
  skipped: '건너뜀',
  scope_reduced: '검사 범위 축소',
  not_recorded: '기록 없음',
  not_verified: '미검증',
  verified: '검증됨',
  develop: '수정',
  developer: '담당자',
  reviewer: '검토자',
  meeting: '기획 회의',
  test: '테스트',
  review: '검토',
  verification: '검증',
  candidate: '후보 저장',
  plan: '계획 작성',
  plan_approved: '계획 승인',
  decision: '결정',
  direction: '추가 지시',
  delivered: '전달 완료',
  adopted: '후보 채택',
  STAGE_STARTED: '단계 시작',
  STAGE_FINISHED: '단계 종료',
  RUN_CREATED: '접수',
  WORKSPACE_CREATED: '작업 폴더 준비',
  RUN_FINISHED: '실행 종료',
  STAGE_COMPLETED: '단계 완료',
  STAGE_FAILED: '단계 실패',
  RETRY_SCHEDULED: '재시도 예정',
  pending: '대기 중',
  approved: '승인',
  changes_requested: '수정 요청'
};
const label = (value) => statusText[value] ?? value ?? '미확인';
const attemptLabel = (value) => (value === 0 ? '기준 커밋' : value + '차 시도');
const elapsed = (value) =>
  Number.isFinite(value) ? Math.floor(value / 60) + '분 ' + Math.floor(value % 60) + '초' : '미확인';
const formatted = (value) => {
  if (!value) return '미확인';
  const date = new Date(value);
  return Number.isNaN(date.valueOf()) ? '미확인' : date.toLocaleString('ko-KR');
};
const active = new Set(['QUEUED', 'RUNNING', 'WAITING_USER', 'TESTING', 'REVIEWING', 'STOP_REQUESTED']);

export function mountGoals({win, get, make, api, refresh, context, openInbox, openMeeting}) {
  let latest = null,
    counter = 0,
    policyRead = 0,
    fields = 0,
    reportKey = '',
    readError = false;
  const patches = new Map();
  const identity = () => JSON.stringify([context().hostId, context().workspaceId, context().runId]);
  const sameRoom = () => JSON.stringify([context().hostId, context().workspaceId]);
  function reset() {
    latest = null;
    ++counter;
    reportKey = '';
    readError = false;
    patches.clear();
    get('goal-report').hidden = true;
    get('goal-note').textContent = '';
    get('goal-record').replaceChildren();
  }
  function button(text, action) {
    const node = make('button', text, 'secondary');
    node.type = 'button';
    node.addEventListener('click', action);
    return node;
  }
  function section(title, id) {
    const node = make('section');
    node.dataset.goalSection = id;
    node.append(make('h3', title));
    return node;
  }
  function fold(title) {
    const node = make('details');
    node.append(make('summary', title));
    return node;
  }
  function lines(parent, values) {
    const list = make('ul');
    for (const value of values) list.append(make('li', value));
    parent.append(list);
  }
  function renderCheck(check) {
    const counts = Object.entries(check.counts ?? {}).map(([key, value]) => label(key) + ' ' + value + '개');
    const node = fold(
      [attemptLabel(check.attempt), stages[check.stage] ?? check.stage, label(check.status), ...counts].join(' · ')
    );
    node.dataset.goalCheck = check.stage;
    node.append(
      make('p', '시작 ' + formatted(check.started_at) + ' · 소요 ' + elapsed(check.elapsed_seconds)),
      make(
        'p',
        '실행 호스트 ' +
          (check.host || '미확인') +
          ' · 종료 코드 ' +
          (check.exit_code ?? '미확인') +
          ' · 성공 기준 ' +
          (check.pass_exit_code ?? 0)
      ),
      make('pre', JSON.stringify(check.argv)),
      make('p', check.cwd)
    );
    if (check.scope_changes?.length) {
      node.append(make('strong', '검사 범위 변경'));
      lines(node, check.scope_changes);
    }
    for (const [key, title] of [
      ['failed_ids', '실패한 검사'],
      ['fixed_ids', '기준 커밋에서 해결한 검사'],
      ['new_failures', '기준 커밋보다 새로 실패한 검사']
    ]) {
      if (check.attempt === 0 && key !== 'failed_ids') continue;
      if (check[key]?.length) {
        node.append(make('strong', title));
        lines(node, check[key]);
      }
    }
    if (check.previous_attempt != null) {
      node.append(make('p', attemptLabel(check.previous_attempt) + ' 대비 검사 결과'));
      for (const [key, title] of [
        ['fixed_since_previous', '해결'],
        ['failures_since_previous', '새 실패'],
        ['still_failing', '남은 실패']
      ]) {
        node.append(make('strong', title + ' ' + (check[key + '_count'] ?? 0) + '건'));
        if (check[key]?.length) lines(node, check[key]);
      }
    }
    if (check.summary) node.append(make('pre', check.summary));
    return node;
  }
  function render(goal) {
    const checks = goal.checks ?? [],
      candidates = goal.candidates ?? [],
      events = goal.events ?? [];
    get('goal-report').hidden = false;
    const running = checks.findLast((check) => check.status === 'running');
    const recent = events.at(-1);
    get('goal-progress').textContent =
      (goal.progress_uncertain ? '진행 불확실 · ' : '') +
      (running
        ? attemptLabel(running.attempt) + ' · ' + (stages[running.stage] ?? running.stage) + ' 실행 중'
        : goal.stop_reason ||
          goal.outcome ||
          (recent ? label(stages[recent.stage] ?? recent.stage) + ' · ' + label(recent.kind) : '접수됨'));
    get('goal-next').textContent = '다음 행동 · ' + (goal.next_action || '실행 호스트의 다음 기록을 기다립니다.');
    get('goal-activity').textContent =
      '마지막 진전 ' +
      formatted(goal.last_progress_at) +
      ' · 실제 작업 ' +
      elapsed(goal.active_seconds) +
      ' · 사용자 대기 ' +
      elapsed(goal.waiting_seconds);
    const key = JSON.stringify([
      goal.run_id,
      goal.state,
      goal.adopted_attempt,
      goal.best_attempt,
      goal.policy,
      checks,
      candidates,
      events,
      goal.recovery?.disposition,
      goal.recovery?.checkpoint,
      goal.recovery?.pending_directions,
      goal.recovery?.pending_decisions,
      goal.recovery?.receipts,
      goal.manual_checks,
      context().pending,
      context().writable,
      context().meetingPhase
    ]);
    if (reportKey === key) return;
    reportKey = key;
    const root = get('goal-record');
    const opened = new Set([...root.querySelectorAll('details[open]')].map((node) => node.dataset.key));
    root.replaceChildren();
    const brief = fold('원래 요청과 완료 기준');
    brief.append(make('p', goal.request), make('p', '기준 커밋 ' + (goal.base_commit || '미확인')));
    lines(brief, [
      ...(goal.scope ?? []).map((value) => '범위 · ' + value),
      ...(goal.criteria ?? []).map((value) => '기준 · ' + value)
    ]);
    if (goal.policy) {
      brief.append(
        make(
          'p',
          '작업 시간 한도 ' + elapsed(goal.policy.active_seconds) + ' · 최대 ' + goal.policy.max_attempts + '회'
        )
      );
      for (const check of goal.policy.checks ?? [])
        brief.append(
          make(
            'p',
            (stages[check.stage] ?? check.stage) +
              ' · ' +
              JSON.stringify(check.argv) +
              ' · ' +
              check.host +
              ' · ' +
              check.timeout_seconds +
              '초 · 성공 종료 코드 ' +
              check.pass_exit_code
          )
        );
    }
    const baseline = section('최초 실패', 'baseline');
    const first = checks.find((check) => check.attempt === 0 && check.status === 'failed');
    if (first) baseline.append(renderCheck(first));
    else baseline.append(make('p', '기준 커밋에서 확인된 실패 기록이 없습니다.'));
    const revisions = section('시도별 변경과 후보', 'candidates');
    revisions.append(
      make(
        'p',
        goal.best_attempt != null
          ? '검토할 최선 후보 · ' + attemptLabel(goal.best_attempt)
          : '검토할 최선 후보가 아직 없습니다.'
      )
    );
    for (const candidate of candidates) {
      const node = fold(
        attemptLabel(candidate.attempt) +
          ' · ' +
          label(candidate.status) +
          (candidate.attempt === goal.best_attempt ? ' · 최선 후보' : '') +
          (candidate.attempt === goal.adopted_attempt ? ' · 채택됨' : '')
      );
      node.dataset.goalAttempt = candidate.attempt;
      for (const [name, value] of [
        ['가설', candidate.hypothesis],
        ['변경', candidate.change],
        ['다음 행동', candidate.next_action]
      ])
        if (value) node.append(make('p', name + ' · ' + label(value)));
      if (candidate.files?.length && candidate.change !== candidate.files.join(', ')) lines(node, candidate.files);
      if (Number.isInteger(candidate.fixed_count))
        node.append(
          make('p', '해결한 실패 ' + candidate.fixed_count + '건 · 새 실패 ' + candidate.new_failure_count + '건')
        );
      if (candidate.scope_changes?.length) {
        node.append(make('strong', '변경되거나 누락된 범위'));
        lines(node, candidate.scope_changes);
      }
      for (const [key, title] of [
        ['review', '독립 검토'],
        ['consultation', '전문가 의견']
      ]) {
        if (!candidate[key + '_status']) continue;
        const evidence = fold(title + ' · ' + label(candidate[key + '_status']));
        if (candidate[key + '_at']) evidence.append(make('p', formatted(candidate[key + '_at'])));
        if (candidate[key + '_output']) evidence.append(make('pre', candidate[key + '_output']));
        node.append(evidence);
      }
      node.append(make('p', '변경 식별자 ' + candidate.digest));
      const patch = make('pre');
      patch.hidden = true;
      const diff = button('기준 커밋과 변경 비교', async () => {
        const key = identity();
        diff.disabled = true;
        try {
          const body = await api('/runs/' + goal.run_id + '/goal/candidates/' + candidate.attempt);
          if (key !== identity() || !diff.isConnected) return;
          if (body.attempt !== candidate.attempt || typeof body.diff !== 'string')
            throw Error('후보 변경을 확인할 수 없습니다.');
          patch.textContent = body.diff || '변경된 파일이 없습니다.';
          patches.set(candidate.digest, patch.textContent);
          patch.hidden = false;
          if (adopt) adopt.hidden = false;
        } catch (error) {
          if (key === identity()) get('goal-note').textContent = error.message;
        } finally {
          diff.disabled = false;
        }
      });
      diff.dataset.goalDiff = candidate.attempt;
      node.append(diff, patch);
      const previous = candidates.findLast((other) => other.attempt < candidate.attempt);
      if (previous) {
        const comparison = make('pre');
        comparison.hidden = true;
        const compare = button(attemptLabel(previous.attempt) + '와 변경 비교', async () => {
          if (!compare.isConnected) return;
          const key = identity();
          compare.disabled = true;
          try {
            const data = await api(
              '/runs/' + goal.run_id + '/goal/compare/' + previous.attempt + '/' + candidate.attempt
            );
            if (key !== identity() || !compare.isConnected) return;
            if (
              data.run_id !== goal.run_id ||
              data.from_attempt !== previous.attempt ||
              data.to_attempt !== candidate.attempt ||
              typeof data.diff !== 'string'
            )
              throw Error('두 후보의 변경을 확인할 수 없습니다.');
            comparison.textContent =
              (data.format === 'patches' ? '각 후보의 패치 비교' : '두 후보의 파일 변경') +
              (data.truncated ? ' · 출력 일부만 표시' : '') +
              '\n' +
              (data.diff || '두 후보의 차이가 없습니다.');
            comparison.hidden = false;
          } catch (error) {
            if (key === identity()) get('goal-note').textContent = error.message;
          } finally {
            compare.disabled = false;
          }
        });
        compare.dataset.goalCompare = previous.attempt + '-' + candidate.attempt;
        node.append(compare, comparison);
      }
      let adopt;
      if (
        !active.has(goal.state) &&
        goal.state !== 'RECOVERY_REQUIRED' &&
        candidate.eligible !== false &&
        candidate.attempt !== goal.adopted_attempt
      ) {
        adopt = button('이 후보 채택 기록', () =>
          mutate(
            goal,
            'adopt',
            {attempt: candidate.attempt, digest: candidate.digest, previous_digest: goal.adopted_digest ?? null},
            adopt
          )
        );
        adopt.hidden = true;
        adopt.disabled = !context().writable;
        adopt.dataset.goalAdopt = candidate.attempt;
        node.append(adopt);
      }
      if (patches.has(candidate.digest)) {
        patch.hidden = false;
        patch.textContent = patches.get(candidate.digest);
        if (adopt) adopt.hidden = false;
      }
      revisions.append(node);
    }
    const validation = section('실제 검증 범위', 'verification');
    for (const check of checks) validation.append(renderCheck(check));
    if (!checks.length) validation.append(make('p', '아직 실행된 검증이 없습니다.'));
    const manual = section('남은 수동 확인', 'manual');
    for (const check of goal.manual_checks ?? [])
      manual.append(make('p', typeof check === 'string' ? check : check.criterion + ' · ' + check.evidence));
    if (!goal.policy?.checks?.some((check) => check.stage === 'journey'))
      manual.append(
        make('p', '설치판에서 실제 사용자 여정을 확인하세요. 이 목표에 사용자 여정 검증이 지정되지 않았습니다.')
      );
    else if (
      !checks.some(
        (check) => check.stage === 'journey' && check.status === 'passed' && check.attempt === goal.best_attempt
      )
    )
      manual.append(make('p', '최선 후보의 사용자 여정 통과 기록을 확인해야 합니다.'));
    if (!(goal.manual_checks ?? []).length)
      manual.append(make('p', '완료 기준과 실제 검증 범위를 대조하고 후보 변경을 검토하세요.'));
    manual.append(
      make(
        'p',
        '후보 채택 ' +
          (goal.adopted_attempt != null ? attemptLabel(goal.adopted_attempt) : '기록 없음') +
          ' · 병합 ' +
          label(goal.merge_status) +
          ' · 배포 ' +
          label(goal.deployment_status)
      )
    );
    if (context().pending) manual.append(button('대기 중인 요청 열기', openInbox));
    if (goal.recovery?.pending_decisions?.length && !context().pending)
      manual.append(button('질문과 권한 요청 열기', openInbox));
    if (context().meetingPhase === 'awaiting_review') manual.append(button('계획 결정 열기', openMeeting));
    const recovery = goal.recovery;
    if (
      recovery &&
      (recovery.checkpoint ||
        recovery.receipts?.length ||
        recovery.pending_directions?.length ||
        recovery.pending_decisions?.length ||
        recovery.disposition === 'inspect')
    ) {
      const status = fold('실행기 복구 기록');
      status.append(make('p', recovery.reason));
      if (recovery.checkpoint)
        status.append(
          make(
            'p',
            '저장한 단계 · ' + label(recovery.checkpoint.stage) + ' · ' + formatted(recovery.checkpoint.created_at)
          )
        );
      for (const row of recovery.pending_directions ?? [])
        status.append(make('p', '미전달 지시 · ' + label(row.role) + ' · ' + row.id));
      for (const row of recovery.pending_decisions ?? [])
        status.append(make('p', '처리할 요청 · ' + label(row.role) + ' · ' + label(row.kind) + ' · ' + row.id));
      for (const row of recovery.receipts ?? [])
        status.append(make('p', formatted(row.created_at) + ' · ' + row.reason));
      manual.append(status);
    }
    if (goal.recovery?.disposition === 'resume_available') {
      const resume = button('확인된 지점에서 재개', () => mutate(goal, 'recover', {}, resume));
      resume.disabled = !context().writable;
      manual.append(resume);
    }
    const history = fold('목표 타임라인 · ' + events.length + '건');
    const list = make('ol');
    for (const event of events) {
      const row = make('li');
      row.append(
        make(
          'strong',
          [
            formatted(event.at),
            event.attempt != null ? attemptLabel(event.attempt) : '',
            event.stage && label(stages[event.stage] ?? event.stage),
            label(event.kind)
          ]
            .filter(Boolean)
            .join(' · ')
        ),
        make('p', typeof event.detail === 'string' ? event.detail : JSON.stringify(event.detail))
      );
      list.append(row);
    }
    history.append(list);
    root.append(brief, baseline, revisions, validation, manual, history);
    for (const node of root.querySelectorAll('details')) {
      node.dataset.key = node.parentElement.dataset.goalSection + ':' + node.querySelector('summary').textContent;
      if (opened.has(node.dataset.key)) node.open = true;
    }
  }
  async function mutate(goal, operation, body, node) {
    if (!context().writable || latest?.run_id !== goal.run_id || node.disabled) return;
    const current = identity();
    const key = [
      'office-goal-action',
      current,
      operation,
      JSON.stringify(body),
      goal.recovery?.checkpoint?.digest
    ].join(':');
    let id = node.dataset.operationId;
    try {
      id ||= win.sessionStorage.getItem(key);
    } catch {
      /* Current view still retains the request. */
    }
    id ||= win.crypto.randomUUID().replaceAll('-', '');
    node.dataset.operationId = id;
    try {
      win.sessionStorage.setItem(key, id);
    } catch {
      /* Current view still retains the request. */
    }
    body = {...body, id};
    node.disabled = true;
    try {
      await api('/runs/' + goal.run_id + '/goal/' + operation, body);
      try {
        win.sessionStorage.removeItem(key);
      } catch {
        /* In-memory receipt is enough for this view. */
      }
      if (current !== identity()) return;
      get('goal-note').textContent = operation === 'adopt' ? '후보 채택을 기록했습니다.' : '재개 요청을 접수했습니다.';
      await refresh();
    } catch (error) {
      if (current === identity()) {
        get('goal-note').textContent = error.message;
        if (error.status === 409) await refresh();
      }
    } finally {
      node.disabled = false;
    }
  }
  async function load(id) {
    const current = identity(),
      serial = ++counter;
    try {
      const {goal} = await api('/runs/' + id + '/goal');
      if (current !== identity() || serial !== counter) return;
      if (!goal) {
        reset();
        return;
      }
      if (goal.run_id !== id || goal.workspace_id !== context().workspaceId) throw Error();
      latest = goal;
      if (readError) get('goal-note').textContent = '';
      readError = false;
      render(goal);
    } catch {
      if (current !== identity() || serial !== counter) return;
      get('goal-report').hidden = false;
      readError = true;
      get('goal-note').textContent = '목표의 최신 상태를 확인할 수 없습니다.';
      get('goal-record')
        .querySelectorAll('button')
        .forEach((node) => {
          node.disabled = true;
        });
      reportKey = '';
    }
  }
  function row(check) {
    const group = make('fieldset');
    group.append(make('legend', '검증 단계'));
    const prefix = 'goal-check-' + ++fields + '-';
    function field(key, title, tag, value) {
      const input = make(tag);
      input.id = prefix + key;
      input.name = key;
      input.required = true;
      const titleNode = make('label', title);
      titleNode.htmlFor = input.id;
      group.append(titleNode, input);
      if (value != null) input.value = value;
      return input;
    }
    const stage = field('stage', '검증 종류', 'select');
    for (const [value, title] of Object.entries(stages)) {
      const option = make('option', title);
      option.value = value;
      stage.append(option);
    }
    stage.value = check.stage;
    const argv = field('argv', '명령과 인수', 'textarea', check.argv.join('\n'));
    argv.rows = Math.max(3, Math.min(6, check.argv.length));
    argv.spellcheck = false;
    argv.setAttribute('aria-describedby', 'goal-argv-help');
    field('host', '실행 호스트', 'input', check.host);
    const timeout = field('timeout_seconds', '제한 시간(초)', 'input', check.timeout_seconds);
    timeout.type = 'number';
    timeout.min = '1';
    timeout.max = '86400';
    const code = field('pass_exit_code', '성공 종료 코드(기본 제공 검사는 0)', 'input', check.pass_exit_code ?? 0);
    code.type = 'number';
    code.min = '0';
    code.max = '123';
    group.append(button('이 단계 삭제', () => group.remove()));
    get('goal-check-fields').append(group);
  }
  let policyReady = false,
    saving = false;
  function settings() {
    const supported = context().supported;
    get('goal-policy').hidden = !supported;
    get('goal-policy').open = false;
    policyReady = false;
    if (!supported) return;
    const serial = ++policyRead,
      current = sameRoom();
    get('goal-policy-form').hidden = true;
    get('goal-policy-note').textContent = '검증 설정을 불러오는 중입니다.';
    api('/rooms/' + context().workspaceId + '/goal-policy')
      .then(({policy}) => {
        if (serial !== policyRead || current !== sameRoom() || !get('room-settings').open) return;
        get('goal-seconds').value = policy.active_seconds;
        get('goal-attempts').value = policy.max_attempts;
        get('goal-check-fields').replaceChildren();
        for (const check of policy.checks) row(check);
        get('goal-policy-note').textContent = '';
        get('goal-policy-form').hidden = false;
        policyReady = true;
      })
      .catch(() => {
        if (serial === policyRead && current === sameRoom())
          get('goal-policy-note').textContent = '검증 설정을 불러올 수 없습니다. 설정을 다시 열어 주세요.';
      });
  }
  get('goal-add-check').addEventListener('click', () =>
    row({stage: 'journey', host: 'local', argv: [], timeout_seconds: 600})
  );
  get('goal-office-preset').addEventListener('click', () => {
    get('goal-check-fields').replaceChildren();
    for (const [stage, timeout_seconds, argv] of [
      ['quick', 1800, ['python', '-m', 'pytest', '-q', '-x']],
      ['full', 7200, ['python', '-m', 'pytest', '-q']],
      ['full', 1800, ['npm', 'run', 'test:web']],
      ['journey', 1800, ['node', 'desktop/verify-install.mjs', '<installed-app>']]
    ])
      row({stage, host: 'local', argv, timeout_seconds});
    get('goal-policy-note').textContent = 'Python, 웹, 설치판 여정 검증을 불러왔습니다. 적용하려면 저장하세요.';
  });
  get('goal-policy-form').addEventListener('submit', async (event) => {
    event.preventDefault();
    if (!context().writable || !policyReady || saving) return;
    const current = sameRoom(),
      serial = policyRead;
    const checks = [...get('goal-check-fields').children].map((group) => {
      const value = (key) => group.querySelector('[name="' + key + '"]').value;
      return {
        stage: value('stage'),
        host: value('host').trim(),
        argv: value('argv')
          .split('\n')
          .map((x) => x.trim())
          .filter(Boolean),
        timeout_seconds: Number(value('timeout_seconds')),
        pass_exit_code: Number(value('pass_exit_code'))
      };
    });
    saving = true;
    get('goal-save-policy').disabled = true;
    try {
      await api('/rooms/' + context().workspaceId + '/goal-policy', {
        active_seconds: Number(get('goal-seconds').value),
        max_attempts: Number(get('goal-attempts').value),
        checks
      });
      if (current === sameRoom() && serial === policyRead) {
        get('goal-policy-note').textContent =
          '검증 설정을 저장했습니다. 다음 파일 수정 작업부터 밤샘 목표가 기본으로 선택됩니다.';
        await refresh();
      }
    } catch (error) {
      if (current === sameRoom() && serial === policyRead) get('goal-policy-note').textContent = error.message;
    } finally {
      saving = false;
      get('goal-save-policy').disabled = false;
    }
  });
  return {reset, load, settings};
}
