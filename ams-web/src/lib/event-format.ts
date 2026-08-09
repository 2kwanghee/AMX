// E2 timeline render logic, split out from the ServersPanel client component so
// it is unit-testable in the node test environment (no DOM/React needed).
//
// The events endpoint returns UsageSnapshot rows whose payload is the raw
// AccountEvent proto rendered with proto field names (contracts/proto/amx.proto):
// snake_case keys, and kind/trigger as their proto enum names such as
// "KIND_SWITCH" / "TRIGGER_AT_LIMIT".
import type { ServerEvent } from './api-client/types';

export function eventLabel(kind?: string): string {
  return (kind ?? 'event').replace(/^KIND_/, '').toLowerCase().replace(/_/g, ' ');
}

export function triggerLabel(trigger?: string): string {
  return (trigger ?? '').replace(/^TRIGGER_/, '').toLowerCase().replace(/_/g, ' ');
}

export interface EventRow {
  kind: string;
  trigger: string;
  /** "from → to" when either side is known, else null. */
  transition: string | null;
  detail: string;
  reportedAt: string;
}

/** Map an events-endpoint row to the display fields the timeline renders. */
export function formatEventRow(ev: ServerEvent): EventRow {
  const p = ev.payload ?? {};
  const from = p.from?.email;
  const to = p.to?.email;
  return {
    kind: eventLabel(p.kind),
    trigger: triggerLabel(p.trigger),
    transition: from || to ? `${from ?? '—'} → ${to ?? '—'}` : null,
    detail: p.detail ?? '',
    reportedAt: ev.reportedAt,
  };
}
