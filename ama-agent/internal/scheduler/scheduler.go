// Package scheduler drives the auto-switch tick (design note §2, §6.4). When the
// server is in mode=auto it runs an adaptive-period loop that asks tsamx to
// evaluate and maybe switch (`tsamx auto --once`), detects the outcome by
// comparing the active account before/after and by reading exit codes, and emits
// AccountEvents through the offline Outbox. It also watches autoswitch_state.json
// for quarantine changes (dual detection).
//
// Every tsamx mutation sequence — here and in the command handlers — is
// serialized by a single shared "engine lock" so a tick can never interleave
// with a deliver/recall/switch critical section (design decision 4, R3).
package scheduler

import (
	"context"
	"path/filepath"
	"sync"
	"time"

	"github.com/2kwanghee/AMX/ama-agent/internal/reporter"
	"github.com/2kwanghee/AMX/ama-agent/internal/tsamx"
	amxv1 "github.com/2kwanghee/AMX/contracts/gen/go"
	"github.com/fsnotify/fsnotify"
	"google.golang.org/protobuf/types/known/timestamppb"
)

// DefaultInterval is the base tick period when none is configured (design note §2).
const DefaultInterval = 60 * time.Second

// Config assembles a Scheduler.
type Config struct {
	AgentID  string
	Bridge   tsamx.Bridge
	Reporter *reporter.Reporter
	Outbox   *reporter.Outbox
	Engine   *sync.Mutex // shared with the command Handler (R3 engine lock)
	Interval time.Duration
	Now      func() time.Time
	Logf     func(string, ...any)
}

// Scheduler runs the auto-switch loop. Start/Stop are driven by SetSwitchMode
// (auto -> Start, manual -> Stop) and are safe to call from another goroutine.
type Scheduler struct {
	agentID  string
	bridge   tsamx.Bridge
	reporter *reporter.Reporter
	outbox   *reporter.Outbox
	engine   *sync.Mutex
	interval time.Duration
	now      func() time.Time
	logf     func(string, ...any)

	mu      sync.Mutex
	running bool
	cancel  context.CancelFunc
	done    chan struct{}
}

// New validates cfg and returns a Scheduler.
func New(cfg Config) *Scheduler {
	interval := cfg.Interval
	if interval <= 0 {
		interval = DefaultInterval
	}
	now := cfg.Now
	if now == nil {
		now = time.Now
	}
	logf := cfg.Logf
	if logf == nil {
		logf = func(string, ...any) {}
	}
	if cfg.Engine == nil {
		cfg.Engine = &sync.Mutex{}
	}
	return &Scheduler{
		agentID:  cfg.AgentID,
		bridge:   cfg.Bridge,
		reporter: cfg.Reporter,
		outbox:   cfg.Outbox,
		engine:   cfg.Engine,
		interval: interval,
		now:      now,
		logf:     logf,
	}
}

// Start begins the tick loop (and the quarantine watcher) if not already
// running. Idempotent: a second Start while running is a no-op.
func (s *Scheduler) Start() {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.running {
		return
	}
	ctx, cancel := context.WithCancel(context.Background())
	s.cancel = cancel
	s.done = make(chan struct{})
	s.running = true
	go s.loop(ctx, s.done)
	if path := s.bridge.AutoStatePath(); path != "" {
		go s.watchQuarantine(ctx, path)
	}
}

// Stop halts the loop and waits for it to exit. Safe to call when not running.
func (s *Scheduler) Stop() {
	s.mu.Lock()
	if !s.running {
		s.mu.Unlock()
		return
	}
	s.running = false
	cancel := s.cancel
	done := s.done
	s.cancel = nil
	s.mu.Unlock()

	cancel()
	<-done
}

// Running reports whether the loop is active.
func (s *Scheduler) Running() bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.running
}

func (s *Scheduler) loop(ctx context.Context, done chan struct{}) {
	defer close(done)
	timer := time.NewTimer(s.interval)
	defer timer.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-timer.C:
			maxPct := s.Tick(ctx)
			timer.Reset(s.nextInterval(maxPct))
		}
	}
}

// nextInterval adapts the period to pressure: the busier the pool, the sooner
// the next tick. Bounded to [interval/4, interval].
func (s *Scheduler) nextInterval(maxPct float64) time.Duration {
	d := s.interval
	switch {
	case maxPct >= 90:
		d = s.interval / 4
	case maxPct >= 75:
		d = s.interval / 2
	}
	if min := s.interval / 4; d < min {
		d = min
	}
	if d <= 0 {
		d = s.interval
	}
	return d
}

// Tick runs one auto-switch evaluation and enqueues any resulting events. It
// returns the pool's max utilization (for adaptive scheduling). The before/auto/
// report sequence is held under the engine lock so it cannot interleave with a
// command handler's tsamx critical section (R3).
func (s *Scheduler) Tick(ctx context.Context) float64 {
	s.engine.Lock()
	before, _ := s.bridge.Status(ctx)
	code, autoErr := s.bridge.AutoOnce(ctx)
	rep, repErr := s.reporter.BuildUsageReport(ctx, amxv1.UsageReport_TRIGGER_SCHEDULE)
	s.engine.Unlock()

	if autoErr != nil {
		s.logf("scheduler: auto --once: %v", autoErr)
	}
	if repErr != nil {
		s.logf("scheduler: build report: %v", repErr)
	}

	var beforeEmail, afterEmail string
	if before != nil {
		beforeEmail = before.ActiveEmail
	}
	var pool *amxv1.PoolSummary
	var to *amxv1.AccountRef
	if rep != nil {
		pool = rep.GetPoolSummary()
		if a := rep.GetActiveAccount(); a != nil {
			afterEmail = a.GetEmail()
			to = a
		}
	}
	activeChanged := afterEmail != "" && afterEmail != beforeEmail

	// A switch occurred: exit code 0 (switched) or the active account moved
	// (covers a switch tsamx made without a 0 we observed). trigger=at-limit.
	if code == 0 || activeChanged {
		ev := &amxv1.AccountEvent{
			SchemaVersion: 1,
			AgentId:       s.agentID,
			EventId:       reporter.NewEventID(),
			OccurredAt:    timestamppb.New(s.now().UTC()),
			Kind:          amxv1.AccountEvent_KIND_SWITCH,
			Trigger:       amxv1.AccountEvent_TRIGGER_AT_LIMIT,
			To:            to,
			PoolSummary:   pool,
		}
		if beforeEmail != "" {
			ev.From = &amxv1.AccountRef{Email: beforeEmail}
		}
		s.outbox.Enqueue(ev)
	}

	// All accounts exhausted: exit code 3 (blocked) or the pool summary says so.
	// Critical — AMS alerts and considers an extra assignment (§6.4).
	if code == 3 || (pool != nil && pool.GetAllExhausted()) {
		s.outbox.Enqueue(&amxv1.AccountEvent{
			SchemaVersion: 1,
			AgentId:       s.agentID,
			EventId:       reporter.NewEventID(),
			OccurredAt:    timestamppb.New(s.now().UTC()),
			Kind:          amxv1.AccountEvent_KIND_ALL_EXHAUSTED,
			PoolSummary:   pool,
		})
	}

	if pool != nil {
		return pool.GetMaxUtilizationPct()
	}
	return 0
}

// watchQuarantine watches autoswitch_state.json for newly quarantined accounts
// (active-invariant changes a status diff would miss, design note §2). It watches
// the parent directory so tsamx's atomic rename-write is observed.
func (s *Scheduler) watchQuarantine(ctx context.Context, path string) {
	w, err := fsnotify.NewWatcher()
	if err != nil {
		s.logf("scheduler: fsnotify: %v", err)
		return
	}
	defer w.Close()
	dir := filepath.Dir(path)
	if err := w.Add(dir); err != nil {
		s.logf("scheduler: watch %s: %v", dir, err)
		return
	}
	clean := filepath.Clean(path)
	last, _ := s.bridge.ReadQuarantine(ctx)
	for {
		select {
		case <-ctx.Done():
			return
		case ev, ok := <-w.Events:
			if !ok {
				return
			}
			if filepath.Clean(ev.Name) != clean {
				continue
			}
			cur, err := s.bridge.ReadQuarantine(ctx)
			if err != nil {
				continue
			}
			for num, email := range cur {
				if _, had := last[num]; !had {
					s.enqueueQuarantine(email)
				}
			}
			last = cur
		case werr, ok := <-w.Errors:
			if !ok {
				return
			}
			s.logf("scheduler: watch error: %v", werr)
		}
	}
}

func (s *Scheduler) enqueueQuarantine(email string) {
	s.outbox.Enqueue(&amxv1.AccountEvent{
		SchemaVersion: 1,
		AgentId:       s.agentID,
		EventId:       reporter.NewEventID(),
		OccurredAt:    timestamppb.New(s.now().UTC()),
		Kind:          amxv1.AccountEvent_KIND_QUARANTINE,
		Trigger:       amxv1.AccountEvent_TRIGGER_FAILOVER,
		From:          &amxv1.AccountRef{Email: email},
	})
}
