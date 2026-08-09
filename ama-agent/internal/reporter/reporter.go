// Package reporter builds usage reports and account events from the tsamx cache
// (design note §6, SSOT §6.5). It re-serializes `tsamx list --json` — it does NOT
// poll the usage API itself. Report construction plus an offline outbox with
// bounded dedupe; the 5-minute usage ticker is driven from cmd/ama/main.go (§6.5).
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
	// resolveID maps a live account email to its AMS identity from the manifest.
	// tsamx knows an account only by email, but reconcile-on-report (AMS §3) keys
	// drift on ams_account_id, so a report that omits it reads as "account absent"
	// and triggers an endless redelivery — which rewrites the live credential file
	// and defeats the O9 re-sync. nil leaves the report email-only (unit tests).
	resolveID func(email string) (amsAccountID, accountUUID string, ok bool)
}

// New returns a Reporter.
func New(agentID string, bridge tsamx.Bridge, now func() time.Time) *Reporter {
	if now == nil {
		now = time.Now
	}
	return &Reporter{agentID: agentID, bridge: bridge, now: now}
}

// SetIDResolver installs the manifest lookup that stamps ams_account_id (and the
// Claude account UUID) onto each reported account, so AMS can match the report to
// its assignment. Set once at wiring time before the report ticker starts.
func (r *Reporter) SetIDResolver(f func(email string) (amsAccountID, accountUUID string, ok bool)) {
	r.resolveID = f
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
		// Stamp the AMS identity so reconcile-on-report can match this account to
		// its assignment; without it the account reads as absent and AMS redelivers
		// (clobbering a locally rotated credential the O9 re-sync must observe).
		if r.resolveID != nil {
			if amsID, accUUID, ok := r.resolveID(row.Email); ok {
				au.Account.AmsAccountId = amsID
				if accUUID != "" {
					au.Account.AccountUuid = accUUID
				}
			}
		}
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

// outboxDedupeWindow bounds how many recent event_ids the outbox remembers for
// dedupe. On a long-running agent an unbounded seen-set would grow without limit
// (memory leak); a re-send of an event_id evicted past this window is treated as
// new, which the emitters never do in practice (event_ids are fresh per event).
const outboxDedupeWindow = 1024

// Outbox queues AccountEvents while AMS is unreachable and flushes them on
// reconnect, deduplicated by event_id over a bounded recent window (SSOT §6.3
// offline outbox).
//
// When opened with OpenOutbox it is backed by an append-only disk log so a queued
// event survives an agent restart (C1 W1): Enqueue appends durably, and an event
// is deleted from disk only after its send is confirmed (C1 W2, see Flush). A
// memory-only Outbox (NewOutbox) keeps the same semantics minus persistence, for
// callers/tests that do not need restart durability.
type Outbox struct {
	mu    sync.Mutex
	queue []outboxItem
	// seen holds the event_ids currently inside the dedupe window; ring is a
	// fixed-size FIFO of the same ids so seen stays bounded at outboxDedupeWindow.
	seen map[string]struct{}
	ring [outboxDedupeWindow]string
	rlen int // occupied slots (grows to the window size, then stays)
	rpos int // next write index (and, once full, the oldest slot)

	// log is the disk backing; nil for a memory-only Outbox. seq is the monotonic
	// per-event key stamped onto disk add/del records.
	log *outboxLog
	seq uint64

	// flushMu serializes concurrent Flush calls (see Flush).
	flushMu sync.Mutex
}

// NewOutbox returns an empty memory-only outbox (no restart durability).
func NewOutbox() *Outbox {
	return &Outbox{seen: make(map[string]struct{})}
}

// OpenOutbox returns an outbox backed by dir/outbox.log, reloading any events that
// were queued but not yet confirmed-delivered before a restart (C1 W1). The
// dedupe window is reseeded from the reloaded events so a re-enqueue of a still
// pending event is suppressed.
func OpenOutbox(dir string) (*Outbox, error) {
	log, items, maxSeq, err := openOutboxLog(dir)
	if err != nil {
		return nil, err
	}
	o := &Outbox{seen: make(map[string]struct{}), log: log, seq: maxSeq}
	o.queue = items
	for _, it := range items {
		if id := it.ev.GetEventId(); id != "" {
			o.markSeen(id)
		}
	}
	return o, nil
}

// Enqueue adds an event unless its event_id is still inside the dedupe window.
// With a disk backing the event is appended durably before it becomes visible, so
// a crash immediately after enqueue cannot lose it (W1). A disk error is returned
// but the event is still queued in memory so live delivery this session is not
// forfeited.
func (o *Outbox) Enqueue(ev *amxv1.AccountEvent) error {
	o.mu.Lock()
	defer o.mu.Unlock()
	id := ev.GetEventId()
	if id != "" {
		if _, ok := o.seen[id]; ok {
			return nil
		}
		o.markSeen(id)
	}
	o.seq++
	seq := o.seq
	var derr error
	if o.log != nil {
		derr = o.log.appendAdd(seq, ev)
	}
	o.queue = append(o.queue, outboxItem{seq: seq, ev: ev})
	return derr
}

// markSeen records id in the dedupe window, evicting the oldest id once the
// window is full so the seen map cannot grow past outboxDedupeWindow entries.
func (o *Outbox) markSeen(id string) {
	if o.rlen == outboxDedupeWindow {
		delete(o.seen, o.ring[o.rpos]) // slot at rpos is the oldest once full
	} else {
		o.rlen++
	}
	o.ring[o.rpos] = id
	o.rpos = (o.rpos + 1) % outboxDedupeWindow
	o.seen[id] = struct{}{}
}

// Depth returns the number of queued events (for Heartbeat.outbox_depth).
func (o *Outbox) Depth() int {
	o.mu.Lock()
	defer o.mu.Unlock()
	return len(o.queue)
}

// flushMu serializes Flush calls so two flushers (OnConnect reconnect-flush and
// the live drain) cannot both send the same queued event from one snapshot before
// either deletes it.
//
// Flush sends each queued event via send, deleting an event from the queue (and,
// when persisted, durably from disk) only after send reports success. On the
// first error it stops and keeps the unsent remainder for the next attempt. This
// is the C1 W2 confirmation gate: send must not return nil until the event is
// truly on the wire (transport.SendConfirmed awaits stream.Send), so a drop
// between dequeue and stream.Send cannot silently discard an event.
func (o *Outbox) Flush(send func(*amxv1.AccountEvent) error) error {
	o.flushMu.Lock()
	defer o.flushMu.Unlock()

	o.mu.Lock()
	snapshot := append([]outboxItem(nil), o.queue...)
	o.mu.Unlock()

	for _, it := range snapshot {
		if err := send(it.ev); err != nil {
			return err
		}
		o.deleteDelivered(it.seq)
	}
	return nil
}

// deleteDelivered removes a confirmed-delivered event from the queue and retires
// its disk record. A lost del tombstone (crash between success and fsync) at worst
// re-sends the event after restart; AMS tolerates the rare duplicate (design
// note §3), so it is never a correctness hazard.
func (o *Outbox) deleteDelivered(seq uint64) {
	o.mu.Lock()
	for i := range o.queue {
		if o.queue[i].seq == seq {
			o.queue = append(o.queue[:i], o.queue[i+1:]...)
			break
		}
	}
	o.mu.Unlock()

	if o.log == nil {
		return
	}
	_ = o.log.appendDel(seq)
	if o.log.shouldCompact() {
		o.mu.Lock()
		live := append([]outboxItem(nil), o.queue...)
		o.mu.Unlock()
		_ = o.log.compact(live)
	}
}
