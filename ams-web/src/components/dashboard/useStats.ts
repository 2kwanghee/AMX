'use client';

// 대시보드 집계 통계 SWR 훅 모음(design-notes/dashboard-redesign-plan.md 부록 A).
// 키는 ['stats', tenantId, range, path(, by)] — 기간·집계축이 바뀔 때만 새 키로
// 재요청되고, keepPreviousData로 그동안은 이전 값을 그대로 보여준다(스켈레톤 금지
// 규칙). 폴링은 45초로 다른 사용량 패널과 같은 느슨한 주기를 쓴다.
import useSWR from 'swr';
import { api } from '@/lib/api-client/client';
import { markDataArrived } from '@/components/common';
import type {
  StatsAccounts,
  StatsFlows,
  StatsHeatmap,
  StatsRange,
  StatsSummary,
  StatsTimeseries,
  StatsTimeseriesBy,
} from '@/lib/api-client/types';

const POLL = 45000;

function onData() {
  markDataArrived();
}

export function useStatsSummary(tenantId: string, range: StatsRange) {
  return useSWR<StatsSummary>(
    ['stats', tenantId, range, 'summary'],
    () => api.getStatsSummary(tenantId, range),
    { refreshInterval: POLL, keepPreviousData: true, onSuccess: onData },
  );
}

export function useStatsTimeseries(tenantId: string, range: StatsRange, by: StatsTimeseriesBy) {
  return useSWR<StatsTimeseries>(
    ['stats', tenantId, range, 'timeseries', by],
    () => api.getStatsTimeseries(tenantId, by, range),
    { refreshInterval: POLL, keepPreviousData: true, onSuccess: onData },
  );
}

export function useStatsFlows(tenantId: string, range: StatsRange) {
  return useSWR<StatsFlows>(
    ['stats', tenantId, range, 'flows'],
    () => api.getStatsFlows(tenantId, range),
    { refreshInterval: POLL, keepPreviousData: true, onSuccess: onData },
  );
}

export function useStatsAccounts(tenantId: string, range: StatsRange) {
  return useSWR<StatsAccounts>(
    ['stats', tenantId, range, 'accounts'],
    () => api.getStatsAccounts(tenantId, range),
    { refreshInterval: POLL, keepPreviousData: true, onSuccess: onData },
  );
}

export function useStatsHeatmap(tenantId: string, range: StatsRange) {
  return useSWR<StatsHeatmap>(
    ['stats', tenantId, range, 'heatmap'],
    () => api.getStatsHeatmap(tenantId, range),
    { refreshInterval: POLL, keepPreviousData: true, onSuccess: onData },
  );
}
