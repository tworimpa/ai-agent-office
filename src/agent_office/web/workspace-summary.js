import {firstProseParagraph} from './office-content.js';

const states = {
  QUEUED: '대기 중', RUNNING: '작업 중', TESTING: '테스트 중', REVIEWING: '검토 중',
  STOP_REQUESTED: '중지 요청 중', CANCELED: '중지됨', PLAN_ONLY: '조회 완료',
  READY_FOR_HUMAN_REVIEW: '검토 결과 확인', WAITING_USER: '응답 필요',
  FAILED: '실패', RECOVERY_REQUIRED: '복구 필요',
};
const activeStates = new Set(['QUEUED', 'RUNNING', 'TESTING', 'REVIEWING', 'STOP_REQUESTED']);
const stateName = state => states[state] ?? '상태 미확인';
function taskTitle(task) {
  const prose = firstProseParagraph(task ?? '');
  const chars = Array.from(prose);
  return chars.length > 90 ? chars.slice(0, 89).join('') + '…' : prose || '작업 요청';
}
const outcomes = {
  PLAN_ONLY: '조회 결과를 정리했습니다.',
  DEVELOPER_FAILED: '담당자 실행에 실패했습니다.',
  TEST_FAILED: '테스트 단계에서 실패했습니다.',
  REVIEW_FAILED: '검토자 실행에 실패했습니다.',
  CHANGES_REQUESTED: '검토자가 수정을 요청했습니다.',
  REVIEW_UNDECIDED: '검토 판정을 확정할 수 없어 사용자 판단이 필요합니다.',
  MEETING_FAILED: '계획을 확정하지 못했습니다.',
  INTERRUPTED: '실행이 중단되어 복구 상태를 확인해야 합니다.',
  CANCELED: '실행이 중지되었습니다.',
};

function summaryConclusion(run) {
  let result;
  if (run.state === 'RECOVERY_REQUIRED') {
    result = run.diagnostic || (run.review_output || run.test_output || '').split(/\r?\n/)
      .find(line => /timed?\s*out|timeout|시간\s*(?:초과|제한)|중단/i.test(line));
  } else if (run.state === 'FAILED') {
    result = run.diagnostic || run.meeting?.error;
    if (!result && run.outcome === 'TEST_FAILED') {
      const failure = run.test_output?.split(/\r?\n/).find(line => /^FAILED |^ERROR |^E\s{3}\S/.test(line));
      if (failure) result = '실패 기록 · ' + failure.replace(/^(?:FAILED |ERROR |E\s{3})/, '');
    } else if (!result && ['REVIEW_FAILED', 'CHANGES_REQUESTED'].includes(run.outcome)) {
      result = run.review_output;
    } else if (!result && run.outcome === 'DEVELOPER_FAILED') {
      result = run.developer_output;
    }
  } else if (run.state === 'PLAN_ONLY' || run.state === 'READY_FOR_HUMAN_REVIEW') {
    result = run.developer_output;
  } else if (run.state === 'WAITING_USER') {
    result = run.review_output;
  }
  const prose = firstProseParagraph((result || '').replace(/^\s*VERDICT: (?:CHANGES_REQUESTED|APPROVED)\s*$/gm, ''));
  const chars = Array.from(prose);
  return chars.length > 180 ? chars.slice(0, 179).join('') + '…' : prose;
}

function resultFacts(run) {
  if (run.kind === 'parallel') {
    const children = run.subtasks ?? [];
    const states = new Map();
    for (const child of children) {
      const label = stateName(child.state);
      states.set(label, (states.get(label) ?? 0) + 1);
    }
    const groups = [...states].map(([label, count]) => label + ' ' + count + '건').join(', ');
    return ['변경 파일 미확인', '테스트는 하위 작업별 확인', '검토는 하위 작업별 확인',
      groups ? '하위 작업 ' + groups : '하위 작업 상태 미확인'];
  }
  const lookup = run.state === 'PLAN_ONLY' || run.allow_edits === false;
  const early = lookup || ['MEETING_FAILED', 'DEVELOPER_FAILED'].includes(run.outcome);
  const review = {APPROVED: '승인 · 최종 사용자 확인 필요', CHANGES_REQUESTED: '수정 요청',
    REVIEW_UNDECIDED: '판정 미확인 · 사용자 판단 필요', REVIEW_FAILED: '실패'}[run.outcome];
  const test = run.outcome === 'TEST_FAILED' ? '실패' : review || run.state === 'REVIEWING' ? '통과' :
    run.state === 'TESTING' ? '실행 중' : early && !run.test_output ? '미실행' : '미확인';
  let files = '미확인';
  if (typeof run.changes === 'string') {
    const lines = run.changes.split(/\r?\n/).filter(Boolean);
    if (lines.length && lines.every(line => /^(?:[ MADRCUT?!]{2}) .+/.test(line) && !line.includes('...[truncated]'))) {
      files = lines.length + '개';
    } else if (!lines.length && !activeStates.has(run.state) && run.kind !== 'parallel' && !run.diff) {
      files = '기록된 변경 없음';
    }
  }
  const facts = ['변경 파일 ' + files, '테스트 ' + (pytestCounts(run.test_output, test) || test),
    '검토 ' + (review ?? (run.state === 'REVIEWING' ? '진행 중' : (early || run.outcome === 'TEST_FAILED') && !run.review_output ? '미실행' : '미확인'))];
  const checks = run.meeting?.verification;
  if (checks?.length) {
    const groups = new Map();
    for (const [i, row] of checks.entries()) {
      const criterion = run.meeting.plan?.criteria?.[i];
      const label = row.status === 'not_verified' && criterion && !criterion.test_files?.length ?
        '수동 확인 필요' : ({passed: '통과', failed: '실패', not_verified: '미검증'}[row.status] ?? '미확인');
      groups.set(label, (groups.get(label) ?? 0) + 1);
    }
    facts.push('완료 기준 ' + [...groups].map(([label, count]) => label + ' ' + count + '건').join(', '));
  }
  return facts;
}

function pytestCounts(output, result) {
  if (!output || !['통과', '실패'].includes(result)) return '';
  const lines = output.trim().split(/\r?\n/);
  if (lines[0] === 'Studio test invocation:') {
    try {
      const {exit_code: code, argv} = JSON.parse(lines[1]);
      if (!Array.isArray(argv) || argv[1] !== '-m' || !['pytest', 'agent_office.desktop_pytest'].includes(argv[2]) || !Number.isInteger(code) ||
          (code === 0) !== (result === '통과') || lines[2] !== 'Captured test output:') return '';
    } catch { return ''; }
  }
  const last = lines.at(-1).replace(/^=+\s*|\s*=+$/g, '');
  const summary = last.match(/^(.+) in \d+(?:\.\d+)?s(?: \([\d:]+\))?$/);
  if (!summary) return '';
  const labels = {passed: '통과', failed: '실패', errors: '오류', error: '오류', skipped: '건너뜀',
    xfailed: '예상된 실패', xpassed: '예상 밖 통과', deselected: '선택 제외'};
  const counts = [], seen = new Set();
  for (const field of summary[1].split(', ')) {
    const part = field.match(/^(\d+) ([a-z]+)$/);
    if (!part || seen.has(part[2]) || !Number.isSafeInteger(Number(part[1]))) return '';
    seen.add(part[2]);
    if (['warning', 'warnings'].includes(part[2])) continue;
    if (!Object.hasOwn(labels, part[2])) return '';
    counts.push(part[1] + '개 ' + labels[part[2]]);
  }
  if (seen.has('failed') || seen.has('error') || seen.has('errors')) {
    if (result !== '실패') return '';
  } else if (result === '실패') return '';
  return counts.join(', ');
}


export {activeStates, stateName, taskTitle, summaryConclusion, resultFacts, outcomes};
