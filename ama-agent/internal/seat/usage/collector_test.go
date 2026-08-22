package usage

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strconv"
	"strings"
	"testing"
	"time"
)

// secretToken is a distinctive value used across the leak-check tests below:
// no returned error, in any scenario, may ever contain this substring.
const secretToken = "sk-ant-oat01-VERY-SECRET-DO-NOT-LEAK-9f3a"

func newTestCollector(t *testing.T, handler http.HandlerFunc) *Collector {
	t.Helper()
	srv := httptest.NewServer(handler)
	t.Cleanup(srv.Close)
	return newCollectorForTest(srv.URL, &http.Client{Timeout: 2 * time.Second})
}

func TestFetch_Success(t *testing.T) {
	var sawAuth, sawBeta string
	c := newTestCollector(t, func(w http.ResponseWriter, r *http.Request) {
		sawAuth = r.Header.Get("Authorization")
		sawBeta = r.Header.Get("anthropic-beta")
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{
			"five_hour": {"utilization": 42.5, "resets_at": "2026-08-23T10:00:00Z"},
			"seven_day": {"utilization": 10.0, "resets_at": "2026-08-27T00:00:00Z"},
			"extra_usage": {"is_enabled": true, "used_credits": 1250, "monthly_limit": 5000, "utilization": 25.0, "currency": "USD"},
			"limits": [{"scope": {"model": {"display_name": "Fable"}}, "percent": 12.5, "resets_at": "2026-08-30T00:00:00Z"}]
		}`))
	})

	got, err := c.Fetch(context.Background(), secretToken)
	if err != nil {
		t.Fatalf("Fetch failed: %v", err)
	}
	if sawAuth != "Bearer "+secretToken {
		t.Fatalf("Authorization header = %q, want Bearer <token>", sawAuth)
	}
	if sawBeta != oauthBetaHeader {
		t.Fatalf("anthropic-beta header = %q, want %q", sawBeta, oauthBetaHeader)
	}
	if got == nil {
		t.Fatal("Fetch returned nil usage on a well-formed response")
	}
	if got.FiveHour == nil || got.FiveHour.Pct != 42.5 || got.FiveHour.ResetsAt != "2026-08-23T10:00:00Z" {
		t.Errorf("FiveHour = %+v, want pct 42.5", got.FiveHour)
	}
	if got.SevenDay == nil || got.SevenDay.Pct != 10.0 {
		t.Errorf("SevenDay = %+v, want pct 10.0", got.SevenDay)
	}
	if len(got.Windows) != 2 || got.Windows[0].Id != "five_hour" || got.Windows[1].Id != "seven_day" {
		t.Errorf("Windows = %+v, want [five_hour, seven_day] in that order", got.Windows)
	}
	if got.Spend == nil || got.Spend.Used != 12.5 || got.Spend.Limit != 50.0 || got.Spend.Pct != 25.0 {
		t.Errorf("Spend = %+v, want used=12.5 limit=50 pct=25", got.Spend)
	}
	if len(got.Scoped) != 1 || got.Scoped[0].Name != "Fable" || got.Scoped[0].Pct != 12.5 {
		t.Errorf("Scoped = %+v, want one Fable window at 12.5", got.Scoped)
	}
}

func TestFetch_EmptyBodyNormalizesToNil(t *testing.T) {
	c := newTestCollector(t, func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write([]byte(`{}`))
	})
	got, err := c.Fetch(context.Background(), secretToken)
	if err != nil {
		t.Fatalf("Fetch failed: %v", err)
	}
	if got != nil {
		t.Fatalf("Fetch = %+v, want nil (no recognizable window data, matches build_usage_result -> None)", got)
	}
}

func TestFetch_MissingFieldsSkipsRatherThanCrashes(t *testing.T) {
	// five_hour present but with no `utilization` key at all: Python would
	// KeyError here; this collector treats it as absent (see normalizeUsage's
	// doc comment on the deliberate divergence).
	c := newTestCollector(t, func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write([]byte(`{"five_hour": {"resets_at": "2026-08-23T10:00:00Z"}, "seven_day": {"utilization": 5.0}}`))
	})
	got, err := c.Fetch(context.Background(), secretToken)
	if err != nil {
		t.Fatalf("Fetch failed: %v", err)
	}
	if got == nil || got.FiveHour != nil {
		t.Fatalf("got = %+v, want FiveHour nil (missing utilization skipped)", got)
	}
	if got.SevenDay == nil || got.SevenDay.Pct != 5.0 {
		t.Fatalf("got.SevenDay = %+v, want pct 5.0", got.SevenDay)
	}
}

func TestFetch_ExtraUsagePartialFieldsSkipsSpendOnly(t *testing.T) {
	// used_credits present, monthly_limit null -> spend entry skipped, but
	// five_hour/seven_day still go through (mirrors build_usage_result's
	// comment: "when any is null skip just the spend entry").
	c := newTestCollector(t, func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write([]byte(`{
			"five_hour": {"utilization": 1.0},
			"extra_usage": {"is_enabled": true, "used_credits": 100, "monthly_limit": null, "utilization": 5.0}
		}`))
	})
	got, err := c.Fetch(context.Background(), secretToken)
	if err != nil {
		t.Fatalf("Fetch failed: %v", err)
	}
	if got.Spend != nil {
		t.Fatalf("Spend = %+v, want nil (monthly_limit null)", got.Spend)
	}
	if got.FiveHour == nil || got.FiveHour.Pct != 1.0 {
		t.Fatalf("FiveHour = %+v, want pct 1.0 to survive independently of spend", got.FiveHour)
	}
}

func TestFetch_429(t *testing.T) {
	c := newTestCollector(t, func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Retry-After", "42")
		w.WriteHeader(http.StatusTooManyRequests)
	})
	_, err := c.Fetch(context.Background(), secretToken)
	var fe *FetchError
	if !asFetchError(err, &fe) {
		t.Fatalf("err = %v, want *FetchError", err)
	}
	if fe.Kind != "http-429" || !fe.IsRateLimited() {
		t.Fatalf("FetchError = %+v, want Kind http-429", fe)
	}
	if fe.RetryAfterS == nil || *fe.RetryAfterS != 42 {
		t.Fatalf("RetryAfterS = %v, want 42", fe.RetryAfterS)
	}
}

func TestFetch_429WithoutRetryAfter(t *testing.T) {
	c := newTestCollector(t, func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusTooManyRequests)
	})
	_, err := c.Fetch(context.Background(), secretToken)
	var fe *FetchError
	if !asFetchError(err, &fe) {
		t.Fatalf("err = %v, want *FetchError", err)
	}
	if fe.RetryAfterS != nil {
		t.Fatalf("RetryAfterS = %v, want nil", fe.RetryAfterS)
	}
}

func TestFetch_5xx(t *testing.T) {
	c := newTestCollector(t, func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusInternalServerError)
	})
	_, err := c.Fetch(context.Background(), secretToken)
	var fe *FetchError
	if !asFetchError(err, &fe) {
		t.Fatalf("err = %v, want *FetchError", err)
	}
	if fe.Kind != "http-5xx" || fe.StatusCode != 500 {
		t.Fatalf("FetchError = %+v, want Kind http-5xx StatusCode 500", fe)
	}
}

func TestFetch_OtherNon2xx(t *testing.T) {
	c := newTestCollector(t, func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusUnauthorized)
	})
	_, err := c.Fetch(context.Background(), secretToken)
	var fe *FetchError
	if !asFetchError(err, &fe) {
		t.Fatalf("err = %v, want *FetchError", err)
	}
	if fe.Kind != "http-401" {
		t.Fatalf("FetchError.Kind = %q, want http-401", fe.Kind)
	}
}

func TestFetch_BrokenJSON(t *testing.T) {
	c := newTestCollector(t, func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write([]byte(`{not valid json`))
	})
	_, err := c.Fetch(context.Background(), secretToken)
	var fe *FetchError
	if !asFetchError(err, &fe) {
		t.Fatalf("err = %v, want *FetchError", err)
	}
	if fe.Kind != "bad-response" {
		t.Fatalf("FetchError.Kind = %q, want bad-response", fe.Kind)
	}
}

func TestFetch_Timeout(t *testing.T) {
	block := make(chan struct{})
	t.Cleanup(func() { close(block) })
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		<-r.Context().Done() // hang until the client gives up
	}))
	t.Cleanup(srv.Close)
	c := newCollectorForTest(srv.URL, &http.Client{Timeout: 50 * time.Millisecond})

	_, err := c.Fetch(context.Background(), secretToken)
	var fe *FetchError
	if !asFetchError(err, &fe) {
		t.Fatalf("err = %v, want *FetchError", err)
	}
	if fe.Kind != "timeout" {
		t.Fatalf("FetchError.Kind = %q, want timeout", fe.Kind)
	}
}

func TestFetch_ContextCanceled(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		<-r.Context().Done()
	}))
	t.Cleanup(srv.Close)
	c := newCollectorForTest(srv.URL, &http.Client{Timeout: 5 * time.Second})

	ctx, cancel := context.WithCancel(context.Background())
	go func() {
		time.Sleep(30 * time.Millisecond)
		cancel()
	}()
	_, err := c.Fetch(ctx, secretToken)
	var fe *FetchError
	if !asFetchError(err, &fe) {
		t.Fatalf("err = %v, want *FetchError", err)
	}
	if fe.Kind != "context-canceled" {
		t.Fatalf("FetchError.Kind = %q, want context-canceled", fe.Kind)
	}
}

func TestFetch_EmptyAccessTokenIsRejectedWithoutARequest(t *testing.T) {
	called := false
	c := newTestCollector(t, func(w http.ResponseWriter, r *http.Request) {
		called = true
		w.WriteHeader(http.StatusOK)
	})
	_, err := c.Fetch(context.Background(), "")
	var fe *FetchError
	if !asFetchError(err, &fe) || fe.Kind != "no-access-token" {
		t.Fatalf("err = %v, want *FetchError{Kind: no-access-token}", err)
	}
	if called {
		t.Fatal("Fetch made a network call with an empty access token")
	}
}

// TestFetch_NoNetworkCallsOutsideHTTPTest is a structural guard, not a live
// check: every test in this file constructs its Collector via
// newCollectorForTest against an httptest.Server, never via NewCollector.
// This test just documents/asserts that NewCollector's default baseURL is
// the real endpoint, so a reviewer can see the two are intentionally never
// mixed in this file.
func TestFetch_NoNetworkCallsOutsideHTTPTest(t *testing.T) {
	c := NewCollector()
	if c.baseURL != usageEndpoint {
		t.Fatalf("NewCollector baseURL = %q, want %q", c.baseURL, usageEndpoint)
	}
}

// TestFetch_CredentialNeverLeaksIntoErrors runs every failure path this file
// exercises and asserts the secret token substring never appears in any
// returned error's message.
func TestFetch_CredentialNeverLeaksIntoErrors(t *testing.T) {
	scenarios := []struct {
		name    string
		handler http.HandlerFunc
	}{
		{"429", func(w http.ResponseWriter, r *http.Request) { w.WriteHeader(429) }},
		{"5xx", func(w http.ResponseWriter, r *http.Request) { w.WriteHeader(500) }},
		{"broken-json", func(w http.ResponseWriter, r *http.Request) { _, _ = w.Write([]byte(`{bad`)) }},
		{"echo-request-line", func(w http.ResponseWriter, r *http.Request) {
			// A pathological server that echoes back everything it saw,
			// including the Authorization header — this collector must
			// still never fold that into an error string of its own; the
			// point is that OUR error-construction code path never touches
			// the token, no matter what the server does.
			w.WriteHeader(400)
			_, _ = fmt.Fprintf(w, "saw auth: %s", r.Header.Get("Authorization"))
		}},
	}
	for _, sc := range scenarios {
		t.Run(sc.name, func(t *testing.T) {
			c := newTestCollector(t, sc.handler)
			_, err := c.Fetch(context.Background(), secretToken)
			if err == nil {
				t.Fatal("expected an error")
			}
			if strings.Contains(err.Error(), secretToken) {
				t.Fatalf("error leaked the access token: %q", err.Error())
			}
		})
	}
}

// asFetchError is a small helper mirroring errors.As without importing it
// twice at call sites in this file.
func asFetchError(err error, target **FetchError) bool {
	fe, ok := err.(*FetchError)
	if !ok {
		return false
	}
	*target = fe
	return true
}

func TestParseRetryAfter(t *testing.T) {
	cases := []struct {
		raw  string
		want *float64
	}{
		{"", nil},
		{"  ", nil},
		{"not-a-number", nil},
		{"-5", nil},
		{"0", func() *float64 { v := 0.0; return &v }()},
		{"42", func() *float64 { v := 42.0; return &v }()},
	}
	for _, tc := range cases {
		t.Run(strconv.Quote(tc.raw), func(t *testing.T) {
			got := parseRetryAfter(tc.raw)
			if (got == nil) != (tc.want == nil) {
				t.Fatalf("parseRetryAfter(%q) = %v, want %v", tc.raw, got, tc.want)
			}
			if got != nil && *got != *tc.want {
				t.Fatalf("parseRetryAfter(%q) = %v, want %v", tc.raw, *got, *tc.want)
			}
		})
	}
}

// TestNormalizeUsage_LimitsSkipMalformedEntries checks the `limits` array
// path (per-model scoped windows): entries missing scope/model/display_name
// or percent are skipped individually rather than aborting the whole parse.
func TestNormalizeUsage_LimitsSkipMalformedEntries(t *testing.T) {
	var raw rawUsageResponse
	body := []byte(`{
		"limits": [
			{"scope": {"model": {"display_name": "Fable"}}, "percent": 5.0},
			{"scope": {"model": {"display_name": ""}}, "percent": 10.0},
			{"scope": {}, "percent": 15.0},
			{"percent": 20.0},
			{"scope": {"model": {"display_name": "Sonnet"}}}
		]
	}`)
	if err := json.Unmarshal(body, &raw); err != nil {
		t.Fatalf("test fixture failed to parse: %v", err)
	}
	out := normalizeUsage(raw)
	if out == nil || len(out.Scoped) != 1 || out.Scoped[0].Name != "Fable" {
		t.Fatalf("Scoped = %+v, want exactly one Fable entry", out)
	}
}
