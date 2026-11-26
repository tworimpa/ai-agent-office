import {validId} from './studio-shared.js';

const roles = {coordinator: '조정자', developer: '개발자', reviewer: '리뷰어'};

export function mountBots({get, make, api, context}) {
  const panel = get('role-bots'), role = get('bot-role'), picker = get('bot-id');
  let catalog, owner, busy = false;

  function available() {
    const current = context();
    return owner && current.local && current.writable && current.epoch === owner.epoch &&
      current.workspaceId === owner.workspaceId && current.journal === owner.journal &&
      get('room-settings').open && panel.open;
  }

  function controls() {
    const enabled = available() && !!catalog && !busy;
    role.disabled = picker.disabled = !enabled;
    get('bot-create').disabled = !enabled;
    get('bot-assign').disabled = !enabled || !picker.value || catalog?.assignments[role.value] === picker.value;
  }

  function render(preferred) {
    const candidates = catalog.bots.filter(bot => bot.role === role.value);
    const assigned = catalog.assignments[role.value];
    picker.replaceChildren(...candidates.map(bot => {
      const option = make('option', bot.name + ' (' + bot.id + ')');
      option.value = bot.id;
      return option;
    }));
    picker.value = preferred || assigned;
    get('bot-current').textContent = '현재 배정 · ' + (candidates.find(bot => bot.id === assigned)?.name || assigned);
    get('bot-legend').textContent = '새 ' + roles[role.value] + ' 봇';
    showInstructions();
  }

  function showInstructions() {
    const selected = catalog?.bots.find(bot => bot.id === picker.value);
    get('bot-guidance').textContent = selected?.instructions || (selected?.builtin ? '작업실 기본 지침을 사용합니다.' : '');
    controls();
  }

  async function change(action, body) {
    if (!available() || busy || (body && !catalog)) return;
    const snapshot = owner;
    const current = () => owner === snapshot && available();
    busy = true;
    controls();
    get('bot-note').textContent = '';
    try {
      const url = '/rooms/' + snapshot.workspaceId + '/bots' + action + '?office_journal=' + snapshot.journal;
      const next = await api(url, body);
      if (!current()) return;
      catalog = next;
      if (body && !action) {
        get('bot-definition').reset();
        get('bot-note').textContent = '봇을 만들었습니다. 이 역할에 사용하려면 배정하세요.';
      } else if (body) {
        get('bot-note').textContent = '새 작업에 사용할 봇을 배정했습니다.';
      }
      render(body?.id);
    } catch (error) {
      if (current()) get('bot-note').textContent = error.message;
    } finally {
      if (owner === snapshot) { busy = false; controls(); }
    }
  }

  panel.addEventListener('toggle', () => {
    if (panel.open) {
      if (available()) change('');
    }
  });
  role.addEventListener('change', () => { if (catalog) render(); });
  picker.addEventListener('change', showInstructions);
  get('bot-assignment').addEventListener('submit', event => {
    event.preventDefault();
    if (!get('bot-assign').disabled) change('/assignment', {role: role.value, id: picker.value});
  });
  get('bot-definition').addEventListener('submit', event => {
    event.preventDefault();
    change('', {role: role.value, id: get('bot-slug').value, name: get('bot-name').value,
      instructions: get('bot-instructions').value});
  });

  return {settings() {
    owner = context();
    busy = false;
    catalog = null;
    panel.open = false;
    panel.hidden = !owner.local || !validId(owner.journal);
    get('bot-definition').reset();
    get('bot-note').textContent = '';
    get('bot-guidance').textContent = get('bot-current').textContent = '';
    role.value = 'coordinator';
    picker.replaceChildren();
    controls();
  }};
}
