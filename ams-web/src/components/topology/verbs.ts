// 할당 동작 verb의 라벨·아이콘·강조 스타일. AssignmentsPanel의 동일 상수를
// 미러링한다(그쪽 상수는 모듈 비공개라 import 불가). 값이 어긋나지 않도록 함께
// 유지할 것. verb 세트·허용 규칙 자체는 client.ts의 allowedAssignmentActions가 SSOT.
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
