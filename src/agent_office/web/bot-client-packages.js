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
