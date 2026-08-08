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
	return &Fake{accounts: make(map[string]*fakeAccount)}
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

// --- test helpers -----------------------------------------------------------

// SetUsage attaches usage to an account (test seeding).
func (f *Fake) SetUsage(email string, u *Usage) {
	f.mu.Lock()
	defer f.mu.Unlock()
	if acc, ok := f.accounts[email]; ok {
		acc.usage = u
	}
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
