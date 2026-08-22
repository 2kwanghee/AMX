// Package codex implements provider.Driver and provider.Bridge for OpenAI Codex
// CLI accounts. It is the ONLY place that knows Codex's config-home layout
// (CODEX_HOME, auth.json, the token schema) and the refresh-token fingerprint
// scheme; every other package sees it through the neutral provider interfaces.
//
// Codex has no management CLI: the account lives as a single auth.json the CLI
// rotates in place. The bridge is therefore a file-manipulation surface (see
// bridge.go), and the driver owns only credential staging and identity hashing.
// Implementations MUST NOT log credential material (§7).
package codex

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"unicode"

	"github.com/2kwanghee/AMX/ama-agent/internal/provider"
)

// EnvConfigHome overrides the Codex config home (the directory holding auth.json).
// Empty is allowed here; whether an empty home gates staging/resync is the
// wiring layer's concern (PR3), not the driver's.
const EnvConfigHome = "AMX_CODEX_HOME"

// EnvBinary names the Codex executable when it is not on PATH.
const EnvBinary = "AMX_CODEX_BIN"

// credentialFile is the single file Codex stores its OAuth token set in.
const credentialFile = "auth.json"

// Driver is the Codex implementation of provider.Driver.
type Driver struct{}

// New returns a Codex driver.
func New() *Driver { return &Driver{} }

var _ provider.Driver = (*Driver)(nil)

// Name identifies the vendor.
func (*Driver) Name() string { return "codex" }

// ConfigHome resolves the Codex config home from AMX_CODEX_HOME (empty when unset;
// the wiring gate that turns an unset home into "codex disabled" is PR3, not here).
func (*Driver) ConfigHome() string { return os.Getenv(EnvConfigHome) }

// DefaultConfigHome mirrors the Codex CLI's ${CODEX_HOME:-$HOME/.codex} fallback so
// the deliver lock lands on the same file the runner wrapper flocks.
func (*Driver) DefaultConfigHome() string {
	if home, err := os.UserHomeDir(); err == nil && home != "" {
		return filepath.Join(home, ".codex")
	}
	return ""
}

// CredentialPath returns the live credential file in configDir — the file resync
// watches for a local rotation (the Codex CLI refreshes tokens in place here).
func (*Driver) CredentialPath(configDir string) string {
	return filepath.Join(configDir, credentialFile)
}

// Env points the Codex CLI at configDir as its config home.
func (*Driver) Env(configDir string) []string {
	return []string{"CODEX_HOME=" + configDir}
}

// BinaryName is the Codex CLI executable ($AMX_CODEX_BIN, else "codex").
func (*Driver) BinaryName() string {
	if b := os.Getenv(EnvBinary); b != "" {
		return b
	}
	return "codex"
}

// StageCredential writes the credential set (the full auth.json body) into
// configDir atomically. Codex reads no separate identity or onboarding file, so
// there is nothing else to seed — meta is captured by the bridge into its own
// sidecar (bridge.go), not into any file the Codex CLI reads. The credential
// travels by file, never as an argument or a log line (§7).
func (*Driver) StageCredential(configDir string, credentialJSON []byte, _ provider.AddMeta) error {
	if err := os.MkdirAll(configDir, 0o700); err != nil {
		return err
	}
	// Atomic write (temp in the same dir + rename): the Codex CLI may read
	// auth.json concurrently, so a non-atomic os.WriteFile could be observed
	// half-written. os.Rename is atomic on the same filesystem.
	return writeFileAtomic(filepath.Join(configDir, credentialFile), credentialJSON, 0o600)
}

// Identity reads back the account email from the bridge's identity sidecar
// (metaFile, written by Bridge.Add — see bridge.go) inside configDir.
// auth.json itself carries no email (see bridge.go's metaFile doc), so this
// never touches the credential file. Returns an error when the sidecar is
// absent/unreadable/unparseable, or when it carries no email (e.g. Create()d
// but never Add()ed through the bridge).
func (*Driver) Identity(configDir string) (string, error) {
	raw, err := os.ReadFile(filepath.Join(configDir, metaFile))
	if err != nil {
		return "", err
	}
	var meta codexMeta
	if err := json.Unmarshal(raw, &meta); err != nil {
		return "", err
	}
	if meta.Email == "" {
		return "", fmt.Errorf("codex: %s has no recorded identity email", configDir)
	}
	return meta.Email, nil
}

// Fingerprint is the stable identity hash of an auth.json body:
//
//   - "sha256:"      + hex(sha256(tokens.refresh_token)) when it exists — survives
//     access/id-token rotation, so two generations of the SAME OAuth lineage
//     compare equal while a refresh-token rotation changes it.
//   - "sha256-full:" + hex(sha256(whole body)) otherwise (e.g. an API-key-only
//     auth.json with no tokens) — content identity IS lineage identity there.
//   - "" only for empty input.
//
// It is a one-way hash of an already-secret value; it is never the credential
// itself and is safe to keep in plaintext metadata (§7).
func (*Driver) Fingerprint(cred []byte) string {
	if len(cred) == 0 {
		return ""
	}
	var data struct {
		Tokens struct {
			RefreshToken string `json:"refresh_token"`
		} `json:"tokens"`
	}
	if err := json.Unmarshal(cred, &data); err == nil && data.Tokens.RefreshToken != "" {
		sum := sha256.Sum256([]byte(data.Tokens.RefreshToken))
		return "sha256:" + hex.EncodeToString(sum[:])
	}
	sum := sha256.Sum256(cred)
	return "sha256-full:" + hex.EncodeToString(sum[:])
}

// HasCredentialMaterial reports whether an auth.json body still carries token
// material. It answers one question only: is this a logged-out shell — a tokens
// block carrying the token keys but nothing in them, and no API key either?
//
// The re-sync caller drops a push when this is false, so the bias is toward true:
// an unparseable body, a non-object top level, a missing or non-object tokens
// block, a tokens block holding NEITHER token key (an unknown schema inside the
// block is as unjudgeable as one outside it), or a token of an unexpected JSON
// type are all shapes this cannot judge and all return true. false requires that
// at least one of refresh_token/access_token is present, that every present one
// is blank, AND that no non-blank top-level OPENAI_API_KEY stands in for them —
// plus the one non-JSON case that is certain: a blank body.
//
// Never logs the credential (§7).
func (*Driver) HasCredentialMaterial(cred []byte) bool {
	if isBlankCredential(string(cred)) {
		return false // whitespace/control bytes only: not even an opaque api key
	}
	root, ok := jsonObject(cred)
	if !ok {
		return true // unparseable or not an object: cannot judge -> usable
	}
	raw, present := root["tokens"]
	if !present {
		return true // unknown schema
	}
	tokens, ok := jsonObject(raw)
	if !ok {
		return true // tokens is not an object (e.g. null on an api-key auth.json)
	}
	anyKey, material := tokenMaterial(tokens, "refresh_token", "access_token")
	if !anyKey {
		return true // neither token key present: unknown schema inside the block
	}
	if material {
		return true
	}
	// An emptied tokens block is still usable when the api-key form is present.
	_, apiKey := tokenMaterial(root, "OPENAI_API_KEY")
	return apiKey
}

// isBlankCredential reports whether s carries no credential information at all:
// every rune is whitespace or a control character (Unicode Cc).
//
// Deliberately NOT strings.TrimSpace: Python's str.strip() counts U+001C–U+001F
// as whitespace and Go's unicode.IsSpace does not, so a token of only those bytes
// would pass here and be refused by the AMS-side mirror — AMA would advance its
// baseline and stop retrying while AMS kept the stale copy. The definition
// (space OR Cc) is a parity contract with ams-server's _is_blank; change neither
// side alone.
func isBlankCredential(s string) bool {
	for _, r := range s {
		if !unicode.IsSpace(r) && !unicode.IsControl(r) {
			return false
		}
	}
	return true
}

// jsonObject decodes b as a JSON object, reporting false when it is not valid
// JSON or not an object (including null).
func jsonObject(b []byte) (map[string]json.RawMessage, bool) {
	var obj map[string]json.RawMessage
	if err := json.Unmarshal(b, &obj); err != nil || obj == nil {
		return nil, false
	}
	return obj, true
}

// tokenMaterial inspects keys in obj and reports whether any of them is present
// at all, and whether any present one carries material. A JSON null reads as
// blank (present, no material); a value that is not a string at all is a shape
// this cannot judge, so it counts as material (conservative: keep the
// credential).
func tokenMaterial(obj map[string]json.RawMessage, keys ...string) (present, material bool) {
	for _, k := range keys {
		raw, ok := obj[k]
		if !ok {
			continue
		}
		present = true
		var s string
		if err := json.Unmarshal(raw, &s); err != nil {
			material = true // not a string: unjudgeable -> treat as material
			continue
		}
		if !isBlankCredential(s) {
			material = true
		}
	}
	return present, material
}

// writeFileAtomic writes data to a temp file in the same directory as path and
// renames it into place, so a concurrent reader never observes a partial write.
// The temp file is created 0o600 and the final file carries perm; on any failure
// before the rename the temp file is removed. Never logs data.
func writeFileAtomic(path string, data []byte, perm os.FileMode) error {
	dir := filepath.Dir(path)
	tmp, err := os.CreateTemp(dir, ".amx-*.tmp")
	if err != nil {
		return err
	}
	tmpName := tmp.Name()
	defer func() {
		if tmpName != "" {
			_ = os.Remove(tmpName)
		}
	}()
	if err := tmp.Chmod(perm); err != nil {
		_ = tmp.Close()
		return err
	}
	if _, err := tmp.Write(data); err != nil {
		_ = tmp.Close()
		return err
	}
	if err := tmp.Sync(); err != nil {
		_ = tmp.Close()
		return err
	}
	if err := tmp.Close(); err != nil {
		return err
	}
	if err := os.Rename(tmpName, path); err != nil {
		return err
	}
	tmpName = "" // renamed into place; skip cleanup
	return nil
}
