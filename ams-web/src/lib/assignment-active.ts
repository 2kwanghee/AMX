// AssignmentsPanel.tsx가 JSX를 포함해 vitest의 node 환경(esbuild 기본 JSX
// 트랜스폼, React 자동 임포트 없음)에서 직접 import하면 "React is not defined"로
// 깨진다. currentActiveByServer는 순수 함수라 JSX와 무관하므로, 테스트 가능하게
// 이 파일로 분리하고 AssignmentsPanel.tsx·dashboard/page.tsx는 그대로 재export를
// 통해 쓴다(기존 import 경로 불변).
import type { Account, Assignment } from './api-client/types';

// 같은 서버에 할당된 계정 중 lastSwitchedAt이 가장 최신(non-null)인 계정을 그
// 서버의 "현재 활성"으로 판정한다. 반환: serverId -> 현재 활성 accountId.
export function currentActiveByServer(
  assignments: Assignment[],
  accounts: Account[],
): Map<string, string> {
  const switchedAt = new Map(accounts.map((a) => [a.id, a.lastSwitchedAt]));
  const best = new Map<string, { accountId: string; t: number }>();
  for (const a of assignments) {
    // "현재 활성"은 로테이션 후보(active)만을 뜻한다. inactive(로테이션 제외)·
    // quarantined(소진/실패)·pending(미전달)·recalling(회수 중)·detached(종말)는
    // 전부 스위칭 후보가 아니므로 제외한다.
    if (a.state !== 'active') continue;
    const iso = switchedAt.get(a.accountId);
    if (!iso) continue;
    const t = new Date(iso).getTime();
    if (Number.isNaN(t)) continue;
    const cur = best.get(a.serverId);
    if (!cur || t > cur.t) best.set(a.serverId, { accountId: a.accountId, t });
  }
  const out = new Map<string, string>();
  for (const [serverId, v] of best) out.set(serverId, v.accountId);
  return out;
}
