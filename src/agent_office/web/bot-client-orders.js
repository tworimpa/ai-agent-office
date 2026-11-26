import { pagedHistory } from './bot-client-ui.js';
import { validId } from './studio-shared.js';

const active = new Set(['QUEUED', 'RUNNING', 'TESTING', 'REVIEWING']);
const otherStates = new Set([
  'pending',
  'STOP_REQUESTED',
  'CANCELED',
  'PLAN_ONLY',
  'READY_FOR_HUMAN_REVIEW',
  'WAITING_USER',
  'FAILED',
  'RECOVERY_REQUIRED',
]);
function matchesText(value, original, masked) {
  if (value === original) return true;
  if (typeof value !== 'string' || !masked) return false;
  const truncated = value.endsWith('...[truncated]');
  const visible = truncated ? value.slice(0, -'...[truncated]'.length) : value;
  const parts = visible.split('[redacted]');
  if (!original.startsWith(parts[0])) return false;
  let offset = parts[0].length;
  for (const part of parts.slice(1)) {
    const index = original.indexOf(part, offset);
    if (index < 0) return false;
    offset = index + part.length;
  }
  return truncated || (parts.length > 1 && original.endsWith(parts.at(-1)));
}
function matchesReceipt(order, body) {
  if (
    !order ||
    order.id !== body.id ||
    order.allow_edits !== body.allow_edits ||
    JSON.stringify(order.indices) !== JSON.stringify(body.indices) ||
    !Array.isArray(order.assignments) ||
    order.assignments.length !== body.assignments.length ||
    !matchesText(order.message, body.message, order.replay_requires_original === true)
  )
    return false;
  const ids = new Set();
  return order.assignments.every((item, i) => {
    if (
      !item ||
      item.bot_id !== body.assignments[i].bot_id ||
      !validId(item.run_id) ||
      ids.has(item.run_id) ||
      (!active.has(item.state) && !otherStates.has(item.state)) ||
      !matchesText(item.duty, body.assignments[i].duty, order.replay_requires_original === true)
    )
      return false;
    ids.add(item.run_id);
    return true;
  });
}

const guides = [
  [/접근성|테스트|검증|품질|리뷰|review|QA/i, '검증 항목을 정리하고 확인 결과를 보고', '검증 준비'],
  [/리서|조사|분석|연구/, '관련 사례를 조사하고 핵심 근거를 정리', '자료 조사'],
  [/에디터|콘텐츠|편집|문안|작가/, '전달할 내용을 읽기 쉬운 초안으로 작성', '문안 작성'],
  [/개발|엔지니어|프로그래머|develop/i, '구현 범위와 변경할 부분을 검토', '구현 검토'],
  [/디자인|디자이너|설계/, '사용 흐름을 설계하고 시각안을 준비', '설계 준비'],
  [/기록|서기|정리/, '논의 사항과 결정 내용을 기록', '기록 정리'],
];
function suggest(bot, message) {
  for (const line of message.split(/[\n;]/)) {
    const pair = line.match(/^\s*@?([^:：]+?)\s*[:：]\s*(.+)$/);
    if (pair && [bot.name, bot.role].includes(pair[1])) return pair[2].trim();
  }
  const guide = guides.find(([pattern]) => pattern.test(bot.role));
  return guide ? guide[1] : bot.role + ' 관점에서 필요한 작업을 진행하고 결과를 보고';
}

export function mountOrders(ui, { api, bots, room, state, uuid, persist, scene, runLink, changed }) {
  const { win, get, element, button, folderChoices, folderPayload } = ui;
  const dialog = get('royal-dialog'),
    input = get('royal-command'),
    send = get('royal-send');
  let executing = false,
    timer;
  const history = pagedHistory(),
    loading = new Set();
  const form = () => (state().royal ??= { message: '', assignments: [] });
  const savedOrders = () => (state().savedOrders ??= {});
  function replaceDraft(draft) {
    state().royal = draft;
    persist();
    input.value = draft.message;
    get('royal-edits').checked = !!draft.edits;
    folderChoices('royal-', room().workspace.folders, draft.index ?? 0);
    get('royal-status').textContent = draft.recover
      ? '저널에서 가린 부분에 원래 어명과 담당 업무를 입력하세요.'
      : '';
    fields();
    readHistory();
  }
  const botFor = (id) => bots().find((bot) => bot.id === id);
  const voice = () =>
    win.speechSynthesis?.getVoices().find((value) => value.localService && /^ko(?:-|_|$)/i.test(value.lang));
  function readHistory() {
    const holder = get('royal-history');
    const opened = new Set([...holder.querySelectorAll('details[open]')].map((row) => row.dataset.id));
    const content = [];
    const visibleIds = new Set(history.groups.flatMap((group) => group.rows.map((row) => row.id)));
    for (const [id, draft] of Object.entries(savedOrders())) {
      if (visibleIds.has(id)) continue;
      const resume = button('접수 확인 · ' + draft.message, () => replaceDraft(draft), 'history-more');
      resume.disabled = executing || !!form().message.trim();
      content.push(resume);
    }
    for (const group of history.groups) {
      for (const order of group.rows) {
        const details = element('details', null, 'order-record');
        details.dataset.id = order.id;
        details.open = opened.has(order.id);
        details.append(element('summary', order.message));
        for (const a of order.assignments) {
          const row = element('p'),
            run = room().runs.find((r) => r.id === a.run_id);
          row.append(
            element('strong', (botFor(a.bot_id)?.name ?? a.bot_id) + ' · '),
            element('span', a.duty),
          );
          if (a.run_id && a.state !== 'pending') {
            row.append(runLink(a.run_id, run?.state || a.state || '실행 보기'));
          } else row.append(element('small', a.error || a.state || '접수 대기'));
          details.append(row);
        }
        if (order.assignments.some((a) => a.state === 'pending')) {
          const retry = button(
            !savedOrders()[order.id] && order.replay_requires_original
              ? '원문을 입력해 접수 재시도'
              : '이 어명 다시 열기',
            () => {
              const assignments = order.assignments.map(({ bot_id, duty }) => ({ bot_id, duty }));
              const binding = {
                id: order.id,
                allow_edits: order.allow_edits,
                ...(order.indices ? { indices: order.indices } : {}),
              };
              replaceDraft(
                savedOrders()[order.id] || {
                  message: order.message,
                  assignments,
                  edits: order.allow_edits,
                  index: order.indices?.[0] ?? 0,
                  ...(order.replay_requires_original
                    ? { recover: binding }
                    : { pending: { ...binding, message: order.message, assignments } }),
                },
              );
            },
          );
          retry.disabled = executing || !!form().message.trim();
          details.append(retry);
        }
        content.push(details);
      }
      if (group.next) {
        const more = button(
          '지난 어명 더 보기',
          async () => {
            const cursor = group.next;
            if (loading.has(cursor)) return;
            loading.add(cursor);
            more.disabled = true;
            try {
              const page = await api('/bot-orders/before/' + encodeURIComponent(cursor));
              history.older(cursor, page.orders, page.next_cursor);
            } catch (err) {
              get('royal-status').textContent = err.message;
            } finally {
              loading.delete(cursor);
              readHistory();
            }
          },
          'history-more',
        );
        more.disabled = loading.has(group.next);
        content.push(more);
      }
    }
    holder.replaceChildren(...content);
  }
  function fields() {
    const draft = form(),
      prepared = !!draft.assignments.length,
      locked = !!draft.pending || executing;
    const rows = draft.assignments.map((a) => {
      const bot = botFor(a.bot_id);
      const label = element('div', null, 'edict-duty');
      label.append(element('strong', bot?.name || a.bot_id), element('small', bot?.role || ''));
      const choose = element('label', null, 'recipient');
      const check = element('input');
      check.type = 'checkbox';
      check.checked = a.chosen !== false;
      check.disabled = locked || !!draft.recover;
      check.setAttribute('aria-label', (bot?.name || a.bot_id) + '에게 배정');
      choose.append(check, element('span', '배정'));
      label.append(choose);
      check.onchange = () => {
        a.chosen = check.checked;
        field.disabled = !a.chosen;
        persist();
        updateSend();
      };
      const field = element('textarea');
      field.rows = 2;
      field.maxLength = draft.recover ? 8192 : 1800;
      field.value = a.duty;
      field.setAttribute('aria-label', (bot?.name || a.bot_id) + '에게 맡길 일');
      field.required = true;
      field.readOnly = locked;
      field.disabled = a.chosen === false;
      field.oninput = () => {
        a.duty = field.value;
        persist();
        updateSend();
      };
      label.append(field);
      return label;
    });
    get('royal-assignments').replaceChildren(...rows);
    get('royal-assignments').hidden = !prepared;
    input.readOnly = locked;
    input.maxLength = draft.recover ? 8192 : 800;
    get('royal-edits').disabled = get('royal-folder').disabled = locked || !!draft.recover;
    function updateSend() {
      const chosen = draft.assignments.filter((row) => row.chosen !== false);
      send.disabled =
        executing ||
        !input.value.trim() ||
        !bots().length ||
        (prepared && (!chosen.length || !chosen.every((row) => row.duty.trim())));
    }
    updateSend();
    get('royal-new').hidden = !draft.message && !draft.pending && !draft.recover;
    get('royal-new').disabled = executing;
    get('royal-new').textContent = draft.pending || draft.recover ? '기록에 두고 새 어명' : '새 어명 작성';
    send.textContent =
      draft.pending || draft.recover
        ? '같은 어명 접수 확인·재시도'
        : prepared
          ? '어명 내리기'
          : '역할별 분담 보기';
  }
  function announce(order) {
    const local = get('royal-voice').checked && voice();
    if (local) {
      const speech = new win.SpeechSynthesisUtterance(order.message);
      speech.voice = local;
      speech.lang = local.lang;
      win.speechSynthesis.speak(speech);
    }
    const toast = get('court-announcement');
    toast.replaceChildren(element('strong', '왕의 어명'), element('span', order.message));
    toast.hidden = false;
    win.clearTimeout(timer);
    timer = win.setTimeout(() => {
      toast.hidden = true;
    }, 12000);
    scene.respond(
      order.assignments.map((a) => ({
        bot: a.bot_id,
        duty: a.duty,
        action: guides.find(([pattern]) => pattern.test(botFor(a.bot_id)?.role || ''))?.[2] || '역할 수행',
      })),
    );
  }
  get('royal-cancel').onclick = () => dialog.close();
  get('royal-new').onclick = () => {
    const draft = form(),
      id = draft.pending?.id || draft.recover?.id;
    if (executing) return;
    if (id) savedOrders()[id] = draft;
    else if (draft.message && !win.confirm('작성 중인 어명을 지우고 새로 작성할까요?')) return;
    replaceDraft({ message: '', assignments: [] });
    input.focus();
  };
  input.oninput = () => {
    const draft = form();
    draft.message = input.value;
    if (!draft.recover) draft.assignments = [];
    persist();
    fields();
    readHistory();
  };
  for (const name of ['royal-edits', 'royal-folder'])
    get(name).onchange = () => {
      const draft = form();
      draft.edits = get('royal-edits').checked;
      draft.index = get('royal-folder').selectedIndex;
      persist();
    };
  get('royal-form').onsubmit = async (event) => {
    event.preventDefault();
    const draft = form(),
      message = draft.recover ? draft.message : input.value.trim();
    if (executing || !message.trim() || !bots().length) return;
    if (!draft.assignments.length) {
      draft.assignments = bots().map((bot) => ({ bot_id: bot.id, duty: suggest(bot, message) }));
      persist();
      fields();
      return;
    }
    const chosen = draft.assignments.filter((a) => a.chosen !== false);
    if (!chosen.length || chosen.some((a) => !a.duty.trim())) return;
    const binding = draft.recover || {
      id: uuid(),
      allow_edits: get('royal-edits').checked,
      ...folderPayload('royal-'),
    };
    const body = draft.pending || {
      ...binding,
      message,
      assignments: chosen.map((a) => ({ bot_id: a.bot_id, duty: draft.recover ? a.duty : a.duty.trim() })),
    };
    draft.pending = body;
    persist();
    executing = true;
    fields();
    get('royal-status').textContent = '역할별 작업을 접수하고 있습니다.';
    try {
      const { order } = await api('/bot-orders', body);
      if (!matchesReceipt(order, body)) throw Error('어명 접수 결과를 확인할 수 없습니다.');
      const previous = history.groups.flatMap((group) => group.rows).find((row) => row.id === order.id);
      const alreadySent = new Set(
        (previous?.assignments ?? []).filter((a) => a.state !== 'pending').map((a) => a.run_id),
      );
      const newlySent = order.assignments.filter((a) => active.has(a.state) && !alreadySent.has(a.run_id));
      if (newlySent.length) announce({ ...order, assignments: newlySent });
      history.put(order);
      readHistory();
      const pending = order.assignments.filter((a) => !a.run_id || a.state === 'pending');
      if (pending.length) {
        delete draft.recover;
        persist();
        get('royal-status').textContent =
          pending
            .map((a) => (botFor(a.bot_id)?.name || a.bot_id) + ' · ' + (a.error || '접수 대기'))
            .join('\n') + '\n같은 어명으로 남은 배정을 다시 접수할 수 있습니다.';
      } else {
        delete savedOrders()[body.id];
        state().royal = { message: '', assignments: [] };
        persist();
        dialog.close();
      }
      await changed();
    } catch (err) {
      if (err.status === 400 || err.status === 404 || (draft.recover && err.status === 409)) {
        delete draft.pending;
        persist();
      }
      get('royal-status').textContent = err.message;
    } finally {
      executing = false;
      fields();
    }
  };
  return {
    open() {
      const draft = form();
      input.value = draft.message;
      get('royal-edits').checked = !!draft.edits;
      folderChoices('royal-', room().workspace.folders, draft.index ?? 0);
      get('royal-status').textContent = !bots().length
        ? '새 봇을 추가하면 역할별로 일을 맡길 수 있습니다.'
        : draft.recover
          ? '저널에서 가린 부분에 원래 어명과 담당 업무를 입력하세요.'
          : '';
      get('voiceLabel').hidden = !voice();
      get('royal-voice').checked = false;
      fields();
      readHistory();
      dialog.showModal();
    },
    update(page) {
      history.latest(page.orders, page.next_cursor);
      for (const order of page.orders) {
        if (savedOrders()[order.id] && order.assignments.every((a) => a.state !== 'pending')) {
          delete savedOrders()[order.id];
          persist();
        }
      }
      if (dialog.open) readHistory();
    },
  };
}
