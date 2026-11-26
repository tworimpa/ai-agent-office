const parentOf = (path) => path.split('/').slice(0, -1).join('/');
const basename = (path) => path.split('/').at(-1);
const byName = new Intl.Collator('ko', { numeric: true }).compare;

function directories(data) {
  const dirs = new Set(['']);
  for (const path of [...(data.folders || []), ...Object.keys(data.files).map(parentOf)]) {
    let dir = path;
    while (dir && !dirs.has(dir)) {
      dirs.add(dir);
      dir = parentOf(dir);
    }
  }
  return dirs;
}

export function mountFiles(ui, { api, bots, current, view, persist, activate, tabs, note }) {
  const { get, button, element } = ui;
  const assets = new Map(),
    reads = new Map(),
    writes = new Set();
  const openRoots = new Set(),
    expanded = new Map(),
    parents = new Map();
  let creating, comparison;
  const folders = (id) => {
    if (!expanded.has(id)) expanded.set(id, new Set());
    return expanded.get(id);
  };
  function reveal(id, path) {
    parents.set(id, path);
    openRoots.add(id);
    while (path) {
      folders(id).add(path);
      path = parentOf(path);
    }
  }
  function filesFor(bot) {
    return '/bots/' + encodeURIComponent(bot.id) + '/assets';
  }
  function isEditor(bot) {
    return current()?.id === bot.id && view(bot).active.startsWith('f:');
  }
  function repaint() {
    tree();
    const bot = current();
    if (bot && isEditor(bot)) editor(bot);
  }
  async function load(bot, force = false) {
    if (!force && assets.has(bot.id)) return assets.get(bot.id);
    if (reads.has(bot.id)) return reads.get(bot.id);
    const read = api(filesFor(bot))
      .then((data) => {
        if (reads.get(bot.id) === read) {
          assets.set(bot.id, data);
          repaint();
        }
        return data;
      })
      .finally(() => {
        if (reads.get(bot.id) === read) reads.delete(bot.id);
      });
    reads.set(bot.id, read);
    return read;
  }
  function ensure(bot) {
    load(bot).catch((err) => note(err.message));
  }
  function branch(bot, data, dirs, parent) {
    const ul = element('ul', null, 'asset-tree');
    const items = [...dirs].filter((dir) => dir && parentOf(dir) === parent).sort(byName);
    for (const dir of items) {
      const open = folders(bot.id).has(dir),
        li = element('li');
      const toggle = button(
        '',
        () => {
          parents.set(bot.id, dir);
          if (open) folders(bot.id).delete(dir);
          else folders(bot.id).add(dir);
          tree();
          Array.from(get('explorer').querySelectorAll('[data-dir]'))
            .find((row) => row.dataset.owner === bot.id && row.dataset.dir === dir)
            ?.focus();
        },
        'frow dir-row',
      );
      toggle.dataset.owner = bot.id;
      toggle.dataset.dir = dir;
      toggle.setAttribute('aria-expanded', String(open));
      toggle.classList.toggle('chosen-dir', parents.get(bot.id) === dir);
      toggle.append(element('span', open ? '▾' : '▸', 'tree-sign'), element('span', basename(dir)));
      li.append(toggle);
      if (open) li.append(branch(bot, data, dirs, dir));
      ul.append(li);
    }
    for (const path of Object.keys(data.files)
      .filter((path) => parentOf(path) === parent)
      .sort(byName)) {
      const li = element('li');
      const file = button('', () => activate(bot, 'f:' + path), 'frow');
      file.title = path;
      file.classList.toggle('active', isEditor(bot) && view(bot).active === 'f:' + path);
      file.append(element('span', '▤', 'tree-sign'), element('span', basename(path)));
      li.append(file);
      ul.append(li);
    }
    return ul;
  }
  function tree() {
    const list = get('explorer');
    list.replaceChildren(
      ...bots().map((bot) => {
        const group = element('details', null, 'folder');
        const wasOpen = openRoots.has(bot.id);
        group.open = wasOpen;
        group.append(element('summary', bot.name + ' · ' + bot.role));
        group.ontoggle = () => {
          if (!group.isConnected) return;
          if (group.open) {
            openRoots.add(bot.id);
            if (!wasOpen) tree();
            ensure(bot);
          } else openRoots.delete(bot.id);
        };
        if (group.open) {
          const data = assets.get(bot.id);
          if (!data) group.append(element('small', '파일 불러오는 중…'));
          else {
            const bar = element('div', null, 'asset-tools');
            for (const [kind, label] of [
              ['folder', '새 폴더'],
              ['file', '새 파일'],
            ])
              bar.append(button(label, () => openCreate(bot, kind)));
            group.append(bar, branch(bot, data, directories(data), ''));
          }
        }
        return group;
      }),
    );
  }
  function openCreate(bot, kind) {
    const data = assets.get(bot.id);
    creating = { bot, kind, digest: data.digest };
    get('entryParent').replaceChildren(
      ...[...directories(data)].sort(byName).map((dir) => {
        const option = element('option', bot.name + ' / ' + (dir || '(최상위)'));
        option.value = dir;
        return option;
      }),
    );
    get('entryParent').value = parents.get(bot.id) || '';
    get('entryTitle').textContent = kind === 'folder' ? '새 폴더' : '새 파일';
    get('entryName').value = '';
    get('entryError').textContent = '';
    get('entryDialog').showModal();
  }
  function accept(bot, data, from) {
    assets.set(bot.id, data);
    reads.delete(bot.id);
    for (const draft of Object.values(view(bot).files)) if (draft.digest === from) draft.digest = data.digest;
    persist();
  }
  get('entryCancel').onclick = () => get('entryDialog').close();
  get('entryForm').onsubmit = async (event) => {
    event.preventDefault();
    const { bot, kind, digest } = creating;
    if (writes.has(bot.id)) return;
    const filename = [get('entryParent').value, get('entryName').value.trim()].filter(Boolean).join('/');
    const submit = get('entryForm').querySelector('[type=submit]');
    writes.add(bot.id);
    submit.disabled = true;
    try {
      const data = await api(filesFor(bot), { filename, content: '', digest, kind, create: true });
      accept(bot, data, digest);
      reveal(bot.id, kind === 'file' ? parentOf(filename) : filename);
      get('entryDialog').close();
      if (kind === 'file') activate(bot, 'f:' + filename);
      else tree();
    } catch (err) {
      get('entryError').textContent = err.message;
      if (err.code === 'REQUEST_CONFLICT') {
        try {
          const data = await load(bot, true);
          creating.digest = data.digest;
        } catch (loadError) {
          get('entryError').textContent = loadError.message;
        }
      }
    } finally {
      writes.delete(bot.id);
      submit.disabled = false;
      repaint();
    }
  };
  function editor(bot) {
    const path = view(bot).active.slice(2),
      key = 'file:' + path;
    const data = assets.get(bot.id),
      draft = view(bot).files[key];
    get('fileowner').textContent = bot.name + ' / 봇 파일';
    get('filename').textContent = path;
    const exists = data && Object.hasOwn(data.files, path),
      conflict = draft && data && draft.digest !== data.digest;
    const input = get('text'),
      value = draft?.text ?? (exists ? data.files[path] : '');
    if (input.value !== value) input.value = value;
    input.readOnly = !exists || writes.has(bot.id);
    get('save').disabled = !draft || !exists || writes.has(bot.id) || !!conflict;
    get('fileStatus').textContent = !data
      ? '파일을 불러오는 중…'
      : !exists
        ? '서버에서 파일을 찾을 수 없습니다.'
        : conflict
          ? '다른 변경이 먼저 저장되었습니다. 서버 내용과 초안을 비교하세요.'
          : draft
            ? '저장 전 변경'
            : '저장된 내용';
    get('resolveFile').hidden = !conflict;
    get('comparison').hidden = !conflict || comparison !== bot.id + '/' + path;
    if (!get('comparison').hidden) get('serverText').value = data.files[path] ?? '';
  }
  get('text').oninput = () => {
    const bot = current(),
      state = view(bot),
      path = state.active.slice(2),
      key = 'file:' + path;
    const data = assets.get(bot.id),
      text = get('text').value;
    if (text === data.files[path]) delete state.files[key];
    else state.files[key] = { text, digest: state.files[key]?.digest ?? data.digest };
    persist();
    tabs();
    editor(bot);
  };
  get('resolveFile').onclick = () => {
    const bot = current();
    comparison = bot.id + '/' + view(bot).active.slice(2);
    editor(bot);
  };
  function resolve(keep) {
    const bot = current(),
      key = 'file:' + view(bot).active.slice(2),
      state = view(bot);
    if (keep) state.files[key].digest = assets.get(bot.id).digest;
    else delete state.files[key];
    comparison = null;
    persist();
    tabs();
    editor(bot);
  }
  get('rebaseDraft').onclick = () => resolve(true);
  get('useServer').onclick = () => resolve(false);
  get('save').onclick = async () => {
    const bot = current(),
      state = view(bot),
      filename = state.active.slice(2),
      key = 'file:' + filename;
    const draft = state.files[key];
    if (!draft || writes.has(bot.id)) return;
    writes.add(bot.id);
    editor(bot);
    try {
      const data = await api(filesFor(bot), { filename, content: draft.text, digest: draft.digest });
      delete state.files[key];
      accept(bot, data, draft.digest);
    } catch (err) {
      note(err.message);
      if (err.code === 'REQUEST_CONFLICT') {
        try {
          await load(bot, true);
        } catch (loadError) {
          note(loadError.message);
        }
      }
    } finally {
      writes.delete(bot.id);
      repaint();
      tabs();
    }
  };
  return {
    tree,
    editor,
    ensure,
    select(bot, path) {
      reveal(bot.id, path ? parentOf(path) : parents.get(bot.id) || '');
      ensure(bot);
    },
    invalidate(id) {
      assets.delete(id);
      reads.delete(id);
      const bot = bots().find((value) => value.id === id);
      if (bot && openRoots.has(id)) ensure(bot);
    },
  };
}
