import {validId} from './studio-shared.js';

const names = {coordinator: '조정자', developer: '개발자', reviewer: '리뷰어'};
const title = bot => Object.hasOwn(names, bot.role) ? names[bot.role] : bot.role;

// The roster belongs to one local journal. Ignore replies from previously opened rooms or bots.
export function mountBotRoom({get, make, api, win, uuid, link, context}) {
  let roomKey = '', base = '', members = [], selected = '', version, filename = '';
  let assetRead = 0, rosterRead = 0, generation = 0, writing = false, sending = false, creating = false, createId = '';
  const drafts = new Map(), commands = new Map(), messages = new Map();
  const createDialog = get('bot-create-dialog');
  const key = () => [roomKey, selected, filename].join('/');
  const commandKey = () => 'office-bot-command/' + roomKey + '/' + selected;
  const endpoint = suffix => base + suffix + '?office_journal=' + context().journal;
  const note = (id, value) => { get(id).textContent = value; };
  const writable = () => !!roomKey && context().writable;
  const owned = (room, id) => room === roomKey && id === selected;
  function readCommand() {
    try { return commands.get(commandKey()) ?? JSON.parse(win.sessionStorage.getItem(commandKey()) || 'null'); }
    catch { return null; }
  }
  function keepCommand(value) {
    const k = commandKey();
    if (value) commands.set(k, value); else commands.delete(k);
    try {
      if (value) win.sessionStorage.setItem(k, JSON.stringify(value)); else win.sessionStorage.removeItem(k);
    } catch { /* retain the request in this tab */ }
  }
  function controls() {
    get('bot-new').disabled = get('bot-settings-new').disabled = !writable() || writing || sending;
    get('bot-create').disabled = creating || !writable();
    get('bot-send').disabled = !writable() || sending || !get('bot-message').value.trim();
    get('bot-message').readOnly = sending;
    get('bot-folder').disabled = get('bot-edits').disabled = sending;
    get('bot-roster-refresh').disabled = !writable() || sending || writing;
    get('bot-save').disabled = !writable() || !version || writing || !drafts.has(key());
    get('bot-text').readOnly = !writable() || writing;
    get('bot-reload').disabled = !writable() || writing;
    get('bot-file-new').disabled = !writable() || writing;
    get('bot-command').hidden = !selected;
    get('bot-editor').hidden = !version;
    get('bot-add-file').hidden = !version;
  }
  function showFile() {
    const files = version?.files ?? {};
    get('bot-file-list').replaceChildren(...Object.keys(files).map(name => {
      const button = make('button', name, 'bot-file');
      button.type = 'button';
      button.setAttribute('aria-pressed', String(name === filename));
      button.disabled = writing;
      button.addEventListener('click', () => { filename = name; note('bot-file-note', ''); showFile(); });
      return button;
    }));
    note('bot-filename', filename);
    get('bot-text').value = drafts.get(key())?.text ?? files[filename] ?? '';
    controls();
  }
  async function loadFiles(discard = false) {
    const room = roomKey, id = selected, serial = ++assetRead;
    if (!id) { version = null; filename = ''; showFile(); return; }
    const original = key();
    const originalDraft = drafts.get(original);
    try {
      const result = await api(endpoint('/' + id + '/assets'));
      if (!owned(room, id) || serial !== assetRead) return;
      if (discard && drafts.get(original) === originalDraft) drafts.delete(original);
      version = result;
      if (!Object.hasOwn(result.files, filename)) filename = 'SOUL.md';
      note('bot-file-note', '');
      showFile();
    } catch (error) { if (owned(room, id) && serial === assetRead) note('bot-file-note', error.message); }
  }
  function portraitPosition(id) {
    let hash = 0;
    for (const character of id) hash = (hash * 31 + character.codePointAt(0)) >>> 0;
    return (hash % 3) * 50 + '%';
  }
  function drawRoster() {
    note('bot-count', members.length + '명');
    get('bot-empty').hidden = members.length !== 0;
    get('bot-roster').replaceChildren(...members.map(bot => {
      const button = make('button', '', 'bot-member');
      button.type = 'button';
      button.setAttribute('aria-pressed', String(bot.id === selected));
      button.disabled = sending || writing;
      const portrait = make('span', '', 'portrait bot-avatar');
      portrait.style.setProperty('--portrait-position', portraitPosition(bot.id));
      portrait.setAttribute('aria-hidden','true');
      const label = make('span', '', 'bot-member-label');
      label.append(make('strong', bot.name), make('small', title(bot)));
      button.append(portrait, label);
      button.addEventListener('click', () => choose(bot.id));
      return button;
    }));
  }
  function choose(id) {
    selected = id;
    version = null; filename = '';
    get('bot-run-link').hidden = true;
    const bot = members.find(item => item.id === id);
    note('bot-files-owner', bot ? bot.name : '봇을 선택하면 파일이 표시됩니다.');
    note('bot-command-label', bot ? bot.name + '에게 명령' : '');
    note('bot-file-note', '');
    const pending = readCommand();
    const composer = messages.get(commandKey()) ?? pending;
    get('bot-message').value = composer?.message || '';
    get('bot-edits').checked = composer?.allow_edits === true;
    const folders = context().folders;
    get('bot-folder-label').hidden = folders.length < 2;
    get('bot-folder').replaceChildren(...folders.map((folder, i) => {
      const option = make('option', typeof folder === 'string' ? folder : folder.path);
      option.value = String(i);
      return option;
    }));
    if (composer?.indices?.length) get('bot-folder').value = String(composer.indices[0]);
    get('bot-file-new').value = '';
    note('bot-room-note', pending ? '접수 결과를 확인하려면 같은 명령을 다시 보내세요.' : '');
    drawRoster(); showFile(); loadFiles();
  }
  async function loadRoster() {
    if (!roomKey) return;
    const room = roomKey, serial = ++rosterRead;
    try {
      const result = await api(endpoint(''));
      if (room !== roomKey || serial !== rosterRead) return;
      members = result.bots.filter(bot => !bot.builtin);
      if (!selected || !members.some(bot => bot.id === selected)) choose(members[0]?.id || '');
      else drawRoster();
    } catch (error) { if (room === roomKey && serial === rosterRead) note('bot-room-note', error.message); }
  }
  function sync() {
    const ctx = context();
    const next = ctx.visible && validId(ctx.journal) && validId(ctx.workspaceId) ? ctx.journal + '/' + ctx.workspaceId : '';
    get('bot-room').hidden = !next;
    if (next !== roomKey) {
      ++generation;
      if (createDialog.open) createDialog.close();
      roomKey = next; base = '/rooms/' + ctx.workspaceId + '/bots';
      members = []; selected = ''; version = null; filename = ''; writing = sending = creating = false;
      ++assetRead; ++rosterRead;
      drawRoster(); showFile();
      get('bot-run-link').hidden = true;
      note('bot-files-owner', '봇을 선택하면 파일이 표시됩니다.');
      note('bot-room-note', ''); note('bot-file-note', '');
      if (next) loadRoster();
    }
    controls();
  }
  function openCreate() {
    if (!writable()) return;
    get('bot-definition').reset();
    createId = 'bot-' + uuid();
    get('bot-preview-avatar').style.setProperty('--portrait-position', portraitPosition(createId));
    note('bot-create-note',''); note('bot-preview-name','새 멤버'); note('bot-preview-role','역할 미입력');
    if (get('room-settings').open) get('room-settings').close();
    createDialog.showModal(); get('bot-name').focus();
  }
  get('bot-new').addEventListener('click', openCreate);
  get('bot-settings-new').addEventListener('click', openCreate);
  get('bot-close').addEventListener('click', () => { if (!creating) createDialog.close(); });
  createDialog.addEventListener('cancel', event => { if (creating) event.preventDefault(); });
  get('bot-name').addEventListener('input', () => note('bot-preview-name', get('bot-name').value || '새 멤버'));
  get('bot-slug').addEventListener('input', () => get('bot-preview-avatar').style.setProperty('--portrait-position',
    portraitPosition(get('bot-slug').value.trim() || createId)));
  get('bot-title').addEventListener('input', () => note('bot-preview-role', get('bot-title').value || '역할 미입력'));
  get('bot-definition').addEventListener('submit', async event => {
    event.preventDefault();
    if (!writable() || creating) return;
    const current = generation;
    const body = {id: get('bot-slug').value.trim() || createId, name: get('bot-name').value.trim(),
      role: get('bot-title').value.trim(), instructions: get('bot-instructions').value};
    creating = true; controls();
    try {
      const result = await api(endpoint(''), body);
      if (current !== generation) return;
      ++rosterRead;
      members = result.bots.filter(bot => !bot.builtin);
      createDialog.close(); choose(body.id); note('bot-room-note', '회의실에 추가했습니다.');
      get('bot-message').focus();
    } catch (error) { if (current === generation) note('bot-create-note', error.message); }
    finally { if (current === generation) { creating = false; controls(); } }
  });
  get('bot-roster-refresh').addEventListener('click', loadRoster);
  get('bot-text').addEventListener('input', () => {
    if (!version) return;
    const text = get('bot-text').value;
    if (text === version.files[filename]) drafts.delete(key());
    else drafts.set(key(), {text, digest: drafts.get(key())?.digest ?? version.digest});
    note('bot-file-note', drafts.has(key()) ? '저장 전 변경' : ''); controls();
  });
  async function save(name, content, digest) {
    const current = generation, room = roomKey, id = selected, savedKey = [room, id, name].join('/');
    if (!writable() || writing) return;
    writing = true; ++assetRead; drawRoster(); showFile();
    try {
      const result = await api(endpoint('/' + id + '/assets'), {filename:name, content, digest});
      if (current !== generation || !owned(room,id)) return;
      // Drafts of other files can advance over our own write if their base was current.
      for (const [k,draft] of drafts) {
        if (k.startsWith(room+'/'+id+'/') && draft.digest === digest) draft.digest = result.digest;
      }
      drafts.delete(savedKey); version = result; filename = name;
      get('bot-file-new').value = ''; note('bot-file-note', name + ' 저장됨');
    } catch (error) {
      if (current === generation && owned(room,id)) note('bot-file-note', error.code === 'REQUEST_CONFLICT' ?
        '다른 변경이 먼저 저장됐습니다. 초안을 복사한 뒤 다시 불러오세요.' : error.message);
    } finally { if (current === generation && owned(room,id)) { writing = false; drawRoster(); showFile(); } }
  }
  get('bot-editor').addEventListener('submit', event => {
    event.preventDefault();
    const draft = drafts.get(key());
    if (draft) save(filename, draft.text, draft.digest);
  });
  get('bot-reload').addEventListener('click', () => loadFiles(true));
  get('bot-add-file').addEventListener('submit', event => {
    event.preventDefault();
    if (!version) return;
    const name = get('bot-file-new').value.trim();
    if (Object.hasOwn(version.files,name)) { note('bot-file-note','이미 있는 파일입니다. 목록에서 선택하세요.'); return; }
    save(name, '', version.digest);
  });
  function messageDraft() {
    return {message: get('bot-message').value, allow_edits: get('bot-edits').checked,
      ...(context().folders.length > 1 ? {indices:[Number(get('bot-folder').value)]} : {})};
  }
  for (const id of ['bot-message', 'bot-edits', 'bot-folder']) get(id).addEventListener('input', () => {
    messages.set(commandKey(), messageDraft()); controls();
  });
  get('bot-command').addEventListener('submit', async event => {
    event.preventDefault();
    if (!writable() || !selected || sending) return;
    const current = generation, room = roomKey, id = selected;
    const {message: raw, allow_edits: edits, indices} = messageDraft(), message = raw.trim();
    if (!message) return;
    const previous = readCommand();
    const same = previous && previous.message === message && previous.allow_edits === edits &&
      JSON.stringify(indices) === JSON.stringify(previous.indices);
    const body = same ? previous : {id:uuid(), message, allow_edits:edits, delegation:false, meeting:false, ...(indices ? {indices} : {})};
    keepCommand(body); sending = true; controls(); drawRoster();
    note('bot-room-note','명령을 접수하고 있습니다.');
    try {
      const {run} = await api(endpoint('/' + id + '/commands'), body);
      if (current !== generation || !owned(room,id)) return;
      if (run?.id !== body.id || run.workspace_id !== context().workspaceId || run.assignment?.bots?.developer?.id !== id) throw Error('실행 배정을 확인할 수 없습니다.');
      keepCommand(null); messages.delete(commandKey()); get('bot-message').value = '';
      note('bot-room-note', '명령을 접수했습니다.');
      const result = get('bot-run-link'); result.href = link(run.id); result.hidden = false;
    } catch (error) { if (current === generation && owned(room,id)) note('bot-room-note',error.message); }
    finally { if (current === generation && owned(room,id)) { sending = false; controls(); drawRoster(); } }
  });
  return {sync};
}
