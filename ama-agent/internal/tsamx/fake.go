package tsamx

import (
	"context"
	"fmt"
	"sync"
)

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
	// StatePath is returned by AutoStatePath (test seeding for the watcher).
	StatePath string
	// quarantine is number->email, returned by ReadQuarantine.
	quarantine map[string]string
}

type fakeAccount struct {
	email    string
	org      string
	disabled bool
	usage    *Usage
	cred     []byte
}

// NewFake returns an empty Fake.
func NewFake() *Fake {
	return &Fake{
		accounts:   make(map[string]*fakeAccount),
		quarantine: make(map[string]string),
		AutoCode:   2, // no action by default
	}
}

func (f *Fake) log(format string, a ...any) {
	f.Calls = append(f.Calls, fmt.Sprintf(format, a...))
}

// Add inserts or updates an account.
func (f *Fake) Add(_ context.Context, req AddRequest) error {
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
func (f *Fake) List(_ context.Context) (*ListResult, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	res := &ListResult{SchemaVersion: 1}
	for i, e := range f.order {
		acc := f.accounts[e]
		num := i + 1
		row := AccountRow{
			Number:           num,
			Email:            acc.email,
			OrganizationName: acc.org,
			Active:           f.active == e,
			Disabled:         acc.disabled,
			UsageStatus:      "ok",
			Usage:            acc.usage,
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
func (f *Fake) Status(ctx context.Context) (*StatusResult, error) {
	list, err := f.List(ctx)
	if err != nil {
		return nil, err
	}
	res := &StatusResult{ActiveAccountNumber: list.ActiveAccountNumber}
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
func (f *Fake) SetUsage(email string, u *Usage) {
	f.mu.Lock()
	defer f.mu.Unlock()
	if acc, ok := f.accounts[email]; ok {
		acc.usage = u
	}
}

// CallLog returns a snapshot copy of the ordered method log (race-safe).
func (f *Fake) CallLog() []string {
	f.mu.Lock()
	defer f.mu.Unlock()
	return append([]string(nil), f.Calls...)
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
