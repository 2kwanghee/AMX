package store

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
)

// CredentialFingerprint is the stable identity hash of a credential-set JSON,
// mirroring tsamx `oauth.credential_fingerprint` (tsamx/src/tsamx/oauth.py):
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
func CredentialFingerprint(cred []byte) string {
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
