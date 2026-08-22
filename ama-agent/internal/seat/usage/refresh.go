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
//   - "no_refresh_token": the input credential has no claudeAiOauth block, or
//     that block has no non-empty refreshToken (oauth.py:110-114). No network
//     call is made in this case. Equally permanent for retry purposes as
//     invalid_grant (mirrors tsamx's PERMANENT_AUTH_ERRORS).
//   - "transient": anything else — network error, timeout, 5xx, an ambiguous
//     4xx without the invalid_grant/invalid_client marker, or a malformed 2xx
//     body (oauth.py:158-161's bare `except Exception`). Retry later; the
//     token may still be valid.
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

// NewRefresher returns a Refresher targeting the real Claude token endpoint,
// using the default transport (proxy-/TLS-config-aware) with no client-level
// timeout — every call bounds itself via the context TryRefresh derives
// internally from its timeout parameter, so a slow DNS/connect phase cannot
// outlive that bound even though the client itself carries none.
func NewRefresher() *Refresher {
	return &Refresher{
		client:   &http.Client{Transport: http.DefaultTransport},
		tokenURL: oauthTokenURL,
	}
}

// newRefresherForTest is used only by refresh_test.go to point TryRefresh at
// an httptest.Server instead of the real endpoint. Not exported: production
// callers must never be able to redirect this off Anthropic's token URL.
func newRefresherForTest(tokenURL string, client *http.Client) *Refresher {
	if client == nil {
		client = &http.Client{}
	}
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
		return RefreshOutcome{Error: "no_refresh_token"}
	}
	oauthRaw, ok := top["claudeAiOauth"]
	if !ok {
		return RefreshOutcome{Error: "no_refresh_token"}
	}
	var oauth map[string]json.RawMessage
	if err := json.Unmarshal(oauthRaw, &oauth); err != nil {
		return RefreshOutcome{Error: "no_refresh_token"}
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
//   - "transient": anything else (network/5xx/ambiguous-4xx/malformed-2xx) —
//     StatusTokenExpired. The token may still be alive; the caller should
//     retry the refresh later, exactly as an idle profile's routine
//     access-token expiry is already reported.
func ClassifyRefreshFailure(errKind string) string {
	switch errKind {
	case "invalid_grant", "no_refresh_token":
		return StatusReloginRequired
	default:
		return StatusTokenExpired
	}
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
func StageRefreshedCredential(store *profile.Store, drv provider.Driver, accountKey string, outcome RefreshOutcome, meta provider.AddMeta) error {
	if outcome.Error != "" || outcome.Credentials == nil {
		return fmt.Errorf("usage: refresh outcome is not a success (error=%q), nothing to stage", outcome.Error)
	}
	return store.Stage(drv, accountKey, outcome.Credentials, meta)
}
