package usage

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"strings"
	"testing"
	"time"

	"github.com/2kwanghee/AMX/ama-agent/internal/provider"
	"github.com/2kwanghee/AMX/ama-agent/internal/provider/claude"
	"github.com/2kwanghee/AMX/ama-agent/internal/seat/profile"
)

// secretRefreshToken/secretAccessToken are distinctive values used across the
// leak-check tests below: no returned error, in any scenario, may ever
// contain either substring.
const (
	secretRefreshToken = "sk-ant-ort01-REFRESH-DO-NOT-LEAK-7c2e"
	secretAccessToken  = "sk-ant-oat01-ACCESS-DO-NOT-LEAK-4b91"
)

func newTestRefresher(t *testing.T, handler http.HandlerFunc) *Refresher {
	t.Helper()
	srv := httptest.NewServer(handler)
	t.Cleanup(srv.Close)
	return newRefresherForTest(srv.URL, &http.Client{})
}

func refreshCredJSON(t *testing.T, refreshToken string, extra map[string]any) []byte {
	t.Helper()
	oauth := map[string]any{
		"refreshToken": refreshToken,
		"accessToken":  "old-access-token",
		"expiresAt":    float64(1000),
		// subscriptionType is a field this package does not recognize; it
		// must survive a refresh untouched (round-trip fidelity check).
		"subscriptionType": "pro",
	}
	top := map[string]any{
		"claudeAiOauth": oauth,
		// a top-level field this package does not recognize either.
		"otherTopLevelField": "keep-me",
	}
	for k, v := range extra {
		top[k] = v
	}
	b, err := json.Marshal(top)
	if err != nil {
		t.Fatalf("marshal test credential: %v", err)
	}
	return b
}

func TestTryRefresh_Success(t *testing.T) {
	var sawContentType, sawUA, sawMethod string
	var sawBody map[string]string
	r := newTestRefresher(t, func(w http.ResponseWriter, req *http.Request) {
		sawMethod = req.Method
		sawContentType = req.Header.Get("Content-Type")
		sawUA = req.Header.Get("User-Agent")
		if req.URL.RawQuery != "" {
			t.Errorf("request carried a URL query: %q (token must travel only in the POST body)", req.URL.RawQuery)
		}
		_ = json.NewDecoder(req.Body).Decode(&sawBody)
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte(`{
			"access_token": "` + secretAccessToken + `",
			"expires_in": 3600,
			"refresh_token": "new-refresh-token",
			"scope": "org:create_api_key user:profile",
			"account": {"uuid": "acct-uuid-1", "email_address": "user@example.com"},
			"organization": {"uuid": "org-uuid-1"}
		}`))
	})

	before := time.Now()
	out := r.TryRefresh(context.Background(), refreshCredJSON(t, secretRefreshToken, nil), time.Second)
	if out.Error != "" {
		t.Fatalf("Error = %q, want success", out.Error)
	}
	if sawMethod != http.MethodPost {
		t.Errorf("method = %q, want POST", sawMethod)
	}
	if sawContentType != "application/json" {
		t.Errorf("Content-Type = %q, want application/json", sawContentType)
	}
	if sawUA != requestUserAgent {
		t.Errorf("User-Agent = %q, want %q", sawUA, requestUserAgent)
	}
	if sawBody["grant_type"] != "refresh_token" || sawBody["refresh_token"] != secretRefreshToken || sawBody["client_id"] != oauthClientID {
		t.Errorf("request body = %+v, want grant_type/refresh_token/client_id matching oauth.py's shape", sawBody)
	}

	var got struct {
		ClaudeAiOauth struct {
			AccessToken      string   `json:"accessToken"`
			RefreshToken     string   `json:"refreshToken"`
			ExpiresAt        float64  `json:"expiresAt"`
			Scopes           []string `json:"scopes"`
			SubscriptionType string   `json:"subscriptionType"`
		} `json:"claudeAiOauth"`
		OtherTopLevelField string `json:"otherTopLevelField"`
	}
	if err := json.Unmarshal(out.Credentials, &got); err != nil {
		t.Fatalf("unmarshal returned credentials: %v", err)
	}
	if got.ClaudeAiOauth.AccessToken != secretAccessToken {
		t.Errorf("accessToken = %q, want %q", got.ClaudeAiOauth.AccessToken, secretAccessToken)
	}
	if got.ClaudeAiOauth.RefreshToken != "new-refresh-token" {
		t.Errorf("refreshToken = %q, want rotated value", got.ClaudeAiOauth.RefreshToken)
	}
	wantExpiresAt := float64(before.UnixMilli() + 3600*1000)
	if got.ClaudeAiOauth.ExpiresAt < wantExpiresAt-2000 || got.ClaudeAiOauth.ExpiresAt > wantExpiresAt+5000 {
		t.Errorf("expiresAt = %v, want near %v", got.ClaudeAiOauth.ExpiresAt, wantExpiresAt)
	}
	if len(got.ClaudeAiOauth.Scopes) != 2 || got.ClaudeAiOauth.Scopes[0] != "org:create_api_key" {
		t.Errorf("scopes = %v, want split from the scope string", got.ClaudeAiOauth.Scopes)
	}
	if got.ClaudeAiOauth.SubscriptionType != "pro" {
		t.Errorf("subscriptionType = %q, an unrecognized field must round-trip unchanged", got.ClaudeAiOauth.SubscriptionType)
	}
	if got.OtherTopLevelField != "keep-me" {
		t.Errorf("otherTopLevelField = %q, an unrecognized top-level field must round-trip unchanged", got.OtherTopLevelField)
	}

	if out.TokenAccount == nil {
		t.Fatal("TokenAccount = nil, want the opportunistic identity from the response")
	}
	if out.TokenAccount.UUID != "acct-uuid-1" || out.TokenAccount.Email != "user@example.com" || out.TokenAccount.OrganizationUUID != "org-uuid-1" {
		t.Errorf("TokenAccount = %+v, want the parsed account/organization", out.TokenAccount)
	}
}

func TestTryRefresh_SuccessWithoutRotatedRefreshToken(t *testing.T) {
	// The server may omit refresh_token/scope/account entirely — the old
	// refreshToken must be preserved, not blanked (oauth.py:138-141: `if
	// resp_data.get("refresh_token"):` only overwrites when present).
	r := newTestRefresher(t, func(w http.ResponseWriter, req *http.Request) {
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte(`{"access_token": "` + secretAccessToken + `", "expires_in": 60}`))
	})
	out := r.TryRefresh(context.Background(), refreshCredJSON(t, secretRefreshToken, nil), time.Second)
	if out.Error != "" {
		t.Fatalf("Error = %q, want success", out.Error)
	}
	var got struct {
		ClaudeAiOauth struct {
			RefreshToken string `json:"refreshToken"`
		} `json:"claudeAiOauth"`
	}
	if err := json.Unmarshal(out.Credentials, &got); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	if got.ClaudeAiOauth.RefreshToken != secretRefreshToken {
		t.Errorf("refreshToken = %q, want the original preserved when the server sent none", got.ClaudeAiOauth.RefreshToken)
	}
	if out.TokenAccount != nil {
		t.Errorf("TokenAccount = %+v, want nil when the response carried no account block", out.TokenAccount)
	}
}

func TestTryRefresh_InvalidGrant(t *testing.T) {
	for _, code := range []int{http.StatusBadRequest, http.StatusUnauthorized, http.StatusForbidden} {
		t.Run(fmt.Sprintf("http-%d", code), func(t *testing.T) {
			r := newTestRefresher(t, func(w http.ResponseWriter, req *http.Request) {
				w.WriteHeader(code)
				_, _ = w.Write([]byte(`{"error": "invalid_grant", "error_description": "Refresh token not found or invalid"}`))
			})
			out := r.TryRefresh(context.Background(), refreshCredJSON(t, secretRefreshToken, nil), time.Second)
			if out.Error != "invalid_grant" {
				t.Fatalf("Error = %q, want invalid_grant", out.Error)
			}
			if out.Credentials != nil {
				t.Errorf("Credentials = %v, want nil on failure", out.Credentials)
			}
		})
	}
}

func TestTryRefresh_InvalidClientMarker(t *testing.T) {
	r := newTestRefresher(t, func(w http.ResponseWriter, req *http.Request) {
		w.WriteHeader(http.StatusUnauthorized)
		_, _ = w.Write([]byte(`{"error": "invalid_client"}`))
	})
	out := r.TryRefresh(context.Background(), refreshCredJSON(t, secretRefreshToken, nil), time.Second)
	if out.Error != "invalid_grant" {
		t.Fatalf("Error = %q, want invalid_grant (invalid_client marker also classifies as dead grant)", out.Error)
	}
}

func TestTryRefresh_AmbiguousFourXXStaysTransient(t *testing.T) {
	// A 400 WITHOUT the invalid_grant/invalid_client marker must stay
	// transient (oauth.py: "anything ambiguous stays transient — a
	// misclassified transient costs one retry, a misclassified permanent
	// would wrongly quarantine a live token").
	r := newTestRefresher(t, func(w http.ResponseWriter, req *http.Request) {
		w.WriteHeader(http.StatusBadRequest)
		_, _ = w.Write([]byte(`{"error": "some_other_error"}`))
	})
	out := r.TryRefresh(context.Background(), refreshCredJSON(t, secretRefreshToken, nil), time.Second)
	if out.Error != "transient" {
		t.Fatalf("Error = %q, want transient", out.Error)
	}
}

func TestTryRefresh_ServerErrorIsTransient(t *testing.T) {
	r := newTestRefresher(t, func(w http.ResponseWriter, req *http.Request) {
		w.WriteHeader(http.StatusInternalServerError)
		_, _ = w.Write([]byte("boom"))
	})
	out := r.TryRefresh(context.Background(), refreshCredJSON(t, secretRefreshToken, nil), time.Second)
	if out.Error != "transient" {
		t.Fatalf("Error = %q, want transient", out.Error)
	}
}

func TestTryRefresh_MalformedSuccessBodyIsTransient(t *testing.T) {
	r := newTestRefresher(t, func(w http.ResponseWriter, req *http.Request) {
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte(`{"expires_in": 60}`)) // no access_token
	})
	out := r.TryRefresh(context.Background(), refreshCredJSON(t, secretRefreshToken, nil), time.Second)
	if out.Error != "transient" {
		t.Fatalf("Error = %q, want transient for a 2xx body missing access_token", out.Error)
	}
}

// TestTryRefresh_NoRefreshTokenNeverCallsNetwork covers credentials that
// PARSE CLEANLY but genuinely carry no usable refresh token — a legitimate
// shape (e.g. a `claude setup token` account, or a non-OAuth api_key
// credential with no claudeAiOauth block at all), distinct from a credential
// this package could not even parse (see
// TestTryRefresh_MalformedCredentialNeverCallsNetwork, A5).
func TestTryRefresh_NoRefreshTokenNeverCallsNetwork(t *testing.T) {
	called := false
	r := newTestRefresher(t, func(w http.ResponseWriter, req *http.Request) {
		called = true
		w.WriteHeader(http.StatusOK)
	})

	cases := map[string][]byte{
		"missing claudeAiOauth (e.g. an api_key credential)": []byte(`{"foo": "bar"}`),
		"empty refreshToken":         refreshCredJSON(t, "", nil),
		"missing refreshToken field": mustMarshal(t, map[string]any{"claudeAiOauth": map[string]any{"accessToken": "x"}}),
	}
	for name, cred := range cases {
		t.Run(name, func(t *testing.T) {
			called = false
			out := r.TryRefresh(context.Background(), cred, time.Second)
			if out.Error != "no_refresh_token" {
				t.Fatalf("Error = %q, want no_refresh_token", out.Error)
			}
			if called {
				t.Error("network was called despite no usable refresh token")
			}
		})
	}
}

// TestTryRefresh_MalformedCredentialNeverCallsNetwork is the A5 regression
// test (adversarial review, reproduced with truncated/empty/JSON-array/
// JSON-null inputs): a credential this package cannot even PARSE must be
// classified distinctly from "no_refresh_token" (which
// ClassifyRefreshFailure promotes toward relogin_required/quarantine) — a
// merely corrupted file on disk must never quarantine an otherwise healthy
// account.
func TestTryRefresh_MalformedCredentialNeverCallsNetwork(t *testing.T) {
	called := false
	r := newTestRefresher(t, func(w http.ResponseWriter, req *http.Request) {
		called = true
		w.WriteHeader(http.StatusOK)
	})

	cases := map[string][]byte{
		"truncated":                 []byte(`{"claudeAiOauth": {"refreshTok`),
		"empty":                     []byte(``),
		"not json at all":           []byte("not json"),
		"json array":                []byte(`[]`),
		"json null":                 []byte(`null`),
		"claudeAiOauth wrong shape": []byte(`{"claudeAiOauth": "not-an-object"}`),
	}
	for name, cred := range cases {
		t.Run(name, func(t *testing.T) {
			called = false
			out := r.TryRefresh(context.Background(), cred, time.Second)
			if out.Error != "malformed_credential" {
				t.Fatalf("Error = %q, want malformed_credential", out.Error)
			}
			if called {
				t.Error("network was called despite an unparseable credential")
			}
			// The whole point (A5): this must NOT be treated as a permanent/
			// quarantine-worthy failure the way a verified-absent refresh
			// token is.
			if got := ClassifyRefreshFailure(out.Error); got != StatusTokenExpired {
				t.Errorf("ClassifyRefreshFailure(%q) = %q, want StatusTokenExpired (never quarantine on a parse failure)", out.Error, got)
			}
		})
	}
}

func mustMarshal(t *testing.T, v any) []byte {
	t.Helper()
	b, err := json.Marshal(v)
	if err != nil {
		t.Fatalf("marshal: %v", err)
	}
	return b
}

// Both timeout tests below release their handler explicitly, AFTER
// asserting TryRefresh already returned, rather than relying on the client's
// context cancellation to tear down the TCP connection out from under a
// still-blocked handler: httptest.Server.Close (invoked by t.Cleanup) waits
// for every connection to become idle, and on this environment a
// server-side handler blocked on <-req.Context().Done() was observed to
// never unblock even after the client-side call had already returned —
// hanging Close() forever. t.Cleanup runs LIFO, so registering the release
// AFTER newTestRefresher's own t.Cleanup(srv.Close) guarantees the handler
// is released (and can finish its response normally) before Close() ever
// waits on it.

func TestTryRefresh_RespectsTimeout(t *testing.T) {
	release := make(chan struct{})
	r := newTestRefresher(t, func(w http.ResponseWriter, req *http.Request) {
		<-release
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte(`{"access_token": "late", "expires_in": 1}`))
	})
	t.Cleanup(func() { close(release) })

	start := time.Now()
	out := r.TryRefresh(context.Background(), refreshCredJSON(t, secretRefreshToken, nil), 100*time.Millisecond)
	elapsed := time.Since(start)

	if out.Error != "transient" {
		t.Fatalf("Error = %q, want transient on timeout", out.Error)
	}
	if elapsed > 2*time.Second {
		t.Fatalf("TryRefresh took %v, want it bounded near its 100ms timeout (no unbounded wait)", elapsed)
	}
}

func TestTryRefresh_RespectsContextCancellation(t *testing.T) {
	release := make(chan struct{})
	r := newTestRefresher(t, func(w http.ResponseWriter, req *http.Request) {
		<-release
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte(`{"access_token": "late", "expires_in": 1}`))
	})
	t.Cleanup(func() { close(release) })

	ctx, cancel := context.WithCancel(context.Background())
	go func() {
		time.Sleep(50 * time.Millisecond)
		cancel()
	}()

	start := time.Now()
	out := r.TryRefresh(ctx, refreshCredJSON(t, secretRefreshToken, nil), 10*time.Second)
	elapsed := time.Since(start)

	if out.Error != "transient" {
		t.Fatalf("Error = %q, want transient on caller cancellation", out.Error)
	}
	if elapsed > 2*time.Second {
		t.Fatalf("TryRefresh took %v, want it bounded near the cancellation, not the 10s timeout", elapsed)
	}
}

// TestTryRefresh_CredentialNeverLeaksIntoErrors runs every failure path this
// file defines and asserts neither secret ever appears in the returned
// Error/kind string. This is the one enforcement point the P5 task asks for
// ("자격증명·토큰이 로그·에러 문자열·URL 쿼리에 절대 남지 않게 하고 그 테스트
// 를 넣어라").
func TestTryRefresh_CredentialNeverLeaksIntoErrors(t *testing.T) {
	scenarios := []struct {
		name    string
		handler http.HandlerFunc
		cred    []byte
	}{
		{"invalid_grant", func(w http.ResponseWriter, req *http.Request) {
			w.WriteHeader(http.StatusBadRequest)
			_, _ = w.Write([]byte(`{"error": "invalid_grant", "hint": "` + secretRefreshToken + `"}`))
		}, nil},
		{"server error with token echoed in body", func(w http.ResponseWriter, req *http.Request) {
			w.WriteHeader(http.StatusInternalServerError)
			_, _ = w.Write([]byte(`request contained token ` + secretRefreshToken))
		}, nil},
		{"malformed success body", func(w http.ResponseWriter, req *http.Request) {
			w.WriteHeader(http.StatusOK)
			_, _ = w.Write([]byte(`{"expires_in": 1}`))
		}, nil},
		{"no refresh token in credential", nil, refreshCredJSON(t, "", nil)},
	}

	for _, sc := range scenarios {
		t.Run(sc.name, func(t *testing.T) {
			var r *Refresher
			if sc.handler != nil {
				r = newTestRefresher(t, sc.handler)
			} else {
				// This scenario ("no refresh token in credential") never
				// reaches the network at all, so the URL below is never
				// dialed — a closed local listener rather than an arbitrary
				// port so a mistaken dial fails fast instead of hanging on a
				// sandbox that silently drops SYNs to unowned ports.
				closedSrv := httptest.NewServer(http.NotFoundHandler())
				closedSrv.Close()
				r = newRefresherForTest(closedSrv.URL, &http.Client{Timeout: time.Second})
			}
			cred := sc.cred
			if cred == nil {
				cred = refreshCredJSON(t, secretRefreshToken, nil)
			}
			out := r.TryRefresh(context.Background(), cred, time.Second)
			if out.Error == "" {
				t.Fatal("expected a failure classification")
			}
			if strings.Contains(out.Error, secretRefreshToken) || strings.Contains(out.Error, secretAccessToken) {
				t.Fatalf("Error = %q leaked a secret token", out.Error)
			}
		})
	}
}

func TestClassifyRefreshFailure(t *testing.T) {
	cases := map[string]string{
		"invalid_grant":     StatusReloginRequired,
		"no_refresh_token":  StatusReloginRequired,
		"transient":         StatusTokenExpired,
		"anything-else-too": StatusTokenExpired,
	}
	for kind, want := range cases {
		if got := ClassifyRefreshFailure(kind); got != want {
			t.Errorf("ClassifyRefreshFailure(%q) = %q, want %q", kind, got, want)
		}
	}
}

// TestStageRefreshedCredential_UsesProfileStage verifies the P5 connection
// point: a successful refresh is persisted via profile.Store.Stage (lock +
// marker rewrite), never a direct file write, so a subsequent State/Complete
// check reads the profile as cleanly staged rather than "rotated" (which
// would misread a routine refresh as a foreign in-place rotation — see
// StageRefreshedCredential's doc).
func TestStageRefreshedCredential_UsesProfileStage(t *testing.T) {
	store, err := profile.Open(t.TempDir())
	if err != nil {
		t.Fatalf("profile.Open: %v", err)
	}
	drv := claude.New()
	accountKey := profile.AccountKey("user@example.com")

	initialMeta := provider.AddMeta{Email: "user@example.com", AccountUUID: "acct-1"}
	if err := store.Stage(drv, accountKey, refreshCredJSON(t, "initial-refresh-token", nil), initialMeta); err != nil {
		t.Fatalf("initial Stage: %v", err)
	}
	complete, err := store.Complete(drv, accountKey)
	if err != nil || !complete {
		t.Fatalf("initial Complete = (%v, %v), want (true, nil)", complete, err)
	}

	refreshed := RefreshOutcome{Credentials: refreshCredJSON(t, "rotated-refresh-token", nil)}
	conflict, err := StageRefreshedCredential(store, drv, accountKey, refreshed, initialMeta)
	if err != nil {
		t.Fatalf("StageRefreshedCredential: %v", err)
	}
	if conflict {
		t.Error("conflict = true with no TokenAccount in the outcome, want false")
	}

	complete, err = store.Complete(drv, accountKey)
	if err != nil || !complete {
		t.Fatalf("post-refresh Complete = (%v, %v), want (true, nil) — the marker must be re-recorded, not left pointing at the old credential", complete, err)
	}
	state, err := store.State(drv, accountKey)
	if err != nil || state != profile.StateStaged {
		t.Fatalf("post-refresh State = (%v, %v), want (StateStaged, nil)", state, err)
	}

	dir, err := store.ProfileDir(drv.Name(), accountKey)
	if err != nil {
		t.Fatalf("ProfileDir: %v", err)
	}
	onDisk, err := os.ReadFile(drv.CredentialPath(dir))
	if err != nil {
		t.Fatalf("read staged credential: %v", err)
	}
	if !strings.Contains(string(onDisk), "rotated-refresh-token") {
		t.Error("staged credential on disk does not carry the refreshed token")
	}
}

func TestStageRefreshedCredential_RejectsFailedOutcome(t *testing.T) {
	store, err := profile.Open(t.TempDir())
	if err != nil {
		t.Fatalf("profile.Open: %v", err)
	}
	drv := claude.New()
	accountKey := profile.AccountKey("user@example.com")

	for _, out := range []RefreshOutcome{
		{Error: "invalid_grant"},
		{Error: "transient"},
		{}, // Credentials nil, Error "" — malformed caller usage
	} {
		if _, err := StageRefreshedCredential(store, drv, accountKey, out, provider.AddMeta{Email: "user@example.com"}); err == nil {
			t.Errorf("StageRefreshedCredential(%+v) = nil error, want a rejection", out)
		}
	}
	if _, err := store.State(drv, accountKey); err != nil {
		t.Fatalf("State on an untouched profile should not error: %v", err)
	}
	complete, err := store.Complete(drv, accountKey)
	if err != nil || complete {
		t.Fatalf("Complete = (%v, %v), want (false, nil) — a rejected outcome must not stage anything", complete, err)
	}
}

// -- A1 (adversarial review): redirect never leaks the refresh token -------

// TestTryRefresh_RedirectIsNeverFollowed is the A1 regression test. A
// primary server answers with a 307 (which, per RFC 7231, preserves the
// original request's method AND BODY) pointing at a second, "malicious"
// server; the malicious server would receive credJSON's real refresh token
// verbatim in its POST body if TryRefresh ever followed the redirect. It
// must not: Go's default CheckRedirect follows up to 10 redirects, which is
// exactly the bug NewRefresher's CheckRedirect (refresh.go) now closes.
func TestTryRefresh_RedirectIsNeverFollowed(t *testing.T) {
	for _, code := range []int{http.StatusTemporaryRedirect, http.StatusPermanentRedirect} {
		t.Run(fmt.Sprintf("http-%d", code), func(t *testing.T) {
			maliciousCalled := false
			var maliciousSawBody map[string]string
			malicious := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, req *http.Request) {
				maliciousCalled = true
				_ = json.NewDecoder(req.Body).Decode(&maliciousSawBody)
				// A malicious host would of course claim success and hand
				// back attacker-controlled tokens.
				w.WriteHeader(http.StatusOK)
				_, _ = w.Write([]byte(`{"access_token": "ATTACKER-CONTROLLED-TOKEN", "expires_in": 3600}`))
			}))
			t.Cleanup(malicious.Close)

			primary := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, req *http.Request) {
				http.Redirect(w, req, malicious.URL, code)
			}))
			t.Cleanup(primary.Close)

			r := newRefresherForTest(primary.URL, &http.Client{Timeout: 2 * time.Second})
			out := r.TryRefresh(context.Background(), refreshCredJSON(t, secretRefreshToken, nil), time.Second)

			if maliciousCalled {
				t.Fatal("the malicious redirect target was contacted at all — the refresh_token was sent to it")
			}
			if maliciousSawBody != nil {
				t.Fatalf("malicious server decoded a body: %+v", maliciousSawBody)
			}
			if out.Error == "" {
				t.Fatalf("out = %+v, want a failure — a redirect response must never classify as success", out)
			}
			if out.Credentials != nil {
				t.Fatalf("Credentials = %s, want nil — must never adopt the attacker's fabricated token", out.Credentials)
			}
		})
	}
}

// TestFetch_RedirectIsNeverFollowed is Collector's A1 counterpart: a
// redirect must never be classified as a successful usage fetch, and the
// redirect target (which would receive the Authorization header on some Go
// versions/stdlib configurations if this policy regressed) must never be
// contacted.
func TestFetch_RedirectIsNeverFollowed(t *testing.T) {
	maliciousCalled := false
	malicious := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, req *http.Request) {
		maliciousCalled = true
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte(`{"five_hour": {"utilization": 1}}`))
	}))
	t.Cleanup(malicious.Close)

	primary := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, req *http.Request) {
		http.Redirect(w, req, malicious.URL, http.StatusTemporaryRedirect)
	}))
	t.Cleanup(primary.Close)

	c := newCollectorForTest(primary.URL, &http.Client{Timeout: 2 * time.Second})
	got, err := c.Fetch(context.Background(), secretToken)

	if maliciousCalled {
		t.Fatal("the malicious redirect target was contacted at all")
	}
	if err == nil {
		t.Fatalf("got = %+v, err = nil, want a *FetchError — a redirect must never classify as success", got)
	}
	var fe *FetchError
	if !asFetchError(err, &fe) {
		t.Fatalf("err = %v, want *FetchError", err)
	}
	if fe.Kind != "http-307" {
		t.Errorf("FetchError.Kind = %q, want http-307", fe.Kind)
	}
}

// -- A3 (adversarial review): expires_in overflow ---------------------------

// TestTryRefresh_OversizedExpiresInIsRejected is the A3 regression test: an
// expires_in value that survives json.Unmarshal's float64 range check but
// overflows int64(*ExpiresIn*1000) inside TryRefresh must be rejected
// (transient), never silently turned into a garbage expiresAt.
func TestTryRefresh_OversizedExpiresInIsRejected(t *testing.T) {
	cases := map[string]float64{
		"far past maxExpiresInS":  1e18,
		"just past maxExpiresInS": maxExpiresInS + 1,
		"negative":                -1,
		"zero":                    0,
	}
	for name, expiresIn := range cases {
		t.Run(name, func(t *testing.T) {
			r := newTestRefresher(t, func(w http.ResponseWriter, req *http.Request) {
				w.WriteHeader(http.StatusOK)
				body, _ := json.Marshal(map[string]any{
					"access_token": secretAccessToken,
					"expires_in":   expiresIn,
				})
				_, _ = w.Write(body)
			})
			out := r.TryRefresh(context.Background(), refreshCredJSON(t, secretRefreshToken, nil), time.Second)
			if out.Error != "transient" {
				t.Fatalf("Error = %q, want transient for expires_in=%v", out.Error, expiresIn)
			}
			if out.Credentials != nil {
				t.Fatalf("Credentials = %s, want nil — must never write a garbage expiresAt", out.Credentials)
			}
		})
	}
}

func TestTryRefresh_SaneExpiresInStillSucceeds(t *testing.T) {
	r := newTestRefresher(t, func(w http.ResponseWriter, req *http.Request) {
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte(`{"access_token": "` + secretAccessToken + `", "expires_in": 3600}`))
	})
	out := r.TryRefresh(context.Background(), refreshCredJSON(t, secretRefreshToken, nil), time.Second)
	if out.Error != "" {
		t.Fatalf("Error = %q, want success for a sane expires_in", out.Error)
	}
}

// -- A4 (adversarial review): identity-conflict on refresh ------------------

func TestIdentityConflict(t *testing.T) {
	conflictTA := &TokenAccount{UUID: "acct-attacker"}
	matchTA := &TokenAccount{UUID: "acct-expected"}

	cases := []struct {
		name    string
		outcome RefreshOutcome
		meta    provider.AddMeta
		want    bool
	}{
		{"uuid mismatch is a conflict", RefreshOutcome{TokenAccount: conflictTA}, provider.AddMeta{AccountUUID: "acct-expected"}, true},
		{"uuid match is not a conflict", RefreshOutcome{TokenAccount: matchTA}, provider.AddMeta{AccountUUID: "acct-expected"}, false},
		{"no TokenAccount is not a conflict (opportunistic, absent)", RefreshOutcome{}, provider.AddMeta{AccountUUID: "acct-expected"}, false},
		{"unknown expected uuid is not a conflict (nothing to compare)", RefreshOutcome{TokenAccount: conflictTA}, provider.AddMeta{}, false},
		{"unknown token uuid is not a conflict", RefreshOutcome{TokenAccount: &TokenAccount{}}, provider.AddMeta{AccountUUID: "acct-expected"}, false},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			if got := IdentityConflict(c.outcome, c.meta); got != c.want {
				t.Errorf("IdentityConflict(%+v, %+v) = %v, want %v", c.outcome, c.meta, got, c.want)
			}
		})
	}
}

// TestStageRefreshedCredential_IdentityConflictStillStagesButReportsTrue
// verifies StageRefreshedCredential's ordering (A4, mirroring
// autoswitch.py's _freshen_target: "Persist first, unconditionally: the
// grant consumed a generation, and not writing the successor would kill the
// lineage regardless of whose it turns out to be"): a conflicting identity
// must NOT prevent the rotated credential from being staged, but must be
// reported so the caller can quarantine this profile as a switch target.
func TestStageRefreshedCredential_IdentityConflictStillStagesButReportsTrue(t *testing.T) {
	store, err := profile.Open(t.TempDir())
	if err != nil {
		t.Fatalf("profile.Open: %v", err)
	}
	drv := claude.New()
	accountKey := profile.AccountKey("user@example.com")
	meta := provider.AddMeta{Email: "user@example.com", AccountUUID: "acct-expected"}

	if err := store.Stage(drv, accountKey, refreshCredJSON(t, "initial-refresh-token", nil), meta); err != nil {
		t.Fatalf("initial Stage: %v", err)
	}

	outcome := RefreshOutcome{
		Credentials:  refreshCredJSON(t, "rotated-refresh-token", nil),
		TokenAccount: &TokenAccount{UUID: "acct-DIFFERENT"},
	}
	conflict, err := StageRefreshedCredential(store, drv, accountKey, outcome, meta)
	if err != nil {
		t.Fatalf("StageRefreshedCredential: %v", err)
	}
	if !conflict {
		t.Error("conflict = false, want true for a mismatched TokenAccount.UUID")
	}

	// The rotated credential must still be on disk — losing it here would
	// kill the refresh-token lineage for good.
	complete, err := store.Complete(drv, accountKey)
	if err != nil || !complete {
		t.Fatalf("Complete = (%v, %v), want (true, nil) — identity conflict must not prevent staging", complete, err)
	}
	dir, err := store.ProfileDir(drv.Name(), accountKey)
	if err != nil {
		t.Fatalf("ProfileDir: %v", err)
	}
	onDisk, err := os.ReadFile(drv.CredentialPath(dir))
	if err != nil {
		t.Fatalf("read staged credential: %v", err)
	}
	if !strings.Contains(string(onDisk), "rotated-refresh-token") {
		t.Error("the rotated credential was not staged despite the identity conflict")
	}
}
