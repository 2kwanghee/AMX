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

// StatusTokenExpired and StatusReloginRequired are the tsamx `usageStatus`
// literals (json_output.usage_fields) a caller reports from
// IdleTokenStatus.Status. They are NOT interchangeable and this package's
// FIRST version conflated them (P4 review, C1) — see JudgeIdleExpiry's doc
// for the full distinction; in one sentence: StatusTokenExpired is routine
// and transient (an idle profile's access token WILL expire every few
// hours; a refresh token can fix it), StatusReloginRequired is the literal
// internal/reporter's unexported usageStatusQuarantined constant
// (ama-agent/internal/reporter/reporter.go) matches to count an account as
// quarantined in PoolSummary — reporting it for a merely-expired-but-
// refreshable token would count every idle profile in the pool as
// quarantined. Duplicated here rather than imported, because this package
// must not create a dependency on internal/reporter (P4 stays
// provider/policy-only and inert; reporter is wired, this is not) —
// internal/reporter/usage_status_test.go asserts these literals stay in
// sync from the reporter side (P4 review, M3).
const (
	StatusTokenExpired    = "token_expired"
	StatusReloginRequired = "relogin_required"
)

// claudeOAuthCredential is the subset of a Claude credential file's
// claudeAiOauth block this package reads, ported from the field names
// tsamx.oauth.extract_oauth_data/is_oauth_token_expired and
// internal/provider/claude.Driver.Fingerprint/HasCredentialMaterial all
// agree on (accessToken, refreshToken, expiresAt).
type claudeOAuthCredential struct {
	ClaudeAiOauth struct {
		AccessToken  string   `json:"accessToken"`
		RefreshToken string   `json:"refreshToken"`
		ExpiresAt    *float64 `json:"expiresAt"`
	} `json:"claudeAiOauth"`
}

// CredentialExpiry is the judgement-relevant shape extracted from a raw
// Claude credential file: never the token material itself. See
// ExtractOAuthTokens for the sibling function that DOES return the token
// strings, for the one caller (a future P5 Fetch call) that legitimately
// needs them.
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
// returns, or logs cred's token bytes — use ExtractOAuthTokens when the
// token material itself is actually needed. A cred that is not valid JSON,
// or whose top level is not an object, returns a zero CredentialExpiry and a
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

// ExtractOAuthTokens returns the access token, refresh token, and parsed
// CredentialExpiry from a raw Claude credential file — the (accessToken,
// refreshToken, expiresAt) triple tsamx.oauth.extract_oauth_data reads for
// every OAuth operation (refresh, usage fetch). This is the ONLY function in
// this package that returns token material: Collector.Fetch's accessToken
// parameter is meant to come from here, not from a second, ad hoc
// credential-JSON parse a caller writes itself (P4 review, C2 — the first
// version of this package left P5 with no supported way to get a token INTO
// Fetch at all).
//
// accessToken/refreshToken are "" when absent or when cred is unparseable —
// this function never errors on a merely-empty/missing token (mirrors
// tsamx.oauth.extract_oauth_data returning None for a missing
// claudeAiOauth block rather than raising); err is non-nil only when cred is
// not valid JSON at all.
//
// Callers MUST NOT log the returned tokens or fold them into an error value
// — see TestExtractOAuthTokens_NeverLeaksIntoErrors, which is the only
// enforcement this package can offer; nothing about the Go type system stops
// a caller from misusing a plain string.
func ExtractOAuthTokens(cred []byte) (accessToken, refreshToken string, expiry CredentialExpiry, err error) {
	var parsed claudeOAuthCredential
	if err := json.Unmarshal(cred, &parsed); err != nil {
		return "", "", CredentialExpiry{}, err
	}
	expiry, _ = ParseCredentialExpiry(cred) // cred already proven valid JSON above
	return parsed.ClaudeAiOauth.AccessToken, parsed.ClaudeAiOauth.RefreshToken, expiry, nil
}

// IdleTokenStatus is P4's local (refresh-free) judgement of a profile's
// stored Claude credential — see JudgeIdleExpiry.
type IdleTokenStatus struct {
	// Judgeable is false when cred could not be parsed, or parsed but
	// carried no expiresAt claim at all. Meaningless Status when false.
	Judgeable bool
	// Status is "" (the token is not, as of now, expired — nothing to
	// report), StatusTokenExpired, or StatusReloginRequired. See
	// JudgeIdleExpiry for exactly which inputs produce which value.
	Status string
}

// JudgeIdleExpiry decides a profile's LOCALLY-DECIDABLE token status, for a
// profile Claude Code is NOT currently running against — the design note's
// P4 scope: "Claude Code가 돌지 않는 프로파일은 토큰 refresh가 일어나지
// 않으므로 만료 전 엔진이 갱신하거나 만료를 relogin_required로 보고한다."
//
// # What this function does and does not decide (P4 review, C1)
//
// tsamx distinguishes two states an idle/inactive account's stored
// credential can be in, and they are NOT interchangeable:
//
//   - USAGE_TOKEN_EXPIRED ("token_expired"): the access token is expired,
//     but a refresh token is present. tsamx's own comment calls this
//     exactly what it is — "token expired — refresh deferred this pass;
//     retries automatically" (switcher.py:164/3719) — a ROUTINE, TRANSIENT
//     state every idle profile passes through every few hours. tsamx clears
//     it by refreshing (oauth.py's try_refresh_oauth_credentials, invoked
//     from try_fetch_usage_for_account and the locked active-credential
//     refresh path, switcher.py:3269-3251-ish and 3269).
//   - USAGE_RELOGIN_REQUIRED ("relogin_required"): surfaced when
//     UsageEntry.token_dead() is true — the refresh-token lineage has
//     answered `invalid_grant` AUTH_DEAD_STRIKES (1) times
//     (usage_store.py:337), OR (switcher.py:3260-3280, the active-credential
//     refresh path) the credential carries no refresh token at all, which
//     that path treats identically to a dead lineage ("Permanently
//     unrefreshable: dead lineage or a credential with no refresh token at
//     all"). This is the literal internal/reporter's PoolSummary quarantine
//     count keys on (see StatusReloginRequired's doc) — reporting it wrongly
//     quarantines a healthy account.
//
// P4 does not implement token refresh (out of scope, unchanged from the
// first version — see this package's doc and C2's note on ExtractOAuthTokens).
// Without ever attempting a refresh, this function CANNOT observe an
// `invalid_grant` answer, so it can never prove a refresh-token lineage is
// dead the way tsamx's token_dead() does. What it CAN decide locally, from
// the credential bytes alone, without any network call:
//
//   - no expiresAt claim at all -> Judgeable=false (unknown; never reported —
//     an account with unmeasurable expiry must not be quarantined on that
//     basis alone, matching design note P4's "status must not age into
//     unavailable" for an account Anthropic might still grant/reset quota
//     on);
//   - expiresAt present, NOT (yet) expired -> Status="" regardless of
//     refresh-token presence. A `claude setup token` account legitimately
//     carries a long-lived accessToken and NO refreshToken (see
//     internal/provider/claude.Driver.Fingerprint's doc) and is perfectly
//     healthy right up until its own expiry — flagging it merely for
//     lacking a refresh token, before it is actually expired, would be a
//     false positive this function must not produce;
//   - expiresAt present AND expired AND a refresh token IS present ->
//     StatusTokenExpired (transient — matches USAGE_TOKEN_EXPIRED; a caller
//     wiring in a real refresh, per C2, should retry the refresh and only
//     escalate to StatusReloginRequired if THAT attempt fails with
//     invalid_grant/no_refresh_token, exactly as tsamx does);
//   - expiresAt present AND expired AND NO refresh token is present ->
//     StatusReloginRequired. Nothing local or remote can recover this
//     credential — there is no refresh grant to even attempt — which is the
//     one case among the two USAGE_RELOGIN_REQUIRED triggers above this
//     function CAN decide without a network call.
func JudgeIdleExpiry(cred []byte, now time.Time) IdleTokenStatus {
	exp, err := ParseCredentialExpiry(cred)
	if err != nil || exp.ExpiresAtMS == nil {
		return IdleTokenStatus{Judgeable: false}
	}
	if !exp.IsExpired(now) {
		return IdleTokenStatus{Judgeable: true, Status: ""}
	}
	if exp.HasRefreshToken {
		return IdleTokenStatus{Judgeable: true, Status: StatusTokenExpired}
	}
	return IdleTokenStatus{Judgeable: true, Status: StatusReloginRequired}
}
