const reasons = {
  active: '작업 진행 중', recovery: '복구 필요', review: '사용자 검토 대기', checkpoint: '체크포인트 참조',
  candidate: '후보 검토 대기', pending: '응답 또는 실행 대기', child: '실행기 또는 하위 작업 사용 중',
  recent: '7일 보존 기간', locked: '실행 잠금 사용 중', unknown: '상태 확인 불가', dirty: '로컬 파일 변경 또는 무시된 파일',
  unmerged: '원본 체크아웃에 반영되지 않은 커밋', identity: '등록 경로 또는 브랜치 불일치', git_locked: 'Git 작업 트리 잠금',
  missing: '작업 트리 없음', linked: '링크 또는 마운트 포함', changed: '조회 후 커밋 변경',
  partial: 'Git 정리가 끝나지 않았습니다. 상태를 다시 조회하세요.',
};

export function mountStorage({doc, get, api, context}) {
  const panel = get('worktree-storage'), entries = get('storage-entries'), button = get('storage-clean');
  const preview = get('storage-preview'), note = get('storage-note');
  let snapshot, generation = 0;
  const active = token => token.generation === generation && token.epoch === context().epoch &&
    token.journal === context().journal && context().writable && get('room-settings').open;
  function clear() {
    snapshot = null;
    button.disabled = true;
    entries.hidden = true;
    entries.replaceChildren(doc.createElement('legend'));
    entries.firstChild.textContent = '정리할 실행 선택';
  }
  const selected = () => entries.querySelector('input:checked');
  entries.addEventListener('change', () => { button.disabled = !selected(); });

  async function submit(clean) {
    const ctx = context(), choice = selected();
    if (!ctx.supported || !ctx.writable || preview.disabled || !get('room-settings').open) return;
    if (clean && (!choice || snapshot?.workspace_id !== ctx.workspaceId || snapshot?.journal_id !== ctx.journal)) return;
    const token = {generation: ++generation, epoch: ctx.epoch, journal: ctx.journal};
    const path = '/rooms/' + ctx.workspaceId + '/storage';
    const body = clean ? {run_id: choice.value, head: choice.dataset.head, journal_id: snapshot.journal_id} : undefined;
    clear();
    preview.disabled = true;
    note.textContent = clean ? '선택한 실행의 보존 조건을 다시 확인하고 있습니다.' : '작업 트리 사용량을 확인하고 있습니다.';
    let outcome = '';
    try {
      if (clean) {
        const result = await api(path, body, 120000);
        if (!active(token)) return;
        if (result.journal_id !== ctx.journal || result.workspace_id !== ctx.workspaceId || result.run_id !== body.run_id) throw Error();
        outcome = result.outcome === 'removed' ? '선택한 작업 트리와 브랜치를 정리했습니다. ' :
          '정리 보류 · ' + (reasons[result.outcome] ?? '상태 확인 필요') + '. ';
      }
      const view = await api(path, undefined, 120000);
      if (!active(token)) return;
      if (view.journal_id !== ctx.journal || view.workspace_id !== ctx.workspaceId || !Array.isArray(view.entries)) throw Error();
      let total = 0, unknown = 0;
      const size = bytes => bytes.toLocaleString() + ' B';
      for (const item of view.entries) {
        const row = doc.createElement('div'), label = doc.createElement('label');
        const input = doc.createElement('input');
        input.type = 'radio';
        input.name = 'storage-run';
        input.value = item.run_id;
        input.id = 'storage-' + item.run_id;
        input.dataset.head = item.head ?? '';
        input.disabled = Boolean(item.reasons.length) || !item.head;
        label.htmlFor = input.id;
        const measured = Number.isSafeInteger(item.bytes) && item.bytes >= 0;
        if (measured) total += item.bytes;
        else unknown++;
        label.textContent = [item.run_id, measured ? size(item.bytes) : '용량 미확인',
          item.reasons.map(reason => reasons[reason] ?? reason).join(', ') || '정리 가능'].join(' · ');
        row.append(input, label);
        entries.append(row);
      }
      snapshot = view;
      entries.hidden = !view.entries.length;
      note.textContent = outcome + '작업 트리 ' + view.entries.length + '개 · 확인한 파일 크기 ' + size(total) +
        (unknown ? ' · 용량 미확인 ' + unknown + '개' : '');
    } catch {
      if (active(token)) note.textContent = outcome + '현재 상태를 확인할 수 없습니다. 사용량을 다시 조회하세요.';
    } finally {
      if (active(token)) preview.disabled = false;
    }
  }
  preview.addEventListener('click', () => submit(false));
  get('storage-form').addEventListener('submit', event => { event.preventDefault(); submit(true); });
  return {settings() {
    generation++;
    clear();
    preview.disabled = false;
    panel.open = false;
    panel.hidden = !context().supported;
    note.textContent = '';
  }};
}
