package tsamx

import (
	"context"
	"fmt"
	"sync"

	"github.com/2kwanghee/AMX/ama-agent/internal/provider"
)

// Fake implements the provider.Bridge control surface in memory for tests.
var _ provider.Bridge = (*Fake)(nil)

// Fake is an in-memory Bridge for tests. It models the tsamx local pool: a set
// of accounts keyed by email, an active account, enabled/disabled flags, and a
// call log. It performs no exec.
type Fake struct {
	mu       sync.Mutex
	accounts map[string]*fakeAccount // keyed by email
	order    []string                // insertion order -> slot numbers
	active   string                  // email of the active account
	Calls    []string                // ordered method log, for assertions

	// AddErr, if set, is returned by Add (to test failure paths).
	AddErr error

	// AutoFn, if set, models one `tsamx auto --once` tick: it may mutate the pool
	// through the Fake's own (locked) methods, e.g. SetActiveEmail, and returns
	// the CLI exit code. When nil, AutoOnce is a no-op returning AutoCode. It is
	// invoked WITHOUT the Fake lock held, so it may call any Fake method.
	AutoFn func(f *Fake) int
	// AutoCode is the exit code AutoOnce returns when AutoFn is nil (default 2,
	// "no action").
	AutoCode int
	// AutoErr, if set, is returned by AutoOnce.
	AutoErr error

	// Threshold records the last ConfigSetThreshold value (test assertion).
	Threshold float64
	// Configs records the last value passed to ConfigSet per key (F4 policy,
	// e.g. "cooldown_seconds"/"hysteresis_pct"; test assertion).
	Configs map[string]float64
	// ConfigErr, if set, is returned by ConfigSet.
	ConfigErr error
	// StatePath is returned by AutoStatePath (test seeding for the watcher).
	StatePath string
	// quarantine is number->email, returned by ReadQuarantine.
	quarantine map[string]string
}

type fakeAccount struct {
	email          string
	org            string
	disabled       bool
	usage          *provider.Usage
	usageStatus    string // empty defaults to "ok" in List
	usageFetchedAt string // RFC3339; empty leaves the row field unset
	cred           []byte
}

// NewFake returns an empty Fake.
func NewFake() *Fake {
	return &Fake{
		accounts:   make(map[string]*fakeAccount),
		quarantine: make(map[string]string),
		Configs:    make(map[string]float64),
		AutoCode:   2, // no action by default
	}
}

func (f *Fake) log(format string, a ...any) {
	f.Calls = append(f.Calls, fmt.Sprintf(format, a...))
}

// Add inserts or updates an account.
func (f *Fake) Add(_ context.Context, req provider.AddRequest) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.AddErr != nil {
		return f.AddErr
	}
	f.log("add %s enable=%v", req.Email, req.Enable)
	acc, ok := f.accounts[req.Email]
	if !ok {
		acc = &fakeAccount{email: req.Email}
		f.accounts[req.Email] = acc
		f.order = append(f.order, req.Email)
	}
	acc.cred = append([]byte(nil), req.CredentialJSON...)
	acc.disabled = !req.Enable
	// Model real `tsamx add`: capturing a slot makes it the active account
	// (exec.go Add / SSOT §6.3). deliver relies on this to know it must restore
	// the previously-active account afterward.
	f.active = req.Email
	return nil
}

// Remove deletes an account by email.
func (f *Fake) Remove(_ context.Context, account string) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.log("remove %s", account)
	if _, ok := f.accounts[account]; !ok {
		return nil // idempotent
	}
	delete(f.accounts, account)
	for i, e := range f.order {
		if e == account {
			f.order = append(f.order[:i], f.order[i+1:]...)
			break
		}
	}
	if f.active == account {
		f.active = ""
	}
	return nil
}

// Enable marks an account as a rotation candidate.
func (f *Fake) Enable(_ context.Context, account string) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.log("enable %s", account)
	if acc, ok := f.accounts[account]; ok {
		acc.disabled = false
	}
	return nil
}

// Disable holds an account out of rotation (credential kept).
func (f *Fake) Disable(_ context.Context, account string) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.log("disable %s", account)
	if acc, ok := f.accounts[account]; ok {
		acc.disabled = true
	}
	return nil
}

// Switch makes account the active one.
func (f *Fake) Switch(_ context.Context, target string) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.log("switch %s", target)
	if _, ok := f.accounts[target]; !ok {
		return fmt.Errorf("fake: switch to unknown account %q", target)
	}
	f.active = target
	return nil
}

// List returns the current pool as a ListResult.
func (f *Fake) List(_ context.Context) (*provider.ListResult, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	res := &provider.ListResult{SchemaVersion: 1}
	for i, e := range f.order {
		acc := f.accounts[e]
		num := i + 1
		// Mirror the real ExecBridge.List: dual-record the Claude-shaped
		// five_hour/seven_day into the vendor-neutral Windows list so the reporter
		// (which now consumes Windows exclusively) sees what production emits. Only
		// project when a test seeded the positional fields without Windows; a test
		// that seeds Windows directly (e.g. a codex-shaped pool) is left untouched.
		// A fresh copy keeps the projection idempotent across repeated List calls.
		usage := acc.usage
		if usage != nil && usage.Windows == nil {
			u := *usage
			u.Windows = claudeWindows(&u)
			usage = &u
		}
		status := acc.usageStatus
		if status == "" {
			status = "ok"
		}
		row := provider.AccountRow{
			Number:           num,
			Email:            acc.email,
			OrganizationName: acc.org,
			Active:           f.active == e,
			Disabled:         acc.disabled,
			UsageStatus:      status,
			Usage:            usage,
			UsageFetchedAt:   acc.usageFetchedAt,
		}
		if f.active == e {
			n := num
			res.ActiveAccountNumber = &n
		}
		res.Accounts = append(res.Accounts, row)
	}
	return res, nil
}

// Status returns the active account.
func (f *Fake) Status(ctx context.Context) (*provider.StatusResult, error) {
	list, err := f.List(ctx)
	if err != nil {
		return nil, err
	}
	res := &provider.StatusResult{ActiveAccountNumber: list.ActiveAccountNumber}
	for _, a := range list.Accounts {
		if a.Active {
			res.ActiveEmail = a.Email
			break
		}
	}
	return res, nil
}

// SwitchStrategy models `tsamx switch --strategy`: it activates the first
// enabled, non-active account (deterministic by insertion order).
func (f *Fake) SwitchStrategy(_ context.Context, strategy string) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.log("switch --strategy %s", strategy)
	for _, e := range f.order {
		acc := f.accounts[e]
		if e != f.active && !acc.disabled {
			f.active = e
			return nil
		}
	}
	return nil
}

// ConfigSetThreshold records the injected threshold.
func (f *Fake) ConfigSetThreshold(_ context.Context, pct float64) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.log("config set autoswitch.threshold %g", pct)
	f.Threshold = pct
	return nil
}

// validAutoswitchKeys mirrors the numeric autoswitch settings tsamx actually
// accepts (settings.py SETTING_SPECS json_key, camelCase). The real
// `tsamx config set autoswitch.<key>` rejects any other key with a non-zero
// exit, so the Fake rejects them too — otherwise a snake_case or typo'd key
// (the F4 regression: "cooldown_seconds" instead of "cooldownSeconds") passes
// the unit tests but every SetPolicy diverges in production.
var validAutoswitchKeys = map[string]bool{
	"threshold":       true,
	"cooldownSeconds": true,
	"hysteresisPct":   true,
}

// ConfigSet records the injected value for the given autoswitch key, rejecting
// any key tsamx would not accept (contract check; see validAutoswitchKeys).
func (f *Fake) ConfigSet(_ context.Context, key string, value float64) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.ConfigErr != nil {
		return f.ConfigErr
	}
	if !validAutoswitchKeys[key] {
		return fmt.Errorf("tsamx: unknown setting autoswitch.%s", key)
	}
	f.log("config set autoswitch.%s %g", key, value)
	f.Configs[key] = value
	return nil
}

// AutoOnce runs the programmed tick (AutoFn) under the lock and returns its exit
// code, or AutoCode when AutoFn is nil.
func (f *Fake) AutoOnce(_ context.Context) (int, error) {
	f.mu.Lock()
	f.log("auto --once")
	autoErr := f.AutoErr
	fn := f.AutoFn
	code := f.AutoCode
	f.mu.Unlock()
	// AutoFn is invoked without the lock so it can call locked Fake methods.
	if autoErr != nil {
		return 1, autoErr
	}
	if fn != nil {
		return fn(f), nil
	}
	return code, nil
}

// AutoStatePath returns the seeded state-file path (empty disables the watcher).
func (f *Fake) AutoStatePath() string {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.StatePath
}

// ReadQuarantine returns a copy of the in-memory quarantine map.
func (f *Fake) ReadQuarantine(_ context.Context) (map[string]string, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	out := make(map[string]string, len(f.quarantine))
	for k, v := range f.quarantine {
		out[k] = v
	}
	return out, nil
}

// DeliverLock records the call and returns a no-op release. The Fake models no
// real config home, so there is no cross-process lock to take; tests assert the
// call ordering (lock is taken around the swap) via the call log.
func (f *Fake) DeliverLock(_ context.Context) func() error {
	f.mu.Lock()
	f.log("deliver_lock")
	f.mu.Unlock()
	return func() error {
		f.mu.Lock()
		f.log("deliver_unlock")
		f.mu.Unlock()
		return nil
	}
}

// --- test helpers -----------------------------------------------------------

// SetQuarantine sets the quarantine map returned by ReadQuarantine.
func (f *Fake) SetQuarantine(q map[string]string) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.quarantine = make(map[string]string, len(q))
	for k, v := range q {
		f.quarantine[k] = v
	}
}

// SetActiveEmail forces the active account (test seeding).
func (f *Fake) SetActiveEmail(email string) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.active = email
}

// ActiveEmail returns the current active account's email.
func (f *Fake) ActiveEmail() string {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.active
}

// SetUsage attaches usage to an account (test seeding).
func (f *Fake) SetUsage(email string, u *provider.Usage) {
	f.mu.Lock()
	defer f.mu.Unlock()
	if acc, ok := f.accounts[email]; ok {
		acc.usage = u
	}
}

// SetUsageStatus overrides the list --json usageStatus for an account and, when
// the status implies no measurement (e.g. relogin_required, token_expired),
// clears any seeded usage so the row matches what tsamx emits (test seeding).
func (f *Fake) SetUsageStatus(email, status string) {
	f.mu.Lock()
	defer f.mu.Unlock()
	if acc, ok := f.accounts[email]; ok {
		acc.usageStatus = status
		if status != "ok" {
			acc.usage = nil
		}
	}
}

// SetUsageFetchedAt sets the RFC3339 usageFetchedAt freshness stamp for an
// account (test seeding).
func (f *Fake) SetUsageFetchedAt(email, ts string) {
	f.mu.Lock()
	defer f.mu.Unlock()
	if acc, ok := f.accounts[email]; ok {
		acc.usageFetchedAt = ts
	}
}

// CallLog returns a snapshot copy of the ordered method log (race-safe).
func (f *Fake) CallLog() []string {
	f.mu.Lock()
	defer f.mu.Unlock()
	return append([]string(nil), f.Calls...)
}

// ConfigValue returns the last value recorded by ConfigSet for key and whether
// it was ever set (test assertion).
func (f *Fake) ConfigValue(key string) (float64, bool) {
	f.mu.Lock()
	defer f.mu.Unlock()
	v, ok := f.Configs[key]
	return v, ok
}

// Has reports whether an account exists.
func (f *Fake) Has(email string) bool {
	f.mu.Lock()
	defer f.mu.Unlock()
	_, ok := f.accounts[email]
	return ok
}

// Disabled reports an account's disabled flag.
func (f *Fake) Disabled(email string) (bool, bool) {
	f.mu.Lock()
	defer f.mu.Unlock()
	acc, ok := f.accounts[email]
	if !ok {
		return false, false
	}
	return acc.disabled, true
}
