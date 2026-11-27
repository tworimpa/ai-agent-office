import { requestJson, validId, visiblePoller } from './studio-shared.js';
import { controls, mountCharacterPicker, pagedHistory } from './bot-client-ui.js';
import { mountFiles } from './bot-client-files.js';
import { mountOrders } from './bot-client-orders.js';
import { mountBotCatalog, mountBotPackageChange, mountBotPackages } from './bot-client-packages.js';

export async function mountBotClient(win, makeScene) {
  const ui = controls(win),
    { get, element, button, picture, folderChoices, folderPayload } = ui;
  const doc = win.document,
    params = new URLSearchParams(win.location.search);
  let workspace = params.get('workspace'),
    journal = '',
    room,
    state,
    members = [],
    selected = null,
    otherRun = null;
  let refreshing = null,
    chatSequence = 0,
    working = false,
    ready = false,
    disposed = false,
    rosterStamp = '',
    chatStamp = '';
  const sending = new Set(),
    chats = new Map(),
    loadingPages = new Set();
  const verifiedRuns = new Map();
  let navigation = 0;
  const botById = (id) => members.find((bot) => bot.id === id);
  const current = () => botById(selected);
  const uuid = () => win.crypto.randomUUID().replaceAll('-', '');
  const base = () => '/api/rooms/' + workspace;
  const api = (path, body, options) => requestJson(win, base() + path + '?office_journal=' + journal, body, options);
  const storageKey = () => 'office-bot-room/v1/' + journal + '/' + workspace;
  function note(message) {
    get('notice').textContent = message;
    get('notice').hidden = !message;
  }
  function persist() {
    try {
      win.sessionStorage.setItem(storageKey(), JSON.stringify(state));
    } catch {
      note('브라우저에 초안을 저장할 수 없어 현재 창에서만 유지됩니다.');
    }
  }
  function view(bot) {
    return (state.bots['bot:' + bot.id] ??= { tabs: ['chat'], active: 'chat', files: {}, message: '' });
  }
  function runLink(id, label) {
    const link = element('a', label);
    link.href = id
      ? '/?workspace=' + workspace + '&run=' + encodeURIComponent(id)
      : '/workspace?workspace=' + workspace;
    return link;
  }
  function goBot(target) {
    if (!ready) return;
    const id = typeof target === 'string' ? target : target?.id;
    const bot = members.find((b) => b.id === id);
    ++navigation;
    if (bot && view(bot).active.startsWith('task:')) return openRun(view(bot).active.slice(5), bot);
    showBot(bot);
  }
  function showBot(bot) {
    selected = bot?.id || null;
    otherRun = null;
    if (bot) files.select(bot, view(bot).active.startsWith('f:') ? view(bot).active.slice(2) : '');
    doc.body.classList.remove('showFiles');
    syncUrl();
    render();
    if (bot && view(bot).active === 'chat') loadChat(bot);
  }
  const scene = makeScene(get('figures'), {
    choose: goBot,
    openKing: () => {
      if (ready) orders.open();
    },
  });
  const files = mountFiles(ui, { api, bots: () => members, current, view, persist, activate, tabs, note });
  const orders = mountOrders(ui, {
    api,
    bots: () => members,
    room: () => room,
    state: () => state,
    uuid,
    persist,
    scene,
    runLink,
    changed: () => refresh(true),
  });
  const picker = mountCharacterPicker(ui, { api, uuid, changed: () => refresh(true) });
  const packages = mountBotPackages(ui, {
    api,
    installed: async (id) => {
      await refresh(true);
      const bot = members.find((member) => member.id === id);
      if (bot) {
        goBot(bot);
        note(bot.name + ' 봇을 설치했습니다.');
      }
    },
  });
  const packageChange = mountBotPackageChange(ui, {
    api,
    botById,
    refresh: () => refresh(true),
    changed: (receipt, action, fromCatalog) => {
      if (fromCatalog) goBot(receipt.id);
      const bot = fromCatalog ? botById(receipt.id) : current();
      if (
        bot?.id === receipt.id &&
        bot.package?.commit === receipt.source.commit &&
        bot.package.digest === receipt.source.digest
      )
        note(bot.name + (action === 'update' ? ' 봇을 업데이트했습니다.' : ' 봇을 직전 버전으로 복원했습니다.'));
    },
  });
  const catalog = mountBotCatalog(ui, { api, botById, workspaceName: () => room.workspace.name, packages, packageChange });
  const poller = visiblePoller(
    win,
    () => refresh(),
    () => {
      chatSequence++;
    },
  );
  function syncUrl() {
    const url = new URL(win.location.href);
    url.pathname = '/bots';
    url.searchParams.set('workspace', workspace);
    if (selected) url.searchParams.set('bot', selected);
    else url.searchParams.delete('bot');
    const run =
      otherRun ??
      (current() && view(current()).active.startsWith('task:') ? view(current()).active.slice(5) : null);
    if (run) url.searchParams.set('run', run);
    else url.searchParams.delete('run');
    win.history.replaceState(null, '', url);
  }
  function activate(bot, tab) {
    if (tab.startsWith('task:')) return openRun(tab.slice(5), bot);
    const v = view(bot);
    if (!v.tabs.includes(tab)) v.tabs.push(tab);
    v.active = tab;
    persist();
    goBot(bot);
  }
  async function openRun(id, restoredBot) {
    const sequence = ++navigation,
      tab = 'task:' + id;
    const discard = () => {
      if (!restoredBot) return;
      const v = view(restoredBot);
      v.tabs = v.tabs.filter((key) => key !== tab);
      if (v.active === tab) v.active = 'chat';
      persist();
    };
    try {
      if (!validId(id)) throw Error('유효한 실행을 선택하세요.');
      let run = room.runs.find((row) => row.id === id) || verifiedRuns.get(id);
      if (!run) {
        const result = await requestJson(win, '/api/runs/' + id + '?office_journal=' + journal);
        if (sequence !== navigation || disposed) return;
        if (result.run?.id !== id || result.run.workspace_id !== workspace) {
          discard();
          throw Error('이 작업실에 속한 실행을 선택하세요.');
        }
        const { assignment, task, state, pending_count } = result.run;
        run = { id, assignment, task, state, pending_count };
      }
      verifiedRuns.set(id, run);
      const bot = members.find((b) => b.id === run.assignment?.bots?.developer?.id);
      if (restoredBot && restoredBot.id !== bot?.id) discard();
      if (bot) {
        const v = view(bot);
        if (!v.tabs.includes(tab)) v.tabs.push(tab);
        v.active = tab;
        persist();
        showBot(bot);
      } else {
        otherRun = id;
        selected = null;
        syncUrl();
        render();
      }
    } catch (err) {
      if (sequence !== navigation || disposed) return;
      if (err.status === 404) discard();
      showBot(null);
      note(err.message);
    }
  }
  function tabs() {
    const bot = current(),
      container = get('tabs');
    container.replaceChildren(button('⌂ 회의실', () => goBot(null), !bot && !otherRun ? 'active' : ''));
    if (!bot) {
      if (otherRun) container.append(button('작업 기록', () => openRun(otherRun), 'active'));
      return;
    }
    const v = view(bot);
    for (const key of v.tabs) {
      const path = key.slice(2);
      const label =
        key === 'chat'
          ? bot.name + ' · 대화'
          : key.startsWith('task:')
            ? '작업 기록 · ' + key.slice(5, 11)
            : path + (Object.hasOwn(v.files, 'file:' + path) ? ' •' : '');
      const group = element('div', null, 'group');
      group.append(button(label, () => activate(bot, key), v.active === key ? 'active' : ''));
      if (key !== 'chat') {
        const close = button(
          '×',
          () => {
            const index = v.tabs.indexOf(key);
            v.tabs.splice(index, 1);
            if (v.active === key) v.active = v.tabs[Math.max(0, index - 1)] || 'chat';
            persist();
            goBot(bot);
          },
          'closeTab',
        );
        close.setAttribute('aria-label', label + ' 닫기');
        group.append(close);
      }
      container.append(group);
    }
  }
  function renderNav() {
    get('home').classList.toggle('active', !selected && !otherRun);
    get('bots').replaceChildren(
      ...members.map((bot) => {
        const node = button('', () => goBot(bot), 'botRow' + (bot.id === selected ? ' active' : ''));
        const label = element('span');
        label.append(element('strong', bot.name), element('small', bot.role));
        node.append(picture(bot, 'avatar'), label);
        return node;
      }),
    );
    if (!members.length) get('bots').append(button('첫 봇 만들기', () => picker(null), 'empty-create'));
    get('history').replaceChildren(
      ...room.runs.map((run) => {
        const receipt = run.assignment?.bots?.developer;
        const owner = members.find((b) => b.id === receipt?.id);
        const node = button('', () => openRun(run.id, owner), 'past');
        node.title = run.task;
        node.append(
          element('span', owner?.name || receipt?.name || '작업실'),
          doc.createTextNode(run.task || run.id),
        );
        if (run.pending_count > 0) node.append(element('strong', ' · 응답 ' + run.pending_count));
        return node;
      }),
    );
    if (!room.runs.length) get('history').append(element('small', '아직 맡긴 일이 없습니다.', 'muted'));
    if (room.next_cursor) get('history').append(runLink(null, '작업실에서 전체 기록 보기'));
  }
  function renderTask(id) {
    const run = room.runs.find((row) => row.id === id) || verifiedRuns.get(id),
      area = get('taskBody');
    get('taskOwner').textContent = current()?.name || room.workspace.name;
    area.replaceChildren();
    if (run) {
      area.append(
        element('h2', '맡긴 일'),
        element('p', run.task),
        element('h2', '실행 상태'),
        element('p', run.state),
      );
      if (run.pending_count > 0) area.append(element('p', '응답 대기 ' + run.pending_count + '건'));
      area.append(runLink(id, '질문·승인·테스트·최종 검토 열기 ↗'));
    }
  }
  function renderThread(bot) {
    const thread = get('thread'),
      history = chats.get(bot.id);
    const atEnd = thread.scrollHeight - thread.scrollTop - thread.clientHeight < 48;
    thread.replaceChildren();
    if (!history?.groups.some((group) => group.rows.length)) {
      thread.append(
        element('small', history ? '이 봇에게 맡긴 일이 없습니다.' : '대화 불러오는 중…', 'muted'),
      );
      return;
    }
    const groups = [...history.groups].reverse();
    const lastByRun = new Map(
      groups.flatMap((group) => [...group.rows].reverse().map((m) => [m.run_id, m.id])),
    );
    for (const group of groups) {
      if (group.next) {
        const cursor = group.next,
          key = bot.id + '/' + cursor;
        const more = button(
          '이전 대화 더 보기',
          async () => {
            if (loadingPages.has(key)) return;
            loadingPages.add(key);
            more.disabled = true;
            try {
              const page = await api(
                '/bots/' + encodeURIComponent(bot.id) + '/chat/before/' + encodeURIComponent(cursor),
              );
              history.older(cursor, [...page.messages].reverse(), page.next_cursor);
            } catch (err) {
              if (current()?.id === bot.id) note(err.message);
            } finally {
              loadingPages.delete(key);
              if (current()?.id === bot.id && view(bot).active === 'chat') {
                const offset = thread.scrollHeight - thread.scrollTop;
                renderThread(bot);
                thread.scrollTop = thread.scrollHeight - offset;
              }
            }
          },
          'history-more',
        );
        more.disabled = loadingPages.has(key);
        thread.append(more);
      }
      for (const m of [...group.rows].reverse()) {
        const bubble = element('div', null, 'bubble' + (m.role === 'user' ? ' me' : ''));
        bubble.append(element('small', m.role === 'user' ? '나' : bot.name), element('p', m.text));
        if (validId(m.run_id) && lastByRun.get(m.run_id) === m.id)
          bubble.append(runLink(m.run_id, (m.state || '실행') + ' · 질문·승인·결과 ↗'));
        thread.append(bubble);
      }
    }
    if (atEnd) thread.scrollTop = thread.scrollHeight;
  }
  function composeControls(bot) {
    const v = view(bot),
      busy = sending.has(bot.id);
    get('msg').readOnly = busy;
    get('edits').disabled = get('folder').disabled = busy;
    get('send').disabled = busy || !get('msg').value.trim();
    get('send').textContent = busy ? '접수 중…' : v.pending ? '같은 명령 재시도' : '보내기 ↗';
  }
  async function loadChat(bot) {
    const epoch = ++chatSequence;
    try {
      const { messages, next_cursor } = await api('/bots/' + encodeURIComponent(bot.id) + '/chat');
      if (disposed || current()?.id !== bot.id || view(bot).active !== 'chat' || epoch !== chatSequence)
        return;
      const stamp = bot.id + JSON.stringify([messages, next_cursor]);
      if (chatStamp !== stamp) {
        let history = chats.get(bot.id);
        if (!history) {
          history = pagedHistory();
          chats.set(bot.id, history);
        }
        history.latest([...messages].reverse(), next_cursor);
        chatStamp = stamp;
        renderThread(bot);
      }
    } catch (err) {
      if (epoch === chatSequence && current()?.id === bot.id) note(err.message);
    }
  }
  function render() {
    const bot = current(),
      key = otherRun ? 'task:' + otherRun : bot ? view(bot).active : 'room';
    packageChange.render(bot);
    const panel = key.startsWith('f:') ? 'editor' : key.startsWith('task:') ? 'task' : key;
    for (const id of ['room', 'chat', 'editor', 'task']) get(id).hidden = id !== panel;
    renderNav();
    tabs();
    files.tree();
    if (panel === 'room') {
      get('members').textContent = members.length + '명의 봇이 있습니다.';
      scene.show(members);
      return;
    }
    scene.pause();
    if (panel === 'editor') {
      files.editor(bot);
      return;
    }
    if (panel === 'task') {
      renderTask(key.slice(5));
      return;
    }
    const v = view(bot);
    get('role').textContent = bot.role;
    get('chatTitle').textContent = bot.name + ' · 대화';
    const download = get('exportBot');
    download.href = base() + '/bots/' + encodeURIComponent(bot.id) + '/manifest?office_journal=' + journal;
    download.download = bot.id + '.bot.json';
    const packageDownload = get('packageExport');
    packageDownload.href = base() + '/bots/' + encodeURIComponent(bot.id) + '/package?office_journal=' + journal;
    packageDownload.download = bot.id + '.bot-package.zip';
    if (get('msg').value !== v.message) get('msg').value = v.message;
    get('edits').checked = !!v.edits;
    folderChoices('', room.workspace.folders, v.index ?? 0);
    renderThread(bot);
    composeControls(bot);
  }
  function setRoster(data) {
    const old = new Map(members.map((bot) => [bot.id, bot.digest]));
    const custom = data.bots.filter((bot) => !bot.builtin);
    const signature = JSON.stringify(
      custom.map((bot) => [bot.id, bot.name, bot.role, bot.digest, bot.package, bot.package_rollback]),
    );
    if (signature === rosterStamp) return false;
    rosterStamp = signature;
    members = custom.map((bot) => ({
      ...bot,
      face: bot.appearance?.preset ?? 0,
      photo: bot.appearance?.image
        ? base() +
          '/bots/' +
          encodeURIComponent(bot.id) +
          '/portrait?office_journal=' +
          journal +
          '#' +
          bot.digest
        : '',
    }));
    if (selected && !current()) selected = null;
    for (const bot of members)
      if (old.has(bot.id) && old.get(bot.id) !== bot.digest) files.invalidate(bot.id);
    return true;
  }
  async function update() {
    const [next, roster, records] = await Promise.all([api(''), api('/bots'), api('/bot-orders')]);
    if (disposed) return;
    if (next.journal_id !== journal || next.workspace?.id !== workspace)
      throw Error('작업실 저널이 바뀌었습니다. 페이지를 다시 여세요.');
    const runsChanged = JSON.stringify(next.runs) !== JSON.stringify(room.runs);
    const nameChanged = next.workspace.name !== room.workspace.name;
    room = next;
    const rosterChanged = setRoster(roster);
    if (rosterChanged || nameChanged) catalog.render();
    orders.update(records);
    if (rosterChanged) render();
    else if (runsChanged) {
      renderNav();
      const id =
        otherRun ||
        (current() && view(current()).active.startsWith('task:') ? view(current()).active.slice(5) : null);
      if (id) renderTask(id);
    }
    if (current() && view(current()).active === 'chat') await loadChat(current());
  }
  async function refresh(force = false) {
    if (!ready || disposed || (!force && doc.hidden)) return;
    if (refreshing) {
      await refreshing;
      if (!force || disposed) return;
    }
    refreshing = update().catch((err) => {
      if (!disposed) note(err.message);
    });
    const active = refreshing;
    await active;
    if (refreshing === active) {
      refreshing = null;
      if (!disposed) poller.schedule(7000);
    }
  }
  get('home').onclick = () => goBot(null);
  get('new').onclick = () => {
    if (ready) picker(null);
  };
  get('importBot').onclick = () => get('manifestFile').click();
  get('manifestFile').onchange = async () => {
    const file = get('manifestFile').files[0];
    if (!ready || !file) return;
    get('importBot').disabled = true;
    const controller = new win.AbortController();
    const timer = win.setTimeout(() => controller.abort(), 15000);
    try {
      if (!file.size || file.size > 512 * 1024) throw Error('512 KiB 이내의 봇 정의 JSON을 선택하세요.');
      // Preserve the original bytes. The host validates UTF-8 and rejects duplicate JSON keys.
      const response = await win.fetch(base() + '/bots/import?office_journal=' + journal, {
        method: 'POST',
        body: file,
        mode: 'same-origin',
        redirect: 'error',
        cache: 'no-store',
        signal: controller.signal,
        headers: { 'Content-Type': 'application/json', 'X-Agent-Office': 'studio' },
      });
      if (!response.ok) {
        const error = await response.json().catch(() => null);
        throw Error((response.status < 500 && error?.error?.message) || '봇 정의를 가져오지 못했습니다.');
      }
      const result = await response.json();
      await refresh(true);
      const imported = members.find((bot) => bot.id === result.bot?.id);
      if (imported) {
        goBot(imported);
        note(imported.name + ' 봇 정의를 가져왔습니다.');
      }
    } catch (err) {
      note(err.message);
    } finally {
      win.clearTimeout(timer);
      get('importBot').disabled = false;
      get('manifestFile').value = '';
    }
  };
  get('character').onclick = () => picker(current());
  get('toggleFiles').onclick = () => doc.body.classList.toggle('showFiles');
  get('hideFiles').onclick = () => doc.body.classList.remove('showFiles');
  for (const id of ['msg', 'edits', 'folder'])
    get(id).oninput = () => {
      const bot = current(),
        v = view(bot);
      v.message = get('msg').value;
      v.edits = get('edits').checked;
      v.index = Math.max(get('folder').selectedIndex, 0);
      if (
        v.pending &&
        (v.pending.message !== v.message.trim() ||
          v.pending.allow_edits !== v.edits ||
          JSON.stringify(v.pending.indices) !== JSON.stringify(folderPayload('').indices))
      )
        v.pending = null;
      persist();
      composeControls(bot);
    };
  get('compose').onsubmit = async (event) => {
    event.preventDefault();
    const bot = current();
    if (!bot || sending.has(bot.id) || !get('msg').value.trim()) return;
    const v = view(bot),
      body = v.pending || {
        id: uuid(),
        message: get('msg').value.trim(),
        allow_edits: get('edits').checked,
        delegation: false,
        meeting: false,
        ...folderPayload(''),
      };
    sending.add(bot.id);
    v.message = get('msg').value;
    v.edits = body.allow_edits;
    v.index = body.indices?.[0] ?? 0;
    v.pending = body;
    persist();
    composeControls(bot);
    note('');
    try {
      const { run } = await api('/bots/' + encodeURIComponent(bot.id) + '/commands', body);
      if (
        run?.id !== body.id ||
        run.workspace_id !== workspace ||
        run.assignment?.bots?.developer?.id !== bot.id
      )
        throw Error('명령의 실행 배정을 확인할 수 없습니다.');
      v.message = '';
      v.pending = null;
      v.edits = false;
      persist();
      if (current()?.id === bot.id && v.active === 'chat') render();
      await refresh(true);
    } catch (err) {
      if (current()?.id === bot.id) note(err.message);
    } finally {
      sending.delete(bot.id);
      if (current()?.id === bot.id) composeControls(bot);
    }
  };
  async function start() {
    if (working) return;
    working = true;
    note('');
    try {
      if (params.get('host') && params.get('host') !== 'local')
        throw Error('원격 호스트는 작업실에서 여세요.');
      const { workspaces } = await requestJson(win, '/api/workspaces');
      const selection = get('workspaceSelect');
      selection.replaceChildren(
        ...workspaces.map((w) => {
          const option = element('option', w.name);
          option.value = w.id;
          return option;
        }),
      );
      selection.hidden = workspaces.length < 2;
      selection.onchange = () => {
        win.location.assign('/bots?workspace=' + encodeURIComponent(selection.value));
      };
      if (!workspaces.length) {
        note('등록된 작업실이 없습니다. 작업실 관리에서 폴더를 등록하세요.');
        return;
      }
      workspace ??= workspaces[0].id;
      if (!validId(workspace) || !workspaces.some((w) => w.id === workspace))
        throw Error('등록된 작업실을 선택하세요.');
      selection.value = workspace;
      const expected = params.get('office_journal');
      room = await requestJson(
        win,
        base() + (expected ? '?office_journal=' + encodeURIComponent(expected) : ''),
      );
      journal = room.journal_id;
      if (!validId(journal) || room.workspace?.id !== workspace || (expected && journal !== expected))
        throw Error('작업실 저널을 확인할 수 없습니다.');
      let saved;
      try {
        saved = JSON.parse(win.sessionStorage.getItem(storageKey()) || 'null');
      } catch {
        /* fresh view */
      }
      state = saved?.bots && typeof saved.bots === 'object' ? saved : { bots: {} };
      const [roster, records] = await Promise.all([api('/bots'), api('/bot-orders')]);
      setRoster(roster);
      orders.update(records);
      get('workspaceName').textContent = room.workspace.name;
      get('studio').href = '/workspace?workspace=' + workspace;
      const requested = params.get('bot'),
        id = params.get('run');
      selected = members.find((b) => b.id === requested)?.id || null;
      ready = true;
      for (const id of ['new', 'importBot', 'packageImport', 'botCatalogOpen']) get(id).disabled = false;
      if (validId(id)) {
        const owner = room.runs.find((r) => r.id === id)?.assignment?.bots?.developer?.id;
        await openRun(id, current() || members.find((b) => b.id === owner));
      } else await goBot(current());
      poller.schedule(7000);
    } catch (err) {
      note(err.message);
      get('notice').append(button('다시 불러오기', start));
    } finally {
      working = false;
    }
  }
  await start();
  return {
    dispose() {
      disposed = true;
      chatSequence++;
      poller.dispose();
      catalog.dispose();
      packages.dispose();
      packageChange.dispose();
      if (scene.dispose) scene.dispose();
      else scene.pause();
    },
  };
}

if (typeof window !== 'undefined') {
  import('./bot-room-scene.js').then(({ mountRoomScene }) => mountBotClient(window, mountRoomScene));
}
