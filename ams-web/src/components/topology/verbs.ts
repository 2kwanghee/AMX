// 할당 동작 verb의 라벨·아이콘·강조 스타일. TopologyView와 AssignmentsPanel이
// 함께 import하는 단일 SSOT. verb 세트·허용 규칙 자체는 client.ts의
// allowedAssignmentActions가 SSOT.
import type { AssignmentActionVerb as Verb } from '@/lib/api-client/client';
import type { AssignmentState } from '@/lib/api-client/types';
import type { IconName } from '../common';

export const VERB_LABEL: Record<Verb, string> = {
  deliver: '전달',
  activate: '활성화',
  deactivate: '비활성화',
  recover: '복구',
  'switch-now': '즉시 전환',
  recall: '회수',
};

// 상태 인지 라벨. pending 상태의 recall은 한 번도 전달된 적 없는 연결을 그냥
// 지우는 것이라 "회수"라는 말이 오해를 준다 — 이 조합만 "배정 취소"로 바꾸고
// 나머지는 VERB_LABEL 그대로. 서버 API·verb 값 자체는 바뀌지 않는다.
export function verbLabel(verb: Verb, state: AssignmentState): string {
  if (verb === 'recall' && state === 'pending') return '배정 취소';
  return VERB_LABEL[verb];
}

export const VERB_ICON: Record<Verb, IconName> = {
  deliver: 'send',
  activate: 'power',
  deactivate: 'pause',
  recover: 'rotate',
  'switch-now': 'zap',
  recall: 'undo',
};

export const VERB_STYLE: Partial<Record<Verb, string>> = {
  'switch-now': 'accent',
  recall: 'warn',
};
