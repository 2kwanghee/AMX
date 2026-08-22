package usage

// This file ports tsamx's OAuth refresh-token grant
// (tsamx/src/tsamx/oauth.py:99 try_refresh_oauth_credentials, :193
// refresh_oauth_credentials, :18-19 OAUTH_TOKEN_URL/OAUTH_CLIENT_ID, :81
// RefreshOutcome, :164 _parse_token_account) to Go — the P5 선결 조건 #1
// (design note docs/design-notes/seat-engine-plan.md, P5: "OAuth 토큰 갱신
// 이식(선결)"). It is used by whatever future P5 scheduler needs a live
// access token for an idle profile; this file itself wires nothing into
// cmd/ama or the existing tsamx bridge and constructs no scheduler.
//
// Result classification mirrors the Python source EXACTLY (verified against
// oauth.py's function body, not its docstring, per code-truth-verification):
//   - success: the token endpoint returns 2xx with a well-formed body ->
//     Credentials holds the rotated credential-set JSON.
//   - "invalid_grant": the endpoint answered 400/401/403 AND the response
//     body contains the literal substring "invalid_grant" or "invalid_client"
//     (oauth.py:154-157). This refresh-token lineage is permanently dead —
//     the ONLY outcome that should ever promote a caller toward
//     relogin_required (see ClassifyRefreshFailure).
//   - "no_refresh_token": the input credential PARSES CLEANLY (a JSON object,
//     with a claudeAiOauth object when present) but genuinely carries no
//     non-empty refreshToken — e.g. a `claude setup token` account, which
//     legitimately has none (oauth.py:110-114). Equally permanent for retry
//     purposes as invalid_grant (mirrors tsamx's PERMANENT_AUTH_ERRORS).
//   - "malformed_credential": the input could NOT be read as a credential at
//     all — invalid JSON, a non-object top level, a claudeAiOauth key present
//     but not itself an object. This is a DELIBERATE DIVERGENCE from
//     oauth.py, which maps this same shape to "no_refresh_token" (verified:
//     try_refresh_oauth_credentials's `except json.JSONDecodeError` branch
//     returns the literal RefreshOutcome(None, "no_refresh_token")) — folding
//     the two together is exactly what let a merely CORRUPTED file on disk
//     (a torn write, a truncated copy) permanently quarantine an otherwise
//     healthy account the same way a verified-absent refresh token does
//     (adversarial review A5, reproduced with truncated/empty/JSON-array/
//     JSON-null inputs). ClassifyRefreshFailure treats this the same as
//     "transient" (retry later, never relogin_required) precisely to close
//     that gap.
//   - "transient": anything else — network error, timeout, 5xx, an ambiguous
//     4xx without the invalid_grant/invalid_client marker, a malformed 2xx
//     body (oauth.py:158-161's bare `except Exception`), or an expires_in
//     value outside maxExpiresInS's sane range (A3). Retry later; the token
//     may still be valid.
//
// PRECONDITION (A5, documented — not enforced by this file): tsamx calls
// try_refresh_oauth_credentials only AFTER its own near-expiry check has
// already passed (autoswitch.py:778's `near_expiry` gate,
// switcher.py:2969's `oauth.is_oauth_token_expired` gate) — never
// unconditionally. A caller wiring TryRefresh into a scheduler must apply
// the same gate itself (this package's own expiry.go —
// CredentialExpiry.IsExpired / JudgeIdleExpiry — is the equivalent local
// check) rather than calling TryRefresh on every tick regardless of expiry;
// refreshing a token that is nowhere near expiring burns a refresh-token
// generation for no reason and invites exactly the kind of needless traffic
// poll_policy.go's budget notes warn about elsewhere in this package.
//
// REDIRECT POLICY (A1): every *http.Client this file constructs refuses to
// follow HTTP redirects at all (see noFollowRedirects) — see that function's
// doc for why.
import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"strings"
	"time"

	"github.com/2kwanghee/AMX/ama-agent/internal/provider"
	"github.com/2kwanghee/AMX/ama-agent/internal/seat/profile"
)

// oauthTokenURL and oauthClientID are ported byte-for-byte from
// tsamx.oauth.OAUTH_TOKEN_URL / OAUTH_CLIENT_ID (oauth.py:18-19). Production
// code (NewRefresher) can never be pointed anywhere else; only the unexported
// newRefresherForTest constructor accepts a different URL, and it is used by
// refresh_test.go only.
const (
	oauthTokenURL = "https://platform.claude.com/v1/oauth/token"
	oauthClientID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
)

// defaultRefreshTimeout bounds a Refresher.TryRefresh call when the caller
// passes timeout <= 0. tsamx's urllib call defaults to 10.0s
// (try_refresh_oauth_credentials's timeout_s parameter) — matched here
// exactly, not widened the way Collector.Fetch's defaultTimeout was (that
// widening was to cover http.Client's redirect/drain overhead beyond
// urllib's connect-only timeout; a token POST has no redirects to chase).
const defaultRefreshTimeout = 10 * time.Second

// maxExpiresInS bounds a token endpoint's advertised expires_in (seconds) to
// a sane upper limit — see the A3 comment at TryRefresh's expires_in check
// for why this exists. 10 years is comfortably above any real access-token
// lifetime while still catching the overflow failure mode.
const maxExpiresInS = 10 * 365 * 24 * 3600 // seconds

// TokenAccount is the optional account identity the refresh grant's response
// may carry alongside a successful rotation, ported from
// tsamx.oauth._parse_token_account (oauth.py:164-190). Nil whenever the
// server omitted it or the refresh failed — callers must treat it as
// opportunistic, exactly as the Python source's docstring states (verified
// against the function body: every field degrades to zero-value rather than
// erroring on a malformed shape).
type TokenAccount struct {
	UUID             string
	Email            string
	OrganizationUUID string
}

// RefreshOutcome is the result of one refresh-token grant attempt, mirroring
// tsamx.oauth.RefreshOutcome (oauth.py:71-96) field-for-field.
type RefreshOutcome struct {
	// Credentials is the full rotated credential-set JSON on success, nil
	// otherwise. MUST NEVER be logged (it carries live token material).
	Credentials []byte
	// Error classifies failure: "" (success), "invalid_grant",
	// "no_refresh_token", or "transient". See this file's package doc.
	Error string
	// TokenAccount is the opportunistic identity the token endpoint may have
	// included; nil when absent or on failure.
	TokenAccount *TokenAccount
}

// Refresher exchanges a stored refresh token for a new access token via
// Claude's OAuth token endpoint. The zero value is not usable; construct with
// NewRefresher. Safe for concurrent use (http.Client is).
type Refresher struct {
	client   *http.Client
	tokenURL string
}

// noFollowRedirects is the CheckRedirect policy every http.Client this
// package constructs (both Refresher and Collector) uses: refuse every
// redirect outright, returning the 3xx response itself rather than
// following it (http.ErrUseLastResponse; see the net/http docs for
// Client.CheckRedirect).
//
// Adversarial review A1, reproduced empirically: Go's default CheckRedirect
// (nil) follows up to 10 redirects, and per RFC 7231 a 307/308 response
// preserves the original request's method AND BODY — so an attacker who can
// make the token endpoint (or anything on its path, e.g. a compromised
// intermediate proxy) answer with a 307 to a host they control would receive
// this package's POST body VERBATIM, which for Refresher.TryRefresh IS the
// refresh_token. A prior version of this file's package doc claimed "a token
// POST has no redirects to chase" — that was an unverified assumption, not a
// checked fact, and the experiment above disproves it. Refusing every
// redirect closes the whole class rather than trying to distinguish a
// same-host redirect (plausibly safe) from a cross-host one (attacker
// territory): distinguishing them correctly is exactly the kind of judgment
// call this package must never need to get right under adversarial input.
//
// Collector.Fetch carries the same policy even though its request is a GET
// with the access token only in the Authorization header (never the URL or
// body) — Go's stdlib already strips Authorization on a cross-host redirect,
// but relying on that stdlib behavior staying true across Go versions is a
// weaker guarantee than simply never following a redirect at all, and
// keeping both clients on one shared policy removes any need to reason about
// the two differently.
func noFollowRedirects(*http.Request, []*http.Request) error {
	return http.ErrUseLastResponse
}

// NewRefresher returns a Refresher targeting the real Claude token endpoint,
// using the default transport (proxy-/TLS-config-aware) with no client-level
// timeout — every call bounds itself via the context TryRefresh derives
// internally from its timeout parameter, so a slow DNS/connect phase cannot
// outlive that bound even though the client itself carries none.
func NewRefresher() *Refresher {
	return &Refresher{
		client:   &http.Client{Transport: http.DefaultTransport, CheckRedirect: noFollowRedirects},
		tokenURL: oauthTokenURL,
	}
}

// newRefresherForTest is used only by refresh_test.go to point TryRefresh at
// an httptest.Server instead of the real endpoint. Not exported: production
// callers must never be able to redirect this off Anthropic's token URL.
// CheckRedirect is forced to noFollowRedirects UNCONDITIONALLY, even when a
// test supplies its own client — a test must never be able to silently
// exercise a weaker redirect policy than production ever runs with.
func newRefresherForTest(tokenURL string, client *http.Client) *Refresher {
	if client == nil {
		client = &http.Client{}
	}
	client.CheckRedirect = noFollowRedirects
	return &Refresher{client: client, tokenURL: tokenURL}
}

// TryRefresh attempts one refresh-token grant for credentialJSON (a full
// Claude credential-set, the same bytes provider/claude.Driver.CredentialPath
// names). It makes AT MOST ONE network request — no internal retry loop, no
// unbounded wait — bounded by timeout (defaultRefreshTimeout when <= 0) and
// by ctx: a caller-canceled ctx aborts the in-flight request immediately and
// is classified as a transient failure (the token may still be valid; only
// the attempt was aborted).
//
// credentialJSON and the refresh/access tokens it carries are NEVER logged,
// NEVER included in the returned Error string, and NEVER placed in the
// request URL (they travel only in the POST body's JSON, matching
// oauth.py's own `data=body` construction) — see
// TestTryRefresh_CredentialNeverLeaksIntoErrors.
//
// On success, Credentials is credentialJSON with claudeAiOauth.accessToken,
// .expiresAt, (and .refreshToken/.scopes when the server rotated them)
// replaced — every other top-level and claudeAiOauth-nested field (any the
// caller's stored credential carries that this function does not recognize)
// passes through unchanged, mirroring Python's mutate-the-parsed-dict-then-
// json.dumps behavior exactly (oauth.py:136-145).
func (r *Refresher) TryRefresh(ctx context.Context, credentialJSON []byte, timeout time.Duration) RefreshOutcome {
	var top map[string]json.RawMessage
	if err := json.Unmarshal(credentialJSON, &top); err != nil {
		return RefreshOutcome{Error: "malformed_credential"}
	}
	if top == nil {
		// A well-formed JSON literal `null` unmarshals into a nil map with NO
		// error — Go's json package treats null-into-map as "leave it zero,"
		// not a failure. A nil top-level object is not a credential shape at
		// all (A5, reproduced with a literal `null` input), so it is treated
		// the same as unparseable input, not silently routed into the
		// "absent claudeAiOauth -> no_refresh_token" branch below (which
		// exists for a genuinely well-formed non-OAuth credential, e.g. an
		// api_key set, not for an empty/corrupt one).
		return RefreshOutcome{Error: "malformed_credential"}
	}
	oauthRaw, ok := top["claudeAiOauth"]
	if !ok {
		// A well-formed object with no claudeAiOauth block at all is a
		// legitimate non-OAuth credential (e.g. an api_key set) — genuinely
		// nothing to refresh, not a parse failure.
		return RefreshOutcome{Error: "no_refresh_token"}
	}
	var oauth map[string]json.RawMessage
	if err := json.Unmarshal(oauthRaw, &oauth); err != nil {
		// claudeAiOauth is PRESENT but not itself a JSON object (wrong type)
		// — a schema violation, not a legitimate "no OAuth data" shape.
		return RefreshOutcome{Error: "malformed_credential"}
	}
	refreshToken, ok := stringField(oauth, "refreshToken")
	if !ok || refreshToken == "" {
		return RefreshOutcome{Error: "no_refresh_token"}
	}

	reqBody, err := json.Marshal(map[string]string{
		"grant_type":    "refresh_token",
		"refresh_token": refreshToken,
		"client_id":     oauthClientID,
	})
	if err != nil {
		return RefreshOutcome{Error: "transient"}
	}

	if timeout <= 0 {
		timeout = defaultRefreshTimeout
	}
	reqCtx, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()

	req, err := http.NewRequestWithContext(reqCtx, http.MethodPost, r.tokenURL, bytes.NewReader(reqBody))
	if err != nil {
		return RefreshOutcome{Error: "transient"}
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("User-Agent", requestUserAgent)

	resp, err := r.client.Do(req)
	if err != nil {
		// Every branch below returns a coarse "transient" only — never err's
		// own message, which for a POST body error could in principle embed
		// request internals. classifyTransportError (collector.go) is not
		// reused here because none of its finer distinctions (timeout vs.
		// context-canceled vs. network) change this function's outcome: they
		// all collapse to "transient" (oauth.py's bare `except Exception`
		// does the same — no timeout/network split on the refresh path).
		_ = errors.Is(reqCtx.Err(), context.DeadlineExceeded) // documented, not branched on
		return RefreshOutcome{Error: "transient"}
	}
	defer resp.Body.Close()

	body, readErr := io.ReadAll(io.LimitReader(resp.Body, maxBodyBytes))
	if readErr != nil {
		return RefreshOutcome{Error: "transient"}
	}

	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		bodyStr := string(body)
		if (resp.StatusCode == http.StatusBadRequest ||
			resp.StatusCode == http.StatusUnauthorized ||
			resp.StatusCode == http.StatusForbidden) &&
			(strings.Contains(bodyStr, "invalid_grant") || strings.Contains(bodyStr, "invalid_client")) {
			return RefreshOutcome{Error: "invalid_grant"}
		}
		// Ambiguous 4xx/5xx: never echo bodyStr into the returned outcome —
		// it could carry request/response internals; only the coarse
		// classification crosses this function's boundary.
		return RefreshOutcome{Error: "transient"}
	}

	var tokenResp struct {
		AccessToken  string   `json:"access_token"`
		ExpiresIn    *float64 `json:"expires_in"`
		RefreshToken string   `json:"refresh_token"`
		Scope        string   `json:"scope"`
		Account      *struct {
			UUID         string `json:"uuid"`
			EmailAddress string `json:"email_address"`
		} `json:"account"`
		Organization *struct {
			UUID string `json:"uuid"`
		} `json:"organization"`
	}
	if err := json.Unmarshal(body, &tokenResp); err != nil {
		return RefreshOutcome{Error: "transient"}
	}
	// Mirrors oauth.py's `oauth["accessToken"] = resp_data["access_token"]`
	// and `oauth["expiresAt"] = now_ms + resp_data["expires_in"] * 1000`,
	// both of which raise (-> "transient" via the bare except) if either key
	// is absent. Go has no such implicit crash mode, so both are checked
	// explicitly here to reach the same outcome for the same malformed input.
	if tokenResp.AccessToken == "" || tokenResp.ExpiresIn == nil {
		return RefreshOutcome{Error: "transient"}
	}
	// A3 (adversarial review, reproduced empirically): an oversized-but-still-
	// finite expires_in (one that survives json.Unmarshal's float64 range
	// check, e.g. 1e18) overflows int64(*ExpiresIn*1000) below into an
	// unspecified value — measured: a large expires_in produced
	// expiresAt=-9223370249428224543, a garbage NEGATIVE epoch millisecond
	// that IsExpired (expiry.go) would then read as "already expired,"
	// silently poisoning the very credential this refresh was meant to fix.
	// Python has no equivalent failure mode (arbitrary-precision int), so
	// there is nothing to port here — this bound is new, added specifically
	// to close a Go-only overflow. REJECTED rather than clamped: clamping
	// would fabricate a plausible-looking but fictitious expiry and silently
	// mask a malformed or manipulated response, exactly the failure mode a
	// caller most needs to be able to detect; treating it as "transient" —
	// the same bucket every other malformed-2xx-body case already falls
	// into — costs nothing but a retry.
	if *tokenResp.ExpiresIn <= 0 || *tokenResp.ExpiresIn > maxExpiresInS {
		return RefreshOutcome{Error: "transient"}
	}

	nowMS := time.Now().UnixMilli()
	expiresAtMS := nowMS + int64(*tokenResp.ExpiresIn*1000)

	setRaw(oauth, "accessToken", tokenResp.AccessToken)
	setRaw(oauth, "expiresAt", expiresAtMS)
	if tokenResp.RefreshToken != "" {
		setRaw(oauth, "refreshToken", tokenResp.RefreshToken)
	}
	if tokenResp.Scope != "" {
		setRaw(oauth, "scopes", strings.Fields(tokenResp.Scope))
	}
	oauthBlob, err := json.Marshal(oauth)
	if err != nil {
		return RefreshOutcome{Error: "transient"}
	}
	top["claudeAiOauth"] = oauthBlob
	newCred, err := json.Marshal(top)
	if err != nil {
		return RefreshOutcome{Error: "transient"}
	}

	return RefreshOutcome{Credentials: newCred, TokenAccount: parseTokenAccount(tokenResp.Account, tokenResp.Organization)}
}

// stringField reads a string-typed field from a raw JSON object map, mirroring
// Python's permissive dict.get: absent or non-string returns ("", false).
func stringField(obj map[string]json.RawMessage, key string) (string, bool) {
	raw, ok := obj[key]
	if !ok {
		return "", false
	}
	var s string
	if err := json.Unmarshal(raw, &s); err != nil {
		return "", false
	}
	return s, true
}

// setRaw marshals value and stores it into obj[key], panicking only on a
// value this package itself constructs (a string or int64) — never on
// caller-supplied data — so the ignored error is safe.
func setRaw(obj map[string]json.RawMessage, key string, value any) {
	b, err := json.Marshal(value)
	if err != nil {
		// value is always a string, int64, or []string literal constructed
		// just above — never fails in practice.
		panic(fmt.Sprintf("usage: setRaw marshal %T: %v", value, err))
	}
	obj[key] = b
}

// parseTokenAccount extracts the optional account identity from a refresh
// response, ported from tsamx.oauth._parse_token_account (oauth.py:164-190).
// Returns nil unless a non-empty account.uuid is present — matching Python's
// strict boundary ("usable identity requires a non-empty string
// account.uuid"); email/organizationUuid are optional and degrade to "".
func parseTokenAccount(account *struct {
	UUID         string `json:"uuid"`
	EmailAddress string `json:"email_address"`
}, organization *struct {
	UUID string `json:"uuid"`
}) *TokenAccount {
	if account == nil || strings.TrimSpace(account.UUID) == "" {
		return nil
	}
	ta := &TokenAccount{UUID: strings.TrimSpace(account.UUID), Email: account.EmailAddress}
	if organization != nil {
		ta.OrganizationUUID = organization.UUID
	}
	return ta
}

// ClassifyRefreshFailure maps a failed RefreshOutcome.Error to the P4 idle-
// token status literal (expiry.go's StatusTokenExpired/StatusReloginRequired)
// a caller should report, so a wired-in refresh attempt connects to
// JudgeIdleExpiry's existing two-state contract instead of inventing a third.
// Ported semantics from tsamx.usage_store.PERMANENT_AUTH_ERRORS
// (usage_store.py:229-234: exactly {"invalid_grant", "no_refresh_token"} are
// permanent) and AUTH_DEAD_STRIKES's design-note rationale ("갱신 실패가
// invalid_grant로 누적될 때만 격리로 승격한다 — 단순 만료를 격리로 올리면
// 건전한 계정이 전부 격리된다"):
//
//   - "" (success) is not a failure; callers should not call this.
//   - "invalid_grant" / "no_refresh_token": the refresh-token lineage is
//     provably dead or was never present — StatusReloginRequired. This
//     package does NOT itself accumulate strikes before promoting (that
//     accounting — mirroring usage_store.py's AuthDeadStrikes/token_dead()
//     threshold — lives in the P5 usage-state store this file's sibling
//     commit adds); a caller wiring a scheduler on top decides whether to
//     require AUTH_DEAD_STRIKES consecutive invalid_grant answers (as tsamx
//     does) before acting on this classification, or to act on the first one.
//   - "transient" and "malformed_credential": anything else (network/5xx/
//     ambiguous-4xx/malformed-2xx/oversized-expires_in, or a credential this
//     package could not even parse — A5) — StatusTokenExpired. The token may
//     still be alive; the caller should retry the refresh later, exactly as
//     an idle profile's routine access-token expiry is already reported. A
//     corrupted file on disk must never quarantine an otherwise healthy
//     account merely because this attempt could not read it.
func ClassifyRefreshFailure(errKind string) string {
	switch errKind {
	case "invalid_grant", "no_refresh_token":
		return StatusReloginRequired
	default:
		return StatusTokenExpired
	}
}

// IdentityConflict reports whether outcome's opportunistic TokenAccount
// authenticates as a DIFFERENT account than expectedMeta claims — ported
// from tsamx.autoswitch.AutoSwitchEngine._note_token_identity's core
// judgment (autoswitch.py:800-836: "the credential authenticates under a
// different organization ... or as a different account uuid"), adapted to
// the fields provider.AddMeta actually carries.
//
// DOCUMENTED DIVERGENCE from tsamx: the original compares BOTH
// organizationUuid (checked first, whenever both sides record one) and
// account uuid, and BACKFILLS an empty slot uuid from the token response
// rather than treating it as unknown. provider.AddMeta (internal/provider/
// driver.go, not modified by this file) carries no OrganizationUUID field at
// all — only OrganizationName — so the org-first comparison cannot be
// ported; only the account-uuid comparison is. Backfill is also not
// implemented: AddMeta is a value the CALLER supplies fresh on every call,
// not a stored, mutable per-profile identity record this package owns, so
// there is nothing to backfill INTO here — a caller wiring this in is
// expected to already know and supply the account's real uuid via meta
// once one is known.
//
// Returns true (conflict) only when BOTH expectedMeta.AccountUUID and
// outcome.TokenAccount.UUID are non-empty (after trimming) and they differ.
// Either side being unknown is NOT a conflict — mirrors tsamx's "empty slot
// uuid -> not a conflict" outcome (minus the backfill write); there being
// nothing to compare against is not evidence of a mismatch.
func IdentityConflict(outcome RefreshOutcome, expectedMeta provider.AddMeta) bool {
	if outcome.TokenAccount == nil {
		return false
	}
	want := strings.TrimSpace(expectedMeta.AccountUUID)
	got := strings.TrimSpace(outcome.TokenAccount.UUID)
	if want == "" || got == "" {
		return false
	}
	return want != got
}

// StageRefreshedCredential persists a SUCCESSFUL RefreshOutcome onto a P2
// profile by delegating to profile.Store.Stage — never by writing
// outcome.Credentials to a file directly. This is the connection point the
// design note asks for ("Stage(락+마커 재기록)를 재사용하라 — 직접 파일을
// 쓰지 마라"): Stage takes the profile's per-account lock and re-records the
// staged-marker fingerprint atomically after the credential write succeeds
// (profile.go's Stage/Complete), so a caller that instead wrote
// `.credentials.json` by hand would leave the OLD marker standing next to
// the NEW (refreshed) bytes — the next profile.Store.State/Complete call
// would then misread a routine, healthy refresh as a StateRotated /
// !Complete "foreign rotation," which is exactly the false alarm the P2
// package's own doc warns against (profile.go's Complete CAUTION comment).
//
// meta carries the identity fields Stage writes into the profile's
// .claude.json (Email/AccountUUID/OrganizationName) — the same triple every
// other Stage call site supplies (e.g. the initial delivery from AMS). This
// function does not attempt to invent meta from outcome.TokenAccount: that
// value is opportunistic and carries no OrganizationName at all (see
// TokenAccount's doc), so silently substituting it here could silently
// regress a caller's already-known-good identity fields on a refresh that
// happened to omit the account block. A caller that wants to fold
// outcome.TokenAccount in decides that explicitly, one layer up.
//
// Returns an error without touching the profile at all when outcome is not a
// success (Error != "" or Credentials == nil) — calling this on a failed
// outcome is always a caller bug, not a legitimate no-op.
//
// conflict reports IdentityConflict(outcome, meta) — computed AFTER Stage
// runs, never gating it: mirrors tsamx's _freshen_target, which persists the
// rotated credential UNCONDITIONALLY before ever checking identity ("the
// grant consumed a generation, and not writing the successor would kill the
// lineage regardless of whose it turns out to be" — autoswitch.py's own
// comment on this exact ordering). A true conflict means the credential now
// staged authenticates as a different account than expected: the caller
// must quarantine this profile as a switch target (never activate it) but
// must NOT re-attempt the refresh or discard what was just staged — losing
// the newly rotated refresh token here would kill the lineage for good.
func StageRefreshedCredential(store *profile.Store, drv provider.Driver, accountKey string, outcome RefreshOutcome, meta provider.AddMeta) (conflict bool, err error) {
	if outcome.Error != "" || outcome.Credentials == nil {
		return false, fmt.Errorf("usage: refresh outcome is not a success (error=%q), nothing to stage", outcome.Error)
	}
	if err := store.Stage(drv, accountKey, outcome.Credentials, meta); err != nil {
		return false, err
	}
	return IdentityConflict(outcome, meta), nil
}
