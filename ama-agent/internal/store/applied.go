package store

import (
	"bufio"
	"encoding/json"
	"os"
	"path/filepath"
	"sync"
	"time"
)

// AppliedLogFileName is the plaintext sidecar's basename.
const AppliedLogFileName = "applied.log"

// appliedRingSize bounds the ring; also the cap reported to AMS in
// Register.applied_command_ids (SSOT §5.4 / proto Register field 10).
const appliedRingSize = 128

// AppliedEntry records that a command was processed and what it converged to.
// It carries NO credential material (§7).
type AppliedEntry struct {
	CommandID   string    `json:"commandId"`
	Kind        string    `json:"kind"`             // "deliver", "recall", ...
	Target      string    `json:"target,omitempty"` // amsAccountId, when account-scoped
	Desired     string    `json:"desired,omitempty"`
	Convergence string    `json:"convergence"`
	AppliedAt   time.Time `json:"appliedAt"`
}

// AppliedLog is a bounded, plaintext, JSON-lines ring of processed commands.
// It survives reboot (unlike the memory-only KEK), so idempotency after a crash
// still recognizes previously seen command ids.
type AppliedLog struct {
	mu      sync.Mutex
	path    string
	entries []AppliedEntry // oldest first; len <= appliedRingSize
}

// OpenAppliedLog loads (or initializes) dir/applied.log.
func OpenAppliedLog(dir string) (*AppliedLog, error) {
	if err := os.MkdirAll(dir, 0o700); err != nil {
		return nil, err
	}
	l := &AppliedLog{path: filepath.Join(dir, AppliedLogFileName)}
	if err := l.load(); err != nil {
		return nil, err
	}
	return l, nil
}

func (l *AppliedLog) load() error {
	f, err := os.Open(l.path)
	if err != nil {
		if os.IsNotExist(err) {
			return nil
		}
		return err
	}
	defer f.Close()
	sc := bufio.NewScanner(f)
	sc.Buffer(make([]byte, 0, 64*1024), 1<<20)
	for sc.Scan() {
		line := sc.Bytes()
		if len(line) == 0 {
			continue
		}
		var e AppliedEntry
		if err := json.Unmarshal(line, &e); err != nil {
			continue // skip a corrupt line rather than fail the whole log
		}
		l.entries = append(l.entries, e)
	}
	if len(l.entries) > appliedRingSize {
		l.entries = l.entries[len(l.entries)-appliedRingSize:]
	}
	return sc.Err()
}

// Append records entry, evicting the oldest beyond the ring size, and rewrites
// the file atomically.
func (l *AppliedLog) Append(entry AppliedEntry) error {
	if entry.AppliedAt.IsZero() {
		entry.AppliedAt = time.Now().UTC()
	}
	l.mu.Lock()
	defer l.mu.Unlock()
	l.entries = append(l.entries, entry)
	if len(l.entries) > appliedRingSize {
		l.entries = l.entries[len(l.entries)-appliedRingSize:]
	}
	return l.persistLocked()
}

func (l *AppliedLog) persistLocked() error {
	var buf []byte
	for _, e := range l.entries {
		b, err := json.Marshal(e)
		if err != nil {
			return err
		}
		buf = append(buf, b...)
		buf = append(buf, '\n')
	}
	return atomicWrite(l.path, buf, 0o600)
}

// Lookup returns the most recent entry for commandID, if present.
func (l *AppliedLog) Lookup(commandID string) (AppliedEntry, bool) {
	l.mu.Lock()
	defer l.mu.Unlock()
	for i := len(l.entries) - 1; i >= 0; i-- {
		if l.entries[i].CommandID == commandID {
			return l.entries[i], true
		}
	}
	return AppliedEntry{}, false
}

// RecentIDs returns up to appliedRingSize command ids, newest first, for
// Register.applied_command_ids. Duplicates are collapsed to their newest use.
func (l *AppliedLog) RecentIDs() []string {
	l.mu.Lock()
	defer l.mu.Unlock()
	seen := make(map[string]struct{}, len(l.entries))
	out := make([]string, 0, len(l.entries))
	for i := len(l.entries) - 1; i >= 0; i-- {
		id := l.entries[i].CommandID
		if _, ok := seen[id]; ok {
			continue
		}
		seen[id] = struct{}{}
		out = append(out, id)
		if len(out) >= appliedRingSize {
			break
		}
	}
	return out
}
