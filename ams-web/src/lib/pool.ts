// 계정 풀 화면의 순수 로직. 라벨 매핑·타입 가드·남은 시간 계산·보드 분류는
// 렌더와 분리해 여기서 단위 검증한다(패널은 이 함수들을 조립만 한다).
import type {
  ChainStep,
  IneligibleReason,
  PoolAccount,
  PoolEventKind,
  PoolState,
  RecommendationKind,
} from './api-client/types';
import type { PoolAccountVerb } from './api-client/client';

// -- 라벨 매핑 (없는 값은 원문 폴백) -----------------------------------------
const POOL_STATE_LABEL: Record<PoolState, string> = {
  ready: '배급처',
  leased: '대여중',
  recalling: '회수중',
  cooling: '충전소',
  pinned: '고정',
  held: '보류',
};
export function poolStateLabel(state: string): string {
  return POOL_STATE_LABEL[state as PoolState] ?? state;
}

const RECOMMENDATION_KIND_LABEL: Record<RecommendationKind, string> = {
  prefetch: '미리 전달',
  swap: '교체',
  lease: '배정',
  recall_idle: '초과 회수',
};
export function recommendationKindLabel(kind: string): string {
  return RECOMMENDATION_KIND_LABEL[kind as RecommendationKind] ?? kind;
}

const CHAIN_STEP_LABEL: Record<ChainStep, string> = {
  deliver: '전달',
  switch: '전환',
  recall: '회수',
  done: '완료',
  failed: '실패',
};
export function chainStepLabel(step: string): string {
  return CHAIN_STEP_LABEL[step as ChainStep] ?? step;
}

const POOL_EVENT_KIND_LABEL: Record<PoolEventKind, string> = {
  state_changed: '상태 변경',
  recommendation_created: '권고 생성',
  recommendation_dropped: '권고 취소',
  chain_started: '체인 시작',
  chain_step: '체인 진행',
  chain_done: '체인 완료',
  chain_failed: '체인 실패',
  policy_changed: '정책 변경',
  automation_paused: '자동화 정지',
  automation_resumed: '자동화 재개',
};
export function poolEventKindLabel(kind: string): string {
  return POOL_EVENT_KIND_LABEL[kind as PoolEventKind] ?? kind;
}

// 창 식별자 → 사람이 읽는 이름. 표준 두 창은 고정, 그 밖은 식별자 폴백.
const WINDOW_LABEL: Record<string, string> = {
  five_hour: '5시간',
  seven_day: '7일',
};
export function windowLabel(windowId: string): string {
  return WINDOW_LABEL[windowId] ?? windowId;
}

// 부적격 사유 → 짧은 한글. 카드 배지에 붙는다. 없는 값은 원문 폴백.
const INELIGIBLE_REASON_LABEL: Record<IneligibleReason, string> = {
  api_key: 'API 키',
  excluded: '배정 제외',
  unusable: '사용 불가',
  pinned: '고정',
  held: '보류',
  no_observation: '관측 없음',
};
export function ineligibleReasonLabel(reason: string): string {
  return INELIGIBLE_REASON_LABEL[reason as IneligibleReason] ?? reason;
}

// -- 타입 가드 ---------------------------------------------------------------
const POOL_STATES: readonly PoolState[] = [
  'ready',
  'leased',
  'recalling',
  'cooling',
  'pinned',
  'held',
];
export function isPoolState(v: unknown): v is PoolState {
  return typeof v === 'string' && (POOL_STATES as readonly string[]).includes(v);
}

const RECOMMENDATION_KINDS: readonly RecommendationKind[] = [
  'prefetch',
  'swap',
  'recall_idle',
  'lease',
];
export function isRecommendationKind(v: unknown): v is RecommendationKind {
  return typeof v === 'string' && (RECOMMENDATION_KINDS as readonly string[]).includes(v);
}

const CHAIN_STEPS: readonly ChainStep[] = ['deliver', 'switch', 'recall', 'done', 'failed'];
export function isChainStep(v: unknown): v is ChainStep {
  return typeof v === 'string' && (CHAIN_STEPS as readonly string[]).includes(v);
}

// 체인이 아직 진행 중인지(완료·실패가 아님). 진행 목록 필터에 쓴다.
export function isChainActive(step: string): boolean {
  return step !== 'done' && step !== 'failed';
}

// -- 상태별 허용 동작 --------------------------------------------------------
// pin: 아직 고정·보류가 아닌 계정을 자동화에서 뺀다. unpin: 고정 해제(→배급처).
// hold: 격리(→보류). release: 보류·충전소를 강제로 배급처로 되돌린다.
export function allowedPoolActions(state: PoolState): PoolAccountVerb[] {
  switch (state) {
    case 'ready':
      return ['pin', 'hold'];
    case 'leased':
    case 'recalling':
      return ['hold'];
    case 'cooling':
      return ['pin', 'hold', 'release'];
    case 'pinned':
      return ['unpin', 'hold'];
    case 'held':
      return ['release'];
    default:
      return [];
  }
}

const POOL_VERB_LABEL: Record<PoolAccountVerb, string> = {
  pin: '고정',
  unpin: '고정 해제',
  hold: '보류',
  release: '해제',
};
export function poolVerbLabel(verb: PoolAccountVerb): string {
  return POOL_VERB_LABEL[verb];
}

// -- 남은 시간 (충전소 복귀 타이머) ------------------------------------------
// coolingUntil까지 남은 밀리초. 이미 지났거나 값이 없으면 0.
export function coolingRemainingMs(coolingUntil: string | null | undefined, now: number): number {
  if (!coolingUntil) return 0;
  const t = new Date(coolingUntil).getTime();
  if (Number.isNaN(t)) return 0;
  return Math.max(0, t - now);
}

// 분 단위 경과 시간(체인 단계가 얼마나 오래 걸렸는지). 1분 미만은 "방금".
export function fmtElapsed(ms: number): string {
  if (ms < 60000) return '방금';
  const totalMin = Math.floor(ms / 60000);
  const days = Math.floor(totalMin / 1440);
  const hours = Math.floor((totalMin % 1440) / 60);
  const mins = totalMin % 60;
  if (days > 0) return `${days}일 ${hours}시간`;
  if (hours > 0) return `${hours}시간 ${mins}분`;
  return `${mins}분`;
}

// 분 단위로 끊어 읽는 남은 시간. 0이면 복귀 임박 문구. 1분 미만은 "1분 이내".
export function fmtRemaining(ms: number): string {
  if (ms <= 0) return '복귀 대기';
  const totalMin = Math.floor(ms / 60000);
  if (totalMin < 1) return '1분 이내';
  const days = Math.floor(totalMin / 1440);
  const hours = Math.floor((totalMin % 1440) / 60);
  const mins = totalMin % 60;
  if (days > 0) return `${days}일 ${hours}시간`;
  if (hours > 0) return `${hours}시간 ${mins}분`;
  return `${mins}분`;
}

// -- 창 사용률 조회 ----------------------------------------------------------
// 계정의 특정 창 pct(관측 없으면 null). 막대 렌더가 쓴다.
export function windowPct(account: PoolAccount, windowId: string): number | null {
  const w = account.windows.find((x) => x.windowId === windowId);
  return w ? w.pct : null;
}

// -- 보드 분류 ---------------------------------------------------------------
// 3열 보드 + 하단 별도 행. 대여중 열은 leased·recalling을 함께 담는다.
export interface PoolLanes {
  ready: PoolAccount[];
  leased: PoolAccount[]; // leased + recalling
  cooling: PoolAccount[];
  pinned: PoolAccount[];
  held: PoolAccount[];
}
export function groupAccountsByLane(accounts: PoolAccount[]): PoolLanes {
  const lanes: PoolLanes = { ready: [], leased: [], cooling: [], pinned: [], held: [] };
  for (const a of accounts) {
    switch (a.poolState) {
      case 'ready':
        lanes.ready.push(a);
        break;
      case 'leased':
      case 'recalling':
        lanes.leased.push(a);
        break;
      case 'cooling':
        lanes.cooling.push(a);
        break;
      case 'pinned':
        lanes.pinned.push(a);
        break;
      case 'held':
        lanes.held.push(a);
        break;
    }
  }
  return lanes;
}

// 요약 수치(상단 스트립). 대여중은 leased+recalling 합.
export interface PoolCounts {
  ready: number;
  leased: number;
  cooling: number;
  pinned: number;
  held: number;
}
export function poolCounts(accounts: PoolAccount[]): PoolCounts {
  const lanes = groupAccountsByLane(accounts);
  return {
    ready: lanes.ready.length,
    leased: lanes.leased.length,
    cooling: lanes.cooling.length,
    pinned: lanes.pinned.length,
    held: lanes.held.length,
  };
}
