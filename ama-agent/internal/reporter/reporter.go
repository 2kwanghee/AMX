// Package reporter builds usage reports and account events from the tsamx cache
// (design note §6, SSOT §6.5). It re-serializes `tsamx list --json` — it does NOT
// poll the usage API itself. Report construction plus an offline outbox with
// bounded dedupe; the 5-minute usage ticker is driven from cmd/ama/main.go (§6.5).
package reporter

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"sort"
	"sync"
	"time"

	"github.com/2kwanghee/AMX/ama-agent/internal/provider"
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

// Reporter turns the local pool into UsageReports. It reads one bridge per
// registered provider and sums their pools into a single report; today only
// "claude" is registered, so the report matches the pre-multi-provider output
// except for the provider stamp added to each account.
type Reporter struct {
	agentID string
	bridges map[string]provider.Bridge
	now     func() time.Time
	// resolveID maps a live (provider, email) to its AMS identity from the
	// manifest. A bridge knows an account only by email, but reconcile-on-report
	// (AMS §3) keys drift on ams_account_id, so a report that omits it reads as
	// "account absent" and triggers an endless redelivery — which rewrites the live
	// credential file and defeats the O9 re-sync. The provider is passed too so the
	// same email under two providers resolves to the correct record. nil leaves the
	// report email-only (unit tests).
	resolveID func(providerKey, email string) (amsAccountID, accountUUID string, ok bool)
}

// New returns a Reporter over a provider->bridge registry. A nil/empty map
// produces empty reports (no providers to read).
func New(agentID string, bridges map[string]provider.Bridge, now func() time.Time) *Reporter {
	if now == nil {
		now = time.Now
	}
	return &Reporter{agentID: agentID, bridges: bridges, now: now}
}

// SetIDResolver installs the manifest lookup that stamps ams_account_id (and the
// account UUID) onto each reported account, so AMS can match the report to its
// assignment. Set once at wiring time before the report ticker starts.
func (r *Reporter) SetIDResolver(f func(providerKey, email string) (amsAccountID, accountUUID string, ok bool)) {
	r.resolveID = f
}

// BuildUsageReport reads every registered provider's pool and projects them onto
// a single UsageReport. Providers are read in a stable (sorted) order so the
// report is deterministic; the accounts[] list carries all providers, each
// account stamped with the provider it came from.
//
// PoolSummary and ActiveAccount, however, are scoped to the auto-switch provider
// (DefaultProvider, "claude") ALONE. Those two fields feed the auto-switch
// control loop (scheduler AllExhausted alert, AMS extra-assignment decision), and
// only Claude rotates. Summing a non-rotating provider in would corrupt them: a
// codex account with no usage counts as eligible (pct 0), so AllExhausted would
// never fire even with every Claude account exhausted, and the single
// ActiveAccount field would be overwritten by a provider that has no active-slot
// notion. accounts[] stays full so AMS still sees every provider's usage.
//
// An error from any provider's List fails the whole report (a partial report
// would misreport the pool to AMS).
func (r *Reporter) BuildUsageReport(ctx context.Context, trigger amxv1.UsageReport_Trigger) (*amxv1.UsageReport, error) {
	rep := &amxv1.UsageReport{
		SchemaVersion: 1,
		AgentId:       r.agentID,
		GeneratedAt:   timestamppb.New(r.now().UTC()),
		Trigger:       trigger,
	}
	var (
		total, active, eligible, quarantined uint32
		maxPct                               float64
		// relievesExhaustion is set by any account that keeps the pool from being
		// fully exhausted (a disabled account, or an eligible one under threshold).
		// allExhausted is then total>0 && !relievesExhaustion. Only the auto-switch
		// provider's accounts contribute, so it is numerically identical to the
		// former single-provider computation.
		relievesExhaustion bool
	)
	keys := make([]string, 0, len(r.bridges))
	for k := range r.bridges {
		keys = append(keys, k)
	}
	sort.Strings(keys)

	for _, providerKey := range keys {
		list, err := r.bridges[providerKey].List(ctx)
		if err != nil {
			return nil, err
		}
		// The PoolSummary/ActiveAccount are the auto-switch control signals; only
		// the rotating provider feeds them (see the doc comment).
		summaryScope := providerKey == provider.DefaultProvider
		for _, row := range list.Accounts {
			au := accountUsage(row)
			// Stamp the provider this account belongs to (proto AccountRef.provider).
			au.Account.Provider = providerKey
			// Stamp the AMS identity so reconcile-on-report can match this account to
			// its assignment; without it the account reads as absent and AMS
			// redelivers (clobbering a locally rotated credential the O9 re-sync must
			// observe).
			if r.resolveID != nil {
				if amsID, accUUID, ok := r.resolveID(providerKey, row.Email); ok {
					au.Account.AmsAccountId = amsID
					if accUUID != "" {
						au.Account.AccountUuid = accUUID
					}
				}
			}
			rep.Accounts = append(rep.Accounts, au)

			if !summaryScope {
				continue
			}
			total++
			if row.Active {
				rep.ActiveAccount = au.GetAccount()
			}
			pct := maxWindowPct(au.Windows)
			if pct > maxPct {
				maxPct = pct
			}
			switch {
			case row.Disabled:
				// out of rotation; not eligible, not counted as exhausted driver
				relievesExhaustion = true
			case row.UsageStatus == "quarantined":
				quarantined++
			default:
				active++
				if pct < SwitchThresholdPct {
					eligible++
					relievesExhaustion = true
				}
			}
		}
	}
	rep.PoolSummary = &amxv1.PoolSummary{
		Total:             total,
		Active:            active,
		Eligible:          eligible,
		Quarantined:       quarantined,
		AllExhausted:      total > 0 && !relievesExhaustion,
		MaxUtilizationPct: maxPct,
	}
	return rep, nil
}

func accountUsage(row provider.AccountRow) *amxv1.AccountUsage {
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
		// The vendor-neutral Usage.Windows list is the single source: every
		// provider's bridge fills it (claude dual-records five_hour/seven_day into
		// it, codex fills primary/secondary). Each window is mirrored into proto
		// windows[]; the legacy positional five_hour/seven_day fields are re-derived
		// ONLY for windows carrying those ids, so a codex account (which has neither)
		// leaves them nil. For a claude account this reproduces the pre-P3 output
		// byte-for-byte — the windows arrive already ordered five_hour then seven_day.
		for i := range row.Usage.Windows {
			w := row.Usage.Windows[i]
			au.Windows = append(au.Windows, &amxv1.QuotaWindow{
				Id:            w.Id,
				Pct:           w.Pct,
				ResetsAt:      parseTime(w.ResetsAt),
				WindowMinutes: int32(w.WindowMinutes),
			})
			switch w.Id {
			case "five_hour":
				au.FiveHour = &amxv1.UsageWindow{Pct: w.Pct, ResetsAt: parseTime(w.ResetsAt)}
			case "seven_day":
				au.SevenDay = &amxv1.UsageWindow{Pct: w.Pct, ResetsAt: parseTime(w.ResetsAt)}
			}
		}
	}
	return au
}

// maxWindowPct returns the highest utilization across the generalized windows.
// Numerically equivalent to the former max(5h, 7d): windows carries exactly the
// present positional windows. Empty windows (no usage) yields 0.
func maxWindowPct(windows []*amxv1.QuotaWindow) float64 {
	var m float64
	for _, w := range windows {
		if w.GetPct() > m {
			m = w.GetPct()
		}
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
		// Hold o.mu across the whole compaction (snapshot + file rewrite + fd swap).
		// Enqueue's disk append also runs under o.mu, so it cannot interleave here:
		// otherwise an Enqueue between snapshotting the live set and swapping in the
		// rewritten file would write to the old, about-to-be-unlinked inode and be
		// lost (F1). o.queue is the authoritative live set at swap time, so no queued
		// event is dropped. Compaction is rare (per outboxCompactThreshold deletes)
		// and rewrites only the small live set, so the lock hold is brief.
		o.mu.Lock()
		_ = o.log.compact(o.queue)
		o.mu.Unlock()
	}
}
