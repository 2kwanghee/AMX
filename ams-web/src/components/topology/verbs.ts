// 할당 동작 verb의 라벨·아이콘·강조 스타일. TopologyView와 AssignmentsPanel이
// 함께 import하는 단일 SSOT. verb 세트·허용 규칙 자체는 client.ts의
// allowedAssignmentActions가 SSOT.
import type { AssignmentActionVerb as Verb } from '@/lib/api-client/client';
import type { IconName } from '../common';

export const VERB_LABEL: Record<Verb, string> = {
  deliver: '전달',
  activate: '활성화',
  deactivate: '비활성화',
  recover: '복구',
  'switch-now': '즉시 전환',
  recall: '회수',
};

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
