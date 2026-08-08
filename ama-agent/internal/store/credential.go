package store

import (
	"os"
	"path/filepath"
	"strings"
	"sync"
)

// ServerCredentialFileName is the plaintext sidecar holding the long-lived
// server credential exchanged during enrollment.
const ServerCredentialFileName = "server_credential"

// CredentialSidecar persists the long-lived server credential to a plaintext
// file alongside applied.log. Unlike the KEK (memory-only, §6.2), this value is
// NOT a decryption secret: it only authenticates the agent to AMS. It MUST
// survive restart, because AMS burns the one-shot enroll_token the moment it
// mints this credential — an agent that lost it after a restart would be locked
// out permanently (path B re-auth is the only way back in). It is written 0600
// but is not encrypted (KEK is unavailable at the point it must be read, on the
// very first Register after a cold start).
type CredentialSidecar struct {
	mu   sync.Mutex
	path string
}

// OpenCredentialSidecar returns the sidecar at dir/server_credential, creating
// dir if needed.
func OpenCredentialSidecar(dir string) (*CredentialSidecar, error) {
	if err := os.MkdirAll(dir, 0o700); err != nil {
		return nil, err
	}
	return &CredentialSidecar{path: filepath.Join(dir, ServerCredentialFileName)}, nil
}

// Load reads the persisted credential, returning "" if none has been written.
func (c *CredentialSidecar) Load() (string, error) {
	c.mu.Lock()
	defer c.mu.Unlock()
	b, err := os.ReadFile(c.path)
	if err != nil {
		if os.IsNotExist(err) {
			return "", nil
		}
		return "", err
	}
	return strings.TrimSpace(string(b)), nil
}

// Save persists credential atomically with 0600 perms. Never logged (§7).
func (c *CredentialSidecar) Save(credential string) error {
	c.mu.Lock()
	defer c.mu.Unlock()
	return atomicWrite(c.path, []byte(credential), 0o600)
}
