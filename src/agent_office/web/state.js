export const emptyEvents = () => ({runId: null, events: [], cursor: 0});

export function mergeEvents(current, page, runId) {
  if (!Array.isArray(page.events)) throw new Error('Invalid event page');
  const sameRun = current.runId === runId;
  const events = new Map((sameRun ? current.events : []).map(event => [event.seq, event]));
  let cursor = sameRun ? current.cursor : 0;
  for (const event of page.events) {
    if (!Number.isSafeInteger(event.seq) || event.seq < 1 || event.run_id !== runId) {
      throw new Error('Invalid event identity');
    }
    events.set(event.seq, event);
    cursor = Math.max(cursor, event.seq);
  }
  return {runId, cursor, events: [...events.values()].sort((a, b) => a.seq-b.seq).slice(-1000)};
}

const labels = {QUEUED: '대기', RUNNING: '개발 중', TESTING: '테스트 중', REVIEWING: '리뷰 중',
  PLAN_ONLY: '계획만 작성', READY_FOR_HUMAN_REVIEW: '검토 준비', WAITING_USER: '답변 필요',
  FAILED: '실패', CANCELED: '취소', RECOVERY_REQUIRED: '복구 확인 필요'};
export const stateLabel = state => labels[state] ?? String(state ?? '상태 미상');
export const stateGroup = state => ['RUNNING', 'TESTING', 'REVIEWING'].includes(state) ? 'active'
  : ['WAITING_USER', 'RECOVERY_REQUIRED', 'READY_FOR_HUMAN_REVIEW'].includes(state) ? 'attention'
  : state === 'FAILED' ? 'failed' : state === 'PLAN_ONLY' ? 'plan' : 'other';
