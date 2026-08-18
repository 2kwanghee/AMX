// currentActiveByServer는 "현재 활성" 판정을 active 배정으로만 한정해야 한다.
// active가 아닌 상태(inactive/quarantined/pending/recalling/detached)를 후보에
// 넣으면 로테이션 대상이 아닌 계정이 토폴로지·대시보드에 "현재 활성"으로 표시된다.
import { describe, expect, it } from 'vitest';
import { currentActiveByServer } from '@/lib/assignment-active';
import type { Account, Assignment, AssignmentState } from '@/lib/api-client/types';

function account(id: string, lastSwitchedAt?: string): Account {
  return {
    id,
    tenantId: 'ten-1',
    provider: 'claude',
    email: `${id}@acme.io`,
    credentialType: 'oauth',
    status: 'assigned',
    secretMasked: '****',
    lastSwitchedAt,
  };
}

function assignment(
  id: string,
  accountId: string,
  serverId: string,
  state: AssignmentState,
): Assignment {
  return { id, tenantId: 'ten-1', accountId, serverId, state };
}

describe('currentActiveByServer', () => {
  const nonActiveStates: AssignmentState[] = [
    'inactive',
    'quarantined',
    'pending',
    'recalling',
    'detached',
  ];

  for (const state of nonActiveStates) {
    it(`excludes a ${state} assignment even with the most recent lastSwitchedAt`, () => {
      const accounts = [account('acc-1', '2026-08-17T00:00:00Z')];
      const assignments = [assignment('asg-1', 'acc-1', 'srv-1', state)];
      const result = currentActiveByServer(assignments, accounts);
      expect(result.get('srv-1')).toBeUndefined();
    });
  }

  it('picks the active assignment when mixed with non-active states on the same server', () => {
    const accounts = [
      account('acc-1', '2026-08-10T00:00:00Z'),
      account('acc-2', '2026-08-17T00:00:00Z'),
    ];
    const assignments = [
      assignment('asg-1', 'acc-1', 'srv-1', 'quarantined'),
      assignment('asg-2', 'acc-2', 'srv-1', 'active'),
    ];
    const result = currentActiveByServer(assignments, accounts);
    expect(result.get('srv-1')).toBe('acc-2');
  });

  it('picks the account with the most recent lastSwitchedAt among multiple active assignments', () => {
    const accounts = [
      account('acc-1', '2026-08-10T00:00:00Z'),
      account('acc-2', '2026-08-17T00:00:00Z'),
      account('acc-3', '2026-08-05T00:00:00Z'),
    ];
    const assignments = [
      assignment('asg-1', 'acc-1', 'srv-1', 'active'),
      assignment('asg-2', 'acc-2', 'srv-1', 'active'),
      assignment('asg-3', 'acc-3', 'srv-1', 'active'),
    ];
    const result = currentActiveByServer(assignments, accounts);
    expect(result.get('srv-1')).toBe('acc-2');
  });

  it('excludes an active assignment whose account has no lastSwitchedAt', () => {
    const accounts = [account('acc-1', undefined)];
    const assignments = [assignment('asg-1', 'acc-1', 'srv-1', 'active')];
    const result = currentActiveByServer(assignments, accounts);
    expect(result.get('srv-1')).toBeUndefined();
  });

  it('excludes an active assignment whose account has an unparsable lastSwitchedAt', () => {
    const accounts = [account('acc-1', 'not-a-date')];
    const assignments = [assignment('asg-1', 'acc-1', 'srv-1', 'active')];
    const result = currentActiveByServer(assignments, accounts);
    expect(result.get('srv-1')).toBeUndefined();
  });
});
