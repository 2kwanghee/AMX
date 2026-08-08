package store

import "sync"

// KEKHolder keeps key-encryption keys in memory only (SSOT §6.2, open item O1).
// Keys arrive via SessionSetup and are lost on restart — that is what makes the
// manifest unreadable without a live AMS session. Never persist or log KEKs.
type KEKHolder struct {
	mu       sync.RWMutex
	keys     map[string][]byte
	activeID string
}

// NewKEKHolder returns an empty holder.
func NewKEKHolder() *KEKHolder {
	return &KEKHolder{keys: make(map[string][]byte)}
}

// Put installs (or replaces) the KEK for keyID. The bytes are copied.
func (h *KEKHolder) Put(keyID string, kek []byte) {
	cp := make([]byte, len(kek))
	copy(cp, kek)
	h.mu.Lock()
	defer h.mu.Unlock()
	h.keys[keyID] = cp
}

// Get returns a copy of the KEK for keyID.
func (h *KEKHolder) Get(keyID string) ([]byte, bool) {
	h.mu.RLock()
	defer h.mu.RUnlock()
	k, ok := h.keys[keyID]
	if !ok {
		return nil, false
	}
	cp := make([]byte, len(k))
	copy(cp, k)
	return cp, true
}

// SetActive names the key AMA uses when writing a new record. It must already
// be held.
func (h *KEKHolder) SetActive(keyID string) bool {
	h.mu.Lock()
	defer h.mu.Unlock()
	if _, ok := h.keys[keyID]; !ok {
		return false
	}
	h.activeID = keyID
	return true
}

// ActiveKey returns a copy of the active KEK and its id.
func (h *KEKHolder) ActiveKey() (kek []byte, keyID string, ok bool) {
	h.mu.RLock()
	defer h.mu.RUnlock()
	if h.activeID == "" {
		return nil, "", false
	}
	k, ok := h.keys[h.activeID]
	if !ok {
		return nil, "", false
	}
	cp := make([]byte, len(k))
	copy(cp, k)
	return cp, h.activeID, true
}

// Revoke drops the named keys from memory (completed rotation). Zeroes bytes.
func (h *KEKHolder) Revoke(ids ...string) {
	h.mu.Lock()
	defer h.mu.Unlock()
	for _, id := range ids {
		if k, ok := h.keys[id]; ok {
			for i := range k {
				k[i] = 0
			}
			delete(h.keys, id)
		}
		if h.activeID == id {
			h.activeID = ""
		}
	}
}

// HasKeys reports whether any KEK is held (i.e. a SessionSetup has been applied
// since the last restart).
func (h *KEKHolder) HasKeys() bool {
	h.mu.RLock()
	defer h.mu.RUnlock()
	return len(h.keys) > 0
}
