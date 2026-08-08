// Package reporter builds usage reports and account events from the tsamx cache
// (design note §6, SSOT §6.5). It re-serializes `tsamx list --json` — it does NOT
// poll the usage API itself. P2 skeleton: report construction + an offline outbox
// with dedupe; the 5-minute ticker is wired in cmd/ama but left un-driven per P2.
package reporter

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"sync"
	"time"

	"github.com/2kwanghee/AMX/ama-agent/internal/tsamx"
	amxv1 "github.com/2kwanghee/AMX/contracts/gen/go"
	"google.golang.org/protobuf/types/known/timestamppb"
)

// NewEventID returns a random hex identifier for an AccountEvent's outbox dedupe
// key (proto AccountEvent.event_id). Falls back to a timestamp-derived value if
// the CSPRNG is unavailable (never expected).
func NewEventID() string {
	var b [16]byte
	if _, err := rand.Read(b[:]); err != nil {
		return "evt-" + time.Now().UTC().Format("20060102T150405.000000000")
	}
	return hex.EncodeToString(b[:])
}

// SwitchThresholdPct is the utilization at/above which an account counts as
// exhausted for pool summary purposes (D10, injected into tsamx as 95).
const SwitchThresholdPct = 95.0

// Reporter turns the local pool into UsageReports.
type Reporter struct {
	agentID string
	bridge  tsamx.Bridge
	now     func() time.Time
}

// New returns a Reporter.
func New(agentID string, bridge tsamx.Bridge, now func() time.Time) *Reporter {
	if now == nil {
		now = time.Now
	}
	return &Reporter{agentID: agentID, bridge: bridge, now: now}
}

// BuildUsageReport reads the tsamx cache and projects it onto a UsageReport.
func (r *Reporter) BuildUsageReport(ctx context.Context, trigger amxv1.UsageReport_Trigger) (*amxv1.UsageReport, error) {
	list, err := r.bridge.List(ctx)
	if err != nil {
		return nil, err
	}
	rep := &amxv1.UsageReport{
		SchemaVersion: 1,
		AgentId:       r.agentID,
		GeneratedAt:   timestamppb.New(r.now().UTC()),
		Trigger:       trigger,
	}
	var (
		total, active, eligible, quarantined uint32
		maxPct                               float64
		allExhausted                         = len(list.Accounts) > 0
	)
	for _, row := range list.Accounts {
		total++
		au := accountUsage(row)
		if row.Active {
			rep.ActiveAccount = au.GetAccount()
		}
		pct := maxWindowPct(row.Usage)
		if pct > maxPct {
			maxPct = pct
		}
		switch {
		case row.Disabled:
			// out of rotation; not eligible, not counted as exhausted driver
			allExhausted = false
		case row.UsageStatus == "quarantined":
			quarantined++
		default:
			active++
			if pct < SwitchThresholdPct {
				eligible++
				allExhausted = false
			}
		}
		rep.Accounts = append(rep.Accounts, au)
	}
	rep.PoolSummary = &amxv1.PoolSummary{
		Total:             total,
		Active:            active,
		Eligible:          eligible,
		Quarantined:       quarantined,
		AllExhausted:      allExhausted,
		MaxUtilizationPct: maxPct,
	}
	return rep, nil
}

func accountUsage(row tsamx.AccountRow) *amxv1.AccountUsage {
	au := &amxv1.AccountUsage{
		Account:   &amxv1.AccountRef{Email: row.Email},
		IsCurrent: row.Active,
	}
	switch {
	case row.Disabled:
		au.AllocationStatus = amxv1.AllocationStatus_ALLOCATION_STATUS_INACTIVE
	case row.UsageStatus == "quarantined":
		au.AllocationStatus = amxv1.AllocationStatus_ALLOCATION_STATUS_QUARANTINED
	default:
		au.AllocationStatus = amxv1.AllocationStatus_ALLOCATION_STATUS_ACTIVE
	}
	if row.Usage != nil {
		if w := row.Usage.FiveHour; w != nil {
			au.FiveHour = &amxv1.UsageWindow{Pct: w.Pct, ResetsAt: parseTime(w.ResetsAt)}
		}
		if w := row.Usage.SevenDay; w != nil {
			au.SevenDay = &amxv1.UsageWindow{Pct: w.Pct, ResetsAt: parseTime(w.ResetsAt)}
		}
	}
	return au
}

func maxWindowPct(u *tsamx.Usage) float64 {
	if u == nil {
		return 0
	}
	var m float64
	if u.FiveHour != nil && u.FiveHour.Pct > m {
		m = u.FiveHour.Pct
	}
	if u.SevenDay != nil && u.SevenDay.Pct > m {
		m = u.SevenDay.Pct
	}
	return m
}

func parseTime(s string) *timestamppb.Timestamp {
	if s == "" {
		return nil
	}
	if t, err := time.Parse(time.RFC3339, s); err == nil {
		return timestamppb.New(t)
	}
	return nil
}

// Outbox queues AccountEvents while AMS is unreachable and flushes them on
// reconnect, deduplicated by event_id (SSOT §6.3 offline outbox).
type Outbox struct {
	mu    sync.Mutex
	queue []*amxv1.AccountEvent
	seen  map[string]struct{}
}

// NewOutbox returns an empty outbox.
func NewOutbox() *Outbox {
	return &Outbox{seen: make(map[string]struct{})}
}

// Enqueue adds an event unless its event_id was already queued or flushed.
func (o *Outbox) Enqueue(ev *amxv1.AccountEvent) {
	o.mu.Lock()
	defer o.mu.Unlock()
	id := ev.GetEventId()
	if id != "" {
		if _, ok := o.seen[id]; ok {
			return
		}
		o.seen[id] = struct{}{}
	}
	o.queue = append(o.queue, ev)
}

// Depth returns the number of queued events (for Heartbeat.outbox_depth).
func (o *Outbox) Depth() int {
	o.mu.Lock()
	defer o.mu.Unlock()
	return len(o.queue)
}

// Flush sends each queued event via send, stopping at the first error and
// keeping the unsent remainder for the next attempt.
func (o *Outbox) Flush(send func(*amxv1.AccountEvent) error) error {
	o.mu.Lock()
	pending := o.queue
	o.queue = nil
	o.mu.Unlock()
	for i, ev := range pending {
		if err := send(ev); err != nil {
			o.mu.Lock()
			o.queue = append(pending[i:], o.queue...)
			o.mu.Unlock()
			return err
		}
	}
	return nil
}
