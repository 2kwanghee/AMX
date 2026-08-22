// Package claude implements provider.Driver for Claude Code accounts managed by
// the tsamx pool. It is the ONLY place that knows Claude's config-home layout
// (CLAUDE_CONFIG_DIR, .credentials.json, .claude.json / oauthAccount) and the
// claudeAiOauth fingerprint scheme; every other package sees it through the
// neutral provider.Driver interface.
package claude

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

// EnvBinary names the tsamx executable when it is not on PATH (a venv shim, a
// uv-managed install).
const EnvBinary = "AMX_TSAMX_BIN"

// Driver is the Claude implementation of provider.Driver.
type Driver struct{}

// New returns a Claude driver.
func New() *Driver { return &Driver{} }

var _ provider.Driver = (*Driver)(nil)

// Name identifies the vendor.
func (*Driver) Name() string { return "claude" }

// ConfigHome resolves the Claude config home from CLAUDE_CONFIG_DIR.
func (*Driver) ConfigHome() string { return os.Getenv("CLAUDE_CONFIG_DIR") }

// DefaultConfigHome mirrors the amx-claude wrapper's ${CLAUDE_CONFIG_DIR:-$HOME/.claude}
// fallback so the deliver lock lands on the same file the wrapper flocks.
func (*Driver) DefaultConfigHome() string {
	if home, err := os.UserHomeDir(); err == nil && home != "" {
		return filepath.Join(home, ".claude")
	}
	return ""
}

// CredentialPath returns the live active-account credential file in configDir.
func (*Driver) CredentialPath(configDir string) string {
	return filepath.Join(configDir, ".credentials.json")
}

// Env points tsamx at configDir as its Claude config home.
func (*Driver) Env(configDir string) []string {
	return []string{"CLAUDE_CONFIG_DIR=" + configDir}
}

// BinaryName is the tsamx pool CLI ($AMX_TSAMX_BIN, else "tsamx").
func (*Driver) BinaryName() string {
	if b := os.Getenv(EnvBinary); b != "" {
		return b
	}
	return "tsamx"
}

// claudeIdentity is the subset of Claude's global config that tsamx reads to
// name the account it is about to capture (`oauthAccount` in `.claude.json`).
type claudeIdentity struct {
	OAuthAccount struct {
		EmailAddress     string `json:"emailAddress"`
		AccountUUID      string `json:"accountUuid"`
		OrganizationUUID string `json:"organizationUuid"`
		OrganizationName string `json:"organizationName"`
	} `json:"oauthAccount"`
}

// StageCredential stages configDir for `tsamx add`, which captures whatever
// account the Claude config home currently holds — it takes no identifier — so
// this writes the credential set into `.credentials.json` and the identity into
// `.claude.json`. The credential travels by file, never as an argument or a log
// line (§7).
//
// AMS carries no organization UUID for an account, so every delivered account is
// staged as a personal one; tsamx keys slots on (email, organizationUuid) and an
// empty UUID is its personal-account value.
func (*Driver) StageCredential(configDir string, credentialJSON []byte, meta provider.AddMeta) error {
	if err := os.MkdirAll(configDir, 0o700); err != nil {
		return err
	}
	// Write both files atomically (temp in the same dir + rename). The runner
	// (Claude Code) reads these concurrently; a non-atomic os.WriteFile could be
	// observed half-written, so an in-flight runner request would read a torn
	// credential. os.Rename is atomic on the same filesystem.
	if err := writeFileAtomic(filepath.Join(configDir, ".credentials.json"), credentialJSON, 0o600); err != nil {
		return err
	}
	var identity claudeIdentity
	identity.OAuthAccount.EmailAddress = meta.Email
	identity.OAuthAccount.AccountUUID = meta.AccountUUID
	identity.OAuthAccount.OrganizationName = meta.OrganizationName

	// Merge into the existing .claude.json rather than replacing it: the runner
	// (Claude Code) keeps its own state there (machineID, firstStartTime, …), and
	// `hasCompletedOnboarding` + `theme` are load-bearing — claude shows the
	// onboarding/login screen when `!config.theme || !config.hasCompletedOnboarding`,
	// so a staged home without them demands a browser login even though the
	// credential file is complete (2026-08-10 실측; tsamx session.py seeds the
	// same two keys on its own capture path).
	configPath := filepath.Join(configDir, ".claude.json")
	config := map[string]json.RawMessage{}
	if raw, rerr := os.ReadFile(configPath); rerr == nil {
		// A corrupt file degrades to a fresh map — same failure mode as before.
		_ = json.Unmarshal(raw, &config)
	}
	oauthBlob, err := json.Marshal(identity.OAuthAccount)
	if err != nil {
		return err
	}
	config["oauthAccount"] = oauthBlob
	config["hasCompletedOnboarding"] = json.RawMessage("true")
	if _, ok := config["theme"]; !ok {
		config["theme"] = json.RawMessage(`"dark"`)
	}
	blob, err := json.Marshal(config)
	if err != nil {
		return err
	}
	return writeFileAtomic(configPath, blob, 0o600)
}

// Identity reads back the account email StageCredential wrote into
// configDir's `.claude.json` (`oauthAccount.emailAddress`). It never touches
// `.credentials.json` — the email lives only in the identity file, never in
// the credential itself — so this reads no token material at all. Returns an
// error when the file is absent/unreadable/unparseable, or when the email
// field is empty (a `.claude.json` with no oauthAccount at all, or one an
// account was Create()d for but never Stage()d, carries no identity to
// report); either way the caller (profile.Store.GetActive et al.) sees a
// non-nil error and must not assume a live email.
func (*Driver) Identity(configDir string) (string, error) {
	raw, err := os.ReadFile(filepath.Join(configDir, ".claude.json"))
	if err != nil {
		return "", err
	}
	var identity claudeIdentity
	if err := json.Unmarshal(raw, &identity); err != nil {
		return "", err
	}
	if identity.OAuthAccount.EmailAddress == "" {
		return "", fmt.Errorf("claude: %s has no recorded oauthAccount email", configDir)
	}
	return identity.OAuthAccount.EmailAddress, nil
}

// Fingerprint is the stable identity hash of a credential-set JSON, mirroring
// tsamx `oauth.credential_fingerprint` (tsamx/src/tsamx/oauth.py):
//
//   - "sha256:"      + hex(sha256(refreshToken)) when claudeAiOauth.refreshToken
//     exists — survives access-token rotation, so two generations of the SAME
//     OAuth lineage compare equal, while a refresh-token ROTATION (O9) changes it.
//   - "sha256-full:" + hex(sha256(whole set)) otherwise — API keys and setup
//     tokens never rotate, so content identity IS lineage identity.
//   - "" only for empty input (a caller asking "did it change?" must never get ""
//     for real bytes, or every comparison would degenerate to "changed").
//
// It is a one-way hash of an already-secret value; it is never the credential
// itself and is safe to keep in plaintext metadata. Do not log the refresh
// token; the returned digest is not the token (§7).
func (*Driver) Fingerprint(cred []byte) string {
	if len(cred) == 0 {
		return ""
	}
	var data struct {
		ClaudeAiOauth struct {
			RefreshToken string `json:"refreshToken"`
		} `json:"claudeAiOauth"`
	}
	if err := json.Unmarshal(cred, &data); err == nil && data.ClaudeAiOauth.RefreshToken != "" {
		sum := sha256.Sum256([]byte(data.ClaudeAiOauth.RefreshToken))
		return "sha256:" + hex.EncodeToString(sum[:])
	}
	sum := sha256.Sum256(cred)
	return "sha256-full:" + hex.EncodeToString(sum[:])
}

// HasCredentialMaterial reports whether a credential set still carries token
// material. It answers one question only: is this a logged-out shell — a set
// whose claudeAiOauth block carries the token keys but nothing in them?
//
// The re-sync caller drops a push when this is false, so the bias is toward
// true: an unparseable body (an api_key is an opaque string, credential_type is
// oauth|api_key), a non-object top level, a missing or non-object claudeAiOauth,
// a claudeAiOauth block holding NEITHER token key (an unknown schema inside the
// block is as unjudgeable as one outside it), or a token of an unexpected JSON
// type are all shapes this cannot judge and all return true. false requires that
// at least one of accessToken/refreshToken is present and that every present one
// is blank — plus the one non-JSON case that is certain: a blank body, which no
// api_key could ever be.
//
// Note the asymmetry with enroll (ams-server oauth_enroll), which requires a
// refresh_token: a `claude setup token` account legitimately carries a long-lived
// accessToken and no refreshToken (see Fingerprint above), and demanding one here
// would silently strand such an account's real rotations forever.
//
// Never logs the credential (§7).
func (*Driver) HasCredentialMaterial(cred []byte) bool {
	if isBlankCredential(string(cred)) {
		return false // whitespace/control bytes only: not even an opaque api_key
	}
	root, ok := jsonObject(cred)
	if !ok {
		return true // unparseable or not an object: cannot judge -> usable
	}
	raw, present := root["claudeAiOauth"]
	if !present {
		return true // unknown schema
	}
	oauth, ok := jsonObject(raw)
	if !ok {
		return true // claudeAiOauth is not an object: unknown schema
	}
	anyKey, material := tokenMaterial(oauth, "accessToken", "refreshToken")
	if !anyKey {
		return true // neither token key present: unknown schema inside the block
	}
	return material
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
// renames it into place, so a concurrent reader (the runner) never observes a
// partial write. The temp file is created 0o600 and the final file carries perm;
// on any failure before the rename the temp file is removed. Never logs data.
func writeFileAtomic(path string, data []byte, perm os.FileMode) error {
	dir := filepath.Dir(path)
	tmp, err := os.CreateTemp(dir, ".amx-*.tmp")
	if err != nil {
		return err
	}
	tmpName := tmp.Name()
	// Best-effort cleanup unless the rename below claims the temp file.
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
