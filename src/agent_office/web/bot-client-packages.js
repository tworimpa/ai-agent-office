export function mountBotPackages({ get, element }, { api, installed }) {
  const dialog = get('packageDialog'),
    form = get('packageForm'),
    previewButton = get('packageCheck'),
    installButton = get('packageInstall'),
    preview = get('packagePreview'),
    status = get('packageStatus');
  const controllers = new Set();
  let reviewed = null,
    busy = false,
    generation = 0;

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
  get('packageImport').onclick = () => {
    status.textContent = '';
    clearPreview();
    dialog.showModal();
  };
  get('packageCancel').onclick = () => dialog.close();
  dialog.addEventListener('close', () => {
    generation++;
    for (const controller of controllers) controller.abort();
    clearPreview();
    setBusy(false);
  });
  form.oninput = () => {
    clearPreview();
    status.textContent = '';
  };
  form.onsubmit = async (event) => {
    event.preventDefault();
    if (busy) return;
    clearPreview();
    const request = {
      url: get('packageUrl').value.trim(),
      version: get('packageVersion').value.trim(),
      path: get('packagePath').value.trim(),
    };
    const current = ++generation;
    setBusy(true);
    status.textContent = '공개 Git 저장소에서 패키지를 확인하는 중입니다.';
    try {
      const result = await api('/bots/package-preview', request, options);
      if (generation !== current) return;
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
    if (busy || !reviewed) return;
    const current = ++generation;
    setBusy(true);
    status.textContent = '확인한 패키지를 설치하는 중입니다.';
    try {
      const receipt = await api('/bots/package-install', reviewed, options);
      if (generation !== current) return;
      dialog.close();
      form.reset();
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
    dispose() {
      generation++;
      for (const controller of controllers) controller.abort();
    },
  };
}

const sourceVersion = (source) => [source.version, '커밋 ' + source.commit, '정의 해시 ' + source.digest];
const installedStamp = (bot) => JSON.stringify([bot.package, bot.package_rollback]);
const changeLabels = { added: '추가', modified: '수정', deleted: '삭제', kept: '유지' };

export function mountBotPackageChange({ get, element }, { api, refresh, changed }) {
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
  const options = { timeoutMs: 45000, controllers };
  let selected, session;

  function supports(action) {
    return selected?.package && (action === 'update' || selected.package_rollback);
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
  function open(action) {
    if (dialog.open || !supports(action)) return;
    session = {
      action,
      id: selected.id,
      stamp: installedStamp(selected),
      path: '/bots/' + encodeURIComponent(selected.id) + '/package-' + action,
    };
    const updating = action === 'update';
    get('packageChangeTitle').textContent = updating ? '봇 패키지 업데이트' : '봇 패키지 롤백';
    get('packageChangeTarget').hidden = !updating;
    version.value = '';
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
      ref = version.value.trim();
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
      const result = await api(active.path + '-preview', body, options);
      if (!stillChecking()) return;
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
      const receipt = await api(active.path, reviewed, options);
      if (session !== active) return;
      if (receipt.id !== active.id || !receipt.source) throw Error('패키지 변경 결과를 확인할 수 없습니다.');
      close();
      await refresh();
      changed(receipt, active.action);
    } catch (error) {
      if (session !== active) return;
      if (error.status === 409) await refresh();
      if (session !== active) return;
      if (selected?.id !== active.id || !supports(active.action)) {
        close();
      } else {
        active.stamp = installedStamp(selected);
        cancelPreview(error.message);
      }
    }
  };
  return {
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
      if (bot?.id !== session.id || !supports(session.action)) {
        close();
      } else {
        const stamp = installedStamp(bot);
        if (session.stamp !== stamp) {
          session.stamp = stamp;
          cancelPreview('Git 설치 출처가 바뀌었습니다. 변경을 다시 확인하세요.');
        }
      }
    },
    dispose: close,
  };
}
