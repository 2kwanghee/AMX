package usage

import (
	"encoding/json"
	"time"
)

// ExpiryBufferMS mirrors tsamx.oauth.OAUTH_EXPIRY_BUFFER_MS: a token within
// this many milliseconds of its stated expiry counts as expired. A profile
// this package judges is, by construction, one Claude Code is NOT currently
// running against (see JudgeIdleExpiry's doc), so there is no in-process
// refresh race to buffer against here the way tsamx's live runner needs to —
// the buffer is kept anyway, unchanged, so a caller that later starts a
// runner against a profile this function just called fresh does not hand it
// a token that expires seconds into that run.
const ExpiryBufferMS int64 = 5 * 60 * 1000

// StatusReloginRequired is the tsamx `usageStatus` literal a caller reports
// when JudgeIdleExpiry finds an expired credential. It is the exact string
// internal/reporter's unexported usageStatusQuarantined constant matches
// (ama-agent/internal/reporter/reporter.go) to count an account as
// quarantined in PoolSummary — duplicated here rather than imported, because
// this package must not create a dependency on internal/reporter (P4 stays
// provider/policy-only and inert; reporter is wired, this is not). Whoever
// wires this package's judgement into that reporting path (P5) must keep
// this string in sync with reporter.go's usageStatusQuarantined by hand.
const StatusReloginRequired = "relogin_required"

// claudeOAuthCredential is the subset of a Claude credential file's
// claudeAiOauth block this package reads, ported from the field names
// tsamx.oauth.extract_oauth_data/is_oauth_token_expired and
// internal/provider/claude.Driver.Fingerprint/HasCredentialMaterial all
// agree on (accessToken, refreshToken, expiresAt). AccessToken is decoded
// only to confirm the block is well-formed OAuth material — it is NEVER
// returned by any exported function in this file, logged, or included in an
// error.
type claudeOAuthCredential struct {
	ClaudeAiOauth struct {
		AccessToken  string   `json:"accessToken"`
		RefreshToken string   `json:"refreshToken"`
		ExpiresAt    *float64 `json:"expiresAt"`
	} `json:"claudeAiOauth"`
}

// CredentialExpiry is the judgement-relevant shape extracted from a raw
// Claude credential file: never the token material itself.
type CredentialExpiry struct {
	HasAccessToken  bool
	HasRefreshToken bool
	// ExpiresAtMS is the epoch-millisecond expiry the credential reports, or
	// nil when absent or not a number (mirrors tsamx's
	// `isinstance(expires_at, (int, float))` guard in is_oauth_token_expired).
	ExpiresAtMS *int64
}

// ParseCredentialExpiry extracts CredentialExpiry from a raw Claude
// credential-file JSON (the same `.credentials.json` bytes
// internal/provider/claude.Driver.CredentialPath names). It never retains,
// returns, or logs cred's token bytes. A cred that is not valid JSON, or
// whose top level is not an object, returns a zero CredentialExpiry and a
// non-nil error; a well-formed object missing the claudeAiOauth block, or
// missing/non-numeric expiresAt within it, returns (zero-ish
// CredentialExpiry, nil) — an absent/unparseable expiry is a legitimate,
// non-error shape (mirrors tsamx: is_oauth_token_expired's
// `if not isinstance(expires_at, (int, float)): return False` branch, not an
// exception).
func ParseCredentialExpiry(cred []byte) (CredentialExpiry, error) {
	var parsed claudeOAuthCredential
	if err := json.Unmarshal(cred, &parsed); err != nil {
		return CredentialExpiry{}, err
	}
	out := CredentialExpiry{
		HasAccessToken:  parsed.ClaudeAiOauth.AccessToken != "",
		HasRefreshToken: parsed.ClaudeAiOauth.RefreshToken != "",
	}
	if parsed.ClaudeAiOauth.ExpiresAt != nil {
		ms := int64(*parsed.ClaudeAiOauth.ExpiresAt)
		out.ExpiresAtMS = &ms
	}
	return out, nil
}

// IsExpired reports whether the credential is expired, or within
// ExpiryBufferMS of expiring, as of now. Mirrors
// tsamx.oauth.is_oauth_token_expired: an unknown expiry (ExpiresAtMS == nil)
// is NOT expired by this check alone — an account with no observable expiry
// claim is not reported as needing relogin on that basis.
func (c CredentialExpiry) IsExpired(now time.Time) bool {
	if c.ExpiresAtMS == nil {
		return false
	}
	return now.UnixMilli()+ExpiryBufferMS >= *c.ExpiresAtMS
}

// IdleExpiryStatus is P4's relogin-required judgement for a profile whose
// Claude Code process is not currently running against it — see
// JudgeIdleExpiry.
type IdleExpiryStatus struct {
	// Expired is true only when Judgeable is true and the parsed expiry has
	// passed (or is within ExpiryBufferMS). Meaningless when Judgeable is
	// false.
	Expired bool
	// Judgeable is false when cred could not be parsed, or parsed but
	// carried no expiresAt claim at all — the two cases JudgeIdleExpiry
	// cannot tell apart from "genuinely not expired" and so does not report
	// as relogin_required (a false positive there would quarantine a
	// healthy, merely-never-measured account; see design note P4: "Anthropic
	// can grant/reset quota... status must not age into unavailable").
	Judgeable bool
}

// JudgeIdleExpiry decides whether a profile's stored Claude credential has
// expired, for a profile Claude Code is NOT currently running against — the
// design note's P4 scope: "Claude Code가 돌지 않는 프로파일은 토큰 refresh가
// 일어나지 않으므로 만료 전 엔진이 갱신하거나 만료를 relogin_required로
// 보고한다." This function implements ONLY the second half (detect and
// report); it never contacts the token endpoint and never writes a refreshed
// credential anywhere — token refresh itself is out of scope for P4 (see the
// design note's P4 section and this package's doc comment).
//
// A caller with Expired && Judgeable true should report the profile's
// usageStatus as StatusReloginRequired, matching the literal
// internal/reporter's PoolSummary quarantine count already keys on.
func JudgeIdleExpiry(cred []byte, now time.Time) IdleExpiryStatus {
	exp, err := ParseCredentialExpiry(cred)
	if err != nil || exp.ExpiresAtMS == nil {
		return IdleExpiryStatus{Expired: false, Judgeable: false}
	}
	return IdleExpiryStatus{Expired: exp.IsExpired(now), Judgeable: true}
}
