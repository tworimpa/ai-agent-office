const sameRepository = (source, expected) => source?.url === expected.url && source.path === expected.path;
const matchesPackage = (source, expected) =>
  sameRepository(source, expected) && source.commit === expected.commit;
const catalogMismatch = '패키지 미리보기가 카탈로그 항목과 일치하지 않습니다. 카탈로그를 새로고침하고 다시 확인하세요.';

function skillLink(href, entryId) {
  if (typeof href !== 'string') return false;
  const match = /^\/bot-catalog\/links\/([a-z0-9]+(?:-[a-z0-9]+)*)\/[a-f0-9]{32}$/.exec(href);
  return match?.[0] === href && match[1] === entryId;
}

export function mountBotCatalog({ get, element, button }, { api, botById, workspaceName, packages, packageChange }) {
  const dialog = get('botCatalogDialog'),
    search = get('botCatalogSearch'),
    refresh = get('botCatalogRefresh'),
    status = get('botCatalogStatus'),
    results = get('botCatalogResults'),
    detail = get('botCatalogDetails'),
    action = get('botCatalogAction');
  const controllers = new Set();
  let entries = [],
    selected = null,
    generation = 0,
    loaded = false;

  function sourceDetails(source, versionLabel) {
    return [
      ...(source.version ? [[versionLabel, source.version]] : []),
      ['Git 저장소', source.url],
      ['패키지 경로', source.path || '저장소 루트'],
      ['커밋', source.commit],
    ];
  }

  function metadata(rows) {
    const list = element('dl');
    for (const [label, value] of rows) list.append(element('dt', label), element('dd', value));
    return list;
  }

  function renderDetail() {
    detail.hidden = !selected;
    if (!selected) return;
    const entry = selected,
      existing = botById(entry.botId),
      source = existing?.package,
      sameSource = sameRepository(source, entry.package),
      sameCommit = matchesPackage(source, entry.package);
    get('botCatalogSelection').textContent = entry.name;
    const info = get('botCatalogInfo');
    info.replaceChildren(
      element('p', entry.description),
      metadata([
        ['설치 대상 작업실', workspaceName()],
        ['게시자', entry.publisher],
        ['역할', entry.role],
        ...(entry.tags.length ? [['태그', entry.tags.join(', ')]] : []),
        ...sourceDetails(entry.package, '배포 버전'),
      ]),
    );
    if (entry.skills.length) {
      const skills = element('ul');
      for (const skill of entry.skills) {
        const row = element('li');
        row.append(element('strong', skill.name), element('div', skill.path));
        if (skillLink(skill.href, entry.id)) {
          const link = element('a', 'CODE-Skills에서 보기');
          link.href = skill.href;
          link.target = '_blank';
          link.rel = 'noopener noreferrer';
          link.setAttribute('aria-label', skill.name + ' · CODE-Skills에서 보기');
          row.append(link);
        }
        skills.append(row);
      }
      info.append(element('strong', '포함 스킬'), skills);
    }
    if (existing) {
      const title = sameCommit ? '이미 설치됨' : sameSource ? '설치된 버전' : '같은 봇 ID가 설치되어 있습니다.';
      info.append(element('strong', title));
      if (source) info.append(metadata(sourceDetails(source, '설치 버전')));
      if (!sameSource) info.append(element('div', '설치된 봇의 출처가 카탈로그와 다릅니다.'));
    }
    action.hidden = sameCommit;
    action.textContent = sameSource ? '업데이트 미리보기' : '설치 미리보기';
  }

  function renderResults() {
    const normalize = (value) => value.normalize('NFC').toLowerCase();
    const terms = normalize(search.value).trim().split(/\s+/).filter(Boolean);
    const matching = entries.filter((entry) => {
      const words = normalize([entry.name, entry.role, ...entry.tags].join(' '));
      return terms.every((term) => words.includes(term));
    });
    if (!matching.includes(selected)) selected = null;
    results.replaceChildren(
      ...matching.map((entry) => {
        const row = element('li');
        const choose = button('', () => {
          selected = entry;
          for (const other of results.querySelectorAll('button'))
            other.setAttribute('aria-pressed', String(other === choose));
          renderDetail();
        });
        choose.setAttribute('aria-pressed', String(selected === entry));
        choose.append(element('span', entry.name), element('small', entry.role));
        row.append(choose);
        return row;
      }),
    );
    if (loaded)
      status.textContent = matching.length ? matching.length + '개 봇' : terms.length ? '검색 결과가 없습니다.' : '등록된 봇이 없습니다.';
    renderDetail();
  }

  function invalidate() {
    generation++;
    for (const controller of controllers) controller.abort();
  }

  async function load() {
    invalidate();
    const current = generation,
      previousId = selected?.id;
    loaded = false;
    refresh.disabled = true;
    entries = [];
    selected = null;
    renderResults();
    status.textContent = '봇 목록을 불러오는 중입니다.';
    try {
      const result = await api('/bot-catalog', undefined, { controllers });
      if (generation !== current || !dialog.open) return;
      if (result?.schemaVersion !== 1 || !Array.isArray(result.entries)) throw Error();
      entries = result.entries;
      selected = entries.find((entry) => entry.id === previousId) ?? null;
      loaded = true;
      renderResults();
    } catch {
      if (generation !== current || !dialog.open) return;
      loaded = false;
      entries = [];
      selected = null;
      renderResults();
      status.textContent = '봇 목록을 불러오지 못했습니다. 새로고침으로 다시 시도하세요.';
    } finally {
      if (generation === current) refresh.disabled = false;
    }
  }

  get('botCatalogOpen').onclick = () => {
    if (dialog.open) return;
    dialog.showModal();
    load();
  };
  refresh.onclick = load;
  search.oninput = renderResults;
  get('botCatalogClose').onclick = () => dialog.close();
  dialog.addEventListener('close', () => {
    if (!dialog.open) invalidate();
  });
  action.onclick = () => {
    const entry = selected;
    if (!entry || !dialog.open) return;
    const existing = botById(entry.botId);
    if (matchesPackage(existing?.package, entry.package)) return;
    dialog.close();
    if (sameRepository(existing?.package, entry.package)) packageChange.openUpdate(existing, entry.package);
    else packages.open(entry);
  };
  return {
    render() {
      if (dialog.open) renderDetail();
    },
    dispose: invalidate,
  };
}

export function mountBotPackages({ get, element }, { api, installed }) {
  const dialog = get('packageDialog'),
    form = get('packageForm'),
    previewButton = get('packageCheck'),
    installButton = get('packageInstall'),
    preview = get('packagePreview'),
    status = get('packageStatus');
  const fields = { url: get('packageUrl'), version: get('packageVersion'), path: get('packagePath') };
  const controllers = new Set();
  let reviewed = null,
    busy = false,
    generation = 0,
    expected = null,
    manualDraft = null;

  function clearPreview() {
    reviewed = null;
    preview.hidden = true;
    preview.replaceChildren();
    installButton.disabled = true;
  }

  function setBusy(value) {
    busy = value;
    get('packageFields').disabled = value;
    previewButton.disabled = value;
    installButton.disabled = value || !reviewed;
  }

  function showPreview(result) {
    const version = result.package_version || result.source.version;
    preview.replaceChildren(
      element('strong', result.name + ' · ' + result.role),
      element('div', (result.package_name || result.name) + ' · ' + version),
      element('div', '커밋 ' + result.source.commit),
      element('div', '정의 해시 ' + result.digest),
      element('ul'),
    );
    const files = preview.querySelector('ul');
    for (const path of result.files) files.append(element('li', path));
    preview.hidden = false;
  }

  const options = { timeoutMs: 45000, controllers };
  function readFields() {
    return Object.fromEntries(Object.entries(fields).map(([key, field]) => [key, field.value]));
  }
  function writeFields(values, locked) {
    for (const [key, field] of Object.entries(fields)) {
      field.value = values[key];
      field.readOnly = locked;
    }
  }
  function resetCatalogSelection() {
    if (manualDraft) writeFields(manualDraft, false);
    expected = manualDraft = null;
  }
  function invalidate() {
    generation++;
    for (const controller of controllers) controller.abort();
  }
  function open(entry = null) {
    if (dialog.open) return;
    invalidate();
    resetCatalogSelection();
    if (entry) {
      const { url, path, commit } = entry.package;
      manualDraft = readFields();
      expected = { botId: entry.botId, package: entry.package, request: { url, path, version: commit } };
      writeFields(expected.request, true);
    }
    status.textContent = '';
    clearPreview();
    setBusy(false);
    dialog.showModal();
  }
  get('packageImport').onclick = () => open();
  get('packageCancel').onclick = () => dialog.close();
  dialog.addEventListener('close', () => {
    if (dialog.open) return;
    invalidate();
    resetCatalogSelection();
    clearPreview();
    setBusy(false);
  });
  form.oninput = () => {
    clearPreview();
    status.textContent = '';
  };
  form.onsubmit = async (event) => {
    event.preventDefault();
    if (busy || !dialog.open) return;
    clearPreview();
    const request = expected?.request
      ?? Object.fromEntries(Object.entries(readFields()).map(([key, value]) => [key, value.trim()]));
    const current = ++generation;
    setBusy(true);
    status.textContent = '공개 Git 저장소에서 패키지를 확인하는 중입니다.';
    try {
      const result = await api('/bots/package-preview', request, options);
      if (generation !== current || !dialog.open) return;
      if (expected && (result.bot_id !== expected.botId || !matchesPackage(result.source, expected.package))) {
        status.textContent = catalogMismatch;
        return;
      }
      showPreview(result);
      reviewed = { ...request, commit: result.source.commit, digest: result.digest };
      status.textContent = '커밋과 파일을 확인한 뒤 설치하세요.';
    } catch (error) {
      if (generation === current) status.textContent = error.message;
    } finally {
      if (generation === current) setBusy(false);
    }
  };
  installButton.onclick = async () => {
    if (busy || !reviewed || !dialog.open) return;
    const current = ++generation;
    setBusy(true);
    status.textContent = '확인한 패키지를 설치하는 중입니다.';
    try {
      const receipt = await api('/bots/package-install', reviewed, options);
      if (generation !== current) return;
      if (!expected) form.reset();
      dialog.close();
      await installed(receipt.id);
    } catch (error) {
      if (generation === current) {
        if (error.status === 409) clearPreview();
        status.textContent = error.message;
      }
    } finally {
      if (generation === current) setBusy(false);
    }
  };
  return {
    open,
    dispose: invalidate,
  };
}

const sourceVersion = (source) => [source.version, '커밋 ' + source.commit, '정의 해시 ' + source.digest];
const installedStamp = (bot) => JSON.stringify([bot.package, bot.package_rollback]);
const changeLabels = { added: '추가', modified: '수정', deleted: '삭제', kept: '유지' };

export function mountBotPackageChange({ get, element }, { api, refresh, changed, botById }) {
  const dialog = get('packageChangeDialog'),
    form = get('packageChangeForm'),
    version = get('packageChangeVersion'),
    preview = get('packageChangePreview'),
    conflicts = get('packageChangeConflicts'),
    status = get('packageChangeStatus'),
    check = get('packageChangeCheck'),
    apply = get('packageChangeApply'),
    cancel = get('packageChangeCancel'),
    updateButton = get('packageUpdate'),
    rollbackButton = get('packageRollback');
  const controllers = new Set();
  // An edited legacy install can need both its original commit and the candidate.
  const requestOptions = (action) => ({ timeoutMs: action === 'update' ? 75000 : 15000, controllers });
  let selected, session;

  function supports(action, bot) {
    return bot?.package && (action === 'update' || bot.package_rollback);
  }
  function sessionBot(active) {
    return active.expected ? botById(active.id) : selected;
  }
  function syncControls() {
    version.disabled = check.disabled = !!session?.phase;
    cancel.disabled = session?.phase === 'apply';
    apply.disabled = !!session?.phase || !session?.review;
  }
  function clearPreview() {
    if (session) session.review = null;
    for (const panel of [preview, conflicts]) {
      panel.hidden = true;
      panel.replaceChildren();
    }
    syncControls();
  }
  function cancelPreview(message = '') {
    session.phase = null;
    session.request = null;
    for (const controller of controllers) controller.abort();
    clearPreview();
    status.textContent = message;
  }
  function close() {
    session = null;
    for (const controller of controllers) controller.abort();
    clearPreview();
    if (dialog.open) dialog.close();
  }
  function rows(title, entries) {
    const list = element('ul');
    list.tabIndex = 0;
    list.append(...entries.map((entry) => element('li', entry)));
    return [element('h3', title), list];
  }
  function showPreview(result, action) {
    const comparison = element('div', null, 'package-comparison');
    for (const [label, source] of [
      ['현재 버전', result.current],
      [action === 'update' ? '후보 버전' : '복원할 버전', result.target],
    ]) {
      const card = element('section');
      card.append(element('h3', label), ...sourceVersion(source).map((value) => element('div', value)));
      comparison.append(card);
    }
    preview.replaceChildren(comparison);
    if (result.changes.length)
      preview.append(...rows('변경 내역', result.changes.map(({ path, action }) => changeLabels[action] + ' · ' + path)));
    preview.hidden = false;
    if (result.conflicts.length) {
      conflicts.replaceChildren(...rows('충돌 경로', result.conflicts));
      conflicts.hidden = false;
      status.textContent = '충돌을 해결한 뒤 다시 확인하세요.';
    } else if (result.unchanged) {
      status.textContent = '이미 이 버전이 설치되어 있습니다.';
    } else {
      status.textContent = '커밋과 변경을 확인한 뒤 적용하세요.';
    }
  }
  function open(action, bot = selected, expected = null) {
    if (dialog.open || !supports(action, bot)) return;
    session = {
      action,
      id: bot.id,
      stamp: installedStamp(bot),
      path: '/bots/' + encodeURIComponent(bot.id) + '/package-' + action,
      expected,
    };
    const updating = action === 'update';
    get('packageChangeTitle').textContent = updating ? '봇 패키지 업데이트' : '봇 패키지 롤백';
    get('packageChangeTarget').hidden = !updating;
    version.value = expected?.commit ?? '';
    version.readOnly = !!expected;
    version.required = updating;
    apply.textContent = updating ? '업데이트 적용' : '롤백 적용';
    status.textContent = '';
    clearPreview();
    dialog.showModal();
    if (updating) version.focus();
    else checkChanges();
  }
  async function checkChanges() {
    const active = session;
    if (!active || !dialog.open || active.phase) return;
    const updating = active.action === 'update',
      ref = active.expected?.commit ?? version.value.trim();
    if (updating && !ref) {
      status.textContent = 'Git 태그 또는 전체 커밋 SHA를 입력하세요.';
      return;
    }
    const body = (active.request = updating ? { version: ref } : {});
    active.phase = 'preview';
    clearPreview();
    status.textContent = updating ? '공개 Git 저장소에서 후보 버전을 확인하는 중입니다.' : '직전 버전을 확인하는 중입니다.';
    const stillChecking = () => dialog.open && session === active && active.request === body;
    try {
      const result = await api(active.path + '-preview', body, requestOptions(active.action));
      if (!stillChecking()) return;
      if (active.expected && !matchesPackage(result.target, active.expected)) {
        status.textContent = catalogMismatch;
        return;
      }
      showPreview(result, active.action);
      if (!result.unchanged && !result.conflicts.length && typeof result.token === 'string' && result.token)
        active.review = { ...body, token: result.token };
    } catch (error) {
      if (stillChecking()) status.textContent = error.message;
    } finally {
      if (stillChecking()) {
        active.phase = null;
        syncControls();
      }
    }
  }
  updateButton.onclick = () => open('update');
  rollbackButton.onclick = () => open('rollback');
  cancel.onclick = close;
  dialog.addEventListener('cancel', (event) => {
    if (session?.phase === 'apply') event.preventDefault();
  });
  dialog.addEventListener('close', () => {
    if (!dialog.open) close();
  });
  form.onsubmit = (event) => {
    event.preventDefault();
    return checkChanges();
  };
  version.oninput = () => {
    if (session && session.phase !== 'apply') cancelPreview();
  };
  apply.onclick = async () => {
    const active = session;
    if (!active?.review || !dialog.open || active.phase) return;
    if (active.action === 'update' && active.review.version !== version.value.trim()) {
      cancelPreview('변경을 다시 확인하세요.');
      return;
    }
    const reviewed = active.review;
    active.review = null;
    active.phase = 'apply';
    syncControls();
    status.textContent = '확인한 변경을 적용하는 중입니다.';
    try {
      const receipt = await api(active.path, reviewed, requestOptions(active.action));
      if (session !== active) return;
      if (receipt.id !== active.id || !receipt.source) throw Error('패키지 변경 결과를 확인할 수 없습니다.');
      close();
      await refresh();
      changed(receipt, active.action, !!active.expected);
    } catch (error) {
      if (session !== active) return;
      if (error.status === 409) await refresh();
      if (session !== active) return;
      const owner = sessionBot(active);
      if (owner?.id !== active.id || !supports(active.action, owner)) {
        close();
      } else {
        active.stamp = installedStamp(owner);
        const message = error.code === 'REQUEST_CONFLICT'
          ? '미리보기 이후 적용 조건이 바뀌었습니다. 변경을 다시 확인하세요.'
          : error.message;
        cancelPreview(message);
      }
    }
  };
  return {
    openUpdate(bot, expected) {
      open('update', bot, expected);
    },
    render(bot) {
      selected = bot;
      const source = bot?.package,
        rollback = bot?.package_rollback;
      get('packageSourceDetails').hidden = updateButton.hidden = !source;
      get('packageSource').textContent = source
        ? [source.url, source.path, ...sourceVersion(source)].filter(Boolean).join('\n')
        : '';
      get('packagePrevious').hidden = rollbackButton.hidden = !source || !rollback;
      get('packageRollbackSource').textContent = rollback ? sourceVersion(rollback).join('\n') : '';
      if (!session || session.phase === 'apply') return;
      const owner = sessionBot(session);
      if (owner?.id !== session.id || !supports(session.action, owner)) {
        close();
      } else {
        const stamp = installedStamp(owner);
        if (session.stamp !== stamp) {
          session.stamp = stamp;
          cancelPreview('Git 설치 출처가 바뀌었습니다. 변경을 다시 확인하세요.');
        }
      }
    },
    dispose: close,
  };
}
