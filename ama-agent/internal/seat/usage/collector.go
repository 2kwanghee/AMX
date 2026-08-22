package usage

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strconv"
	"strings"
	"time"

	"github.com/2kwanghee/AMX/ama-agent/internal/provider"
)

// usageEndpoint is Claude's usage API, ported from
// tsamx.oauth.request_usage_data's hardcoded URL.
const usageEndpoint = "https://api.anthropic.com/api/oauth/usage"

// oauthBetaHeader is the beta flag the endpoint requires, ported from
// tsamx.oauth.OAUTH_BETA_HEADER.
const oauthBetaHeader = "oauth-2025-04-20"

// requestUserAgent is sent on every Fetch (M5, P4 review). poll_policy.py's
// module docstring measures the endpoint's budget as scoped to "identity ×
// UA-class" — a dimension of the measured shape, not just the account/token.
// Leaving this unset (Go's http.Client default, "Go-http-client/1.1") would
// silently move this collector's traffic into a UA-class nobody has ever
// measured the 429 behavior of, under either the account-scoped or the
// token-scoped regime poll_policy.py describes.
//
// Chosen value: tsamx's own "tsamx/1.0" (oauth.py's request_usage_data),
// not a distinct new string. Reasoning: this package's requests are, in
// every case this constant matters (P4 is inert; once wired, P6's shadow
// run reads the SAME accounts tsamx already polls — see this file's package
// doc, M4), traffic against an identity tsamx already has measured,
// established behavior against under exactly this UA string. A fresh UA
// value would open a SECOND, never-measured UA-class bucket for the same
// account with unknown 429 behavior — introducing an unmeasured input
// exactly like leaving it unset would, just under a different label. Reusing
// tsamx's string keeps this collector's traffic inside the measured
// envelope instead of adding one. This is a client identification string
// only (not a credential, not an authentication claim) shared between two
// pieces of software this repo owns and operates on the same account's
// behalf, so there is no impersonation of a third party.
//
// This choice does NOT resolve M4's double-consumption risk (a shared UA
// string does not share a request budget) — it only keeps the UA dimension
// of the measurement from being silently invalidated. Revisit if either
// engine's UA is ever used server-side to size a SEPARATE per-engine
// budget, which would flip this reasoning (that has not been measured
// either way).
const requestUserAgent = "tsamx/1.0"

// defaultTimeout bounds a single Fetch call end-to-end (connect, TLS, first
// byte, body read). tsamx's urllib call used a 5s timeout
// (request_usage_data); this collector uses a slightly wider default because
// http.Client.Timeout also covers redirects and body drain that urllib's
// timeout did not have to, but it remains a hard, short bound — never
// unbounded.
const defaultTimeout = 10 * time.Second

// maxBodyBytes caps how much of a response body Fetch will read, so a
// misbehaving or malicious endpoint cannot exhaust memory via an unbounded
// response.
const maxBodyBytes = 1 << 20 // 1 MiB

// Collector fetches usage from Claude's oauth/usage endpoint. The zero value
// is not usable; construct with NewCollector. A Collector is safe for
// concurrent use (http.Client is).
type Collector struct {
	client  *http.Client
	baseURL string
}

// NewCollector returns a Collector using the default transport
// (http.DefaultTransport — proxy- and TLS-config-aware via the standard
// environment variables/system trust store) with an explicit request
// timeout. Building a bespoke Transport was deliberately avoided: the design
// note asks for "기본 트랜스포트를 쓰되 타임아웃을 명시" (use the default
// transport, but state a timeout explicitly).
func NewCollector() *Collector {
	return &Collector{
		client:  &http.Client{Timeout: defaultTimeout, Transport: http.DefaultTransport},
		baseURL: usageEndpoint,
	}
}

// newCollectorForTest is used only by collector_test.go to point Fetch at an
// httptest.Server instead of the real endpoint. Not exported: production
// callers must never be able to redirect this collector away from Anthropic.
func newCollectorForTest(baseURL string, client *http.Client) *Collector {
	if client == nil {
		client = &http.Client{Timeout: defaultTimeout}
	}
	return &Collector{client: client, baseURL: baseURL}
}

// FetchError classifies a failed Fetch, mirroring
// tsamx.oauth._classify_usage_error's (kind, retry_after_s) shape closely
// enough that a caller porting tsamx's failure-handling logic (recent-429
// tracking, edge backoff on Retry-After: 0) can switch on Kind the same way.
// FetchError NEVER carries the access token or any request header — only the
// HTTP status/kind and the server's own Retry-After value.
type FetchError struct {
	// Kind is one of "http-429", "http-5xx", "http-<code>" (any other
	// non-2xx status), "timeout", "context-canceled", "network", or
	// "bad-response" (a 200 whose body was not the expected JSON shape).
	Kind string
	// StatusCode is the HTTP status code, or 0 for a non-HTTP failure
	// (timeout/network/context-canceled).
	StatusCode int
	// RetryAfterS is the server's Retry-After header value in seconds, when
	// present and parseable (HTTP-date Retry-After forms are not parsed,
	// matching tsamx's "rare enough to ignore" note) — nil otherwise.
	RetryAfterS *float64
}

func (e *FetchError) Error() string {
	if e.RetryAfterS != nil {
		return fmt.Sprintf("usage fetch failed: %s (retry-after %.0fs)", e.Kind, *e.RetryAfterS)
	}
	return fmt.Sprintf("usage fetch failed: %s", e.Kind)
}

// IsRateLimited reports whether err is a 429 FetchError.
func (e *FetchError) IsRateLimited() bool { return e != nil && e.Kind == "http-429" }

// Fetch calls the usage endpoint with accessToken and returns the normalized
// usage snapshot, or a *FetchError describing what went wrong. accessToken
// is placed only in the Authorization request header — it is never logged,
// never included in any error message, and never echoed back in Fetch's
// return value.
//
// A successful round trip whose body carries no recognizable window data
// (e.g. `{}`) returns (nil, nil) — mirrors tsamx.oauth.build_usage_result
// returning None, which callers treat as "measured, but nothing to report"
// rather than an error.
func (c *Collector) Fetch(ctx context.Context, accessToken string) (*provider.Usage, error) {
	if accessToken == "" {
		return nil, &FetchError{Kind: "no-access-token"}
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, c.baseURL, nil)
	if err != nil {
		return nil, &FetchError{Kind: "bad-request"}
	}
	req.Header.Set("Authorization", "Bearer "+accessToken)
	req.Header.Set("anthropic-beta", oauthBetaHeader)
	req.Header.Set("User-Agent", requestUserAgent)

	resp, err := c.client.Do(req)
	if err != nil {
		return nil, classifyTransportError(ctx, err)
	}
	defer resp.Body.Close()

	body, readErr := io.ReadAll(io.LimitReader(resp.Body, maxBodyBytes))

	if resp.StatusCode == http.StatusTooManyRequests {
		return nil, &FetchError{
			Kind:        "http-429",
			StatusCode:  resp.StatusCode,
			RetryAfterS: parseRetryAfter(resp.Header.Get("Retry-After")),
		}
	}
	if resp.StatusCode >= 500 {
		return nil, &FetchError{Kind: "http-5xx", StatusCode: resp.StatusCode}
	}
	if resp.StatusCode != http.StatusOK {
		return nil, &FetchError{Kind: fmt.Sprintf("http-%d", resp.StatusCode), StatusCode: resp.StatusCode}
	}
	if readErr != nil {
		return nil, &FetchError{Kind: "network", StatusCode: resp.StatusCode}
	}

	var raw rawUsageResponse
	if err := json.Unmarshal(body, &raw); err != nil {
		return nil, &FetchError{Kind: "bad-response", StatusCode: resp.StatusCode}
	}
	return normalizeUsage(raw), nil
}

// classifyTransportError maps a client.Do transport-level error (no HTTP
// response at all) to a FetchError, mirroring
// tsamx.oauth._classify_usage_error's timeout/network split. It never
// includes err's own message verbatim in the returned Kind (that message can
// embed the request URL including query — this endpoint has none, but the
// convention is kept for any future endpoint that might) — only a coarse
// classification.
func classifyTransportError(ctx context.Context, err error) *FetchError {
	if ctx.Err() != nil && errors.Is(err, ctx.Err()) {
		if errors.Is(ctx.Err(), context.DeadlineExceeded) {
			return &FetchError{Kind: "timeout"}
		}
		return &FetchError{Kind: "context-canceled"}
	}
	var urlErr *url.Error
	if errors.As(err, &urlErr) && urlErr.Timeout() {
		return &FetchError{Kind: "timeout"}
	}
	return &FetchError{Kind: "network"}
}

// parseRetryAfter parses an HTTP Retry-After header's seconds form (the only
// form tsamx.oauth._classify_usage_error parses; HTTP-date is ignored as
// "rare enough to ignore" there too). Returns nil for an empty, negative, or
// unparseable value.
func parseRetryAfter(raw string) *float64 {
	raw = strings.TrimSpace(raw)
	if raw == "" {
		return nil
	}
	v, err := strconv.ParseFloat(raw, 64)
	if err != nil {
		return nil
	}
	// A negative Retry-After is clamped to 0, NOT discarded: tsamx does
	// max(0.0, float(raw)) (oauth.py:358), and with the failure planner in
	// place the two paths diverge otherwise — a 429 carrying "-1" would take
	// the edge backoff here but the ordinary 429 floor there.
	if v < 0 {
		v = 0
	}
	return &v
}

// rawUsageResponse is the subset of the `/api/oauth/usage` JSON schema this
// collector consumes, ported from tsamx.oauth.build_usage_result
// (tsamx/src/tsamx/oauth.py:397-419 as of this port) — NOT from that
// function's docstring or any comment describing it, which (per
// code-truth-verification) is not trusted as the schema source; the field
// reads below were checked against the function body itself.
type rawUsageResponse struct {
	FiveHour   *rawWindow     `json:"five_hour"`
	SevenDay   *rawWindow     `json:"seven_day"`
	ExtraUsage *rawExtraUsage `json:"extra_usage"`
	Limits     []rawLimit     `json:"limits"`
}

type rawWindow struct {
	Utilization *float64 `json:"utilization"`
	ResetsAt    string   `json:"resets_at"`
}

type rawExtraUsage struct {
	IsEnabled    bool     `json:"is_enabled"`
	UsedCredits  *float64 `json:"used_credits"`
	MonthlyLimit *float64 `json:"monthly_limit"`
	Utilization  *float64 `json:"utilization"`
	Currency     string   `json:"currency"`
	ResetsAt     string   `json:"resets_at"`
}

type rawLimit struct {
	Scope    *rawLimitScope `json:"scope"`
	Percent  *float64       `json:"percent"`
	ResetsAt string         `json:"resets_at"`
}

type rawLimitScope struct {
	Model *rawLimitModel `json:"model"`
}

type rawLimitModel struct {
	DisplayName string `json:"display_name"`
}

// normalizeUsage projects rawUsageResponse onto provider.Usage, ported from
// tsamx.oauth.build_usage_result. Deliberate, documented divergences from
// the Python function:
//
//   - Python indexes h5["utilization"]/d7["utilization"] directly and would
//     raise KeyError if `five_hour`/`seven_day` were present but missing
//     `utilization` (a malformed-upstream case that, per the measured
//     history, has not been observed). Go has no such implicit crash mode;
//     this function instead treats a present-but-fieldless window the same
//     as an absent one (skipped), which is strictly safer and changes
//     nothing for a well-formed response.
//   - `currency` defaults to "USD" only when the key is absent, matching
//     Python's `eu.get("currency", "USD")` (an explicit empty string is
//     passed through as-is, matching Python's dict.get returning "" for an
//     explicit "" value — neither language substitutes the default there).
//
// Both windows are filled into BOTH the legacy FiveHour/SevenDay fields and
// the canonical Windows[] list (the "dual record" the provider.Usage doc
// comment describes), in five_hour-then-seven_day order, matching what
// internal/reporter already expects from a driver.
//
// Id/WindowMinutes are set ONLY on the Windows[] copies, never on the
// FiveHour/SevenDay struct fields themselves — this mirrors the existing
// contract exactly: provider.Window's own doc comment ("Id and WindowMinutes
// are set only on entries carried in Usage.Windows... the legacy
// FiveHour/SevenDay fields leave them zero-valued") and
// internal/tsamx/exec.go's claudeWindows (which builds the canonical list by
// copying FiveHour/SevenDay and stamping id/windowMinutes onto the COPIES,
// leaving the originals untouched). An earlier draft of this function
// stamped both fields identically, which would read as a genuine structural
// difference from every other driver's output to any future P6 shadow-run
// comparison between this collector and the tsamx bridge (P4 review, m1).
func normalizeUsage(raw rawUsageResponse) *provider.Usage {
	out := &provider.Usage{}

	if raw.FiveHour != nil && raw.FiveHour.Utilization != nil {
		out.FiveHour = &provider.Window{Pct: *raw.FiveHour.Utilization, ResetsAt: raw.FiveHour.ResetsAt}
	}
	if raw.SevenDay != nil && raw.SevenDay.Utilization != nil {
		out.SevenDay = &provider.Window{Pct: *raw.SevenDay.Utilization, ResetsAt: raw.SevenDay.ResetsAt}
	}
	out.Windows = claudeWindows(out.FiveHour, out.SevenDay)

	if eu := raw.ExtraUsage; eu != nil && eu.IsEnabled &&
		eu.UsedCredits != nil && eu.MonthlyLimit != nil && eu.Utilization != nil {
		currency := eu.Currency
		if currency == "" {
			currency = "USD"
		}
		out.Spend = &provider.Spend{
			Used:     *eu.UsedCredits / 100,
			Limit:    *eu.MonthlyLimit / 100,
			Pct:      *eu.Utilization,
			Currency: currency,
			ResetsAt: eu.ResetsAt,
		}
	}

	for _, lim := range raw.Limits {
		if lim.Scope == nil || lim.Scope.Model == nil {
			continue
		}
		name := lim.Scope.Model.DisplayName
		if name == "" || lim.Percent == nil {
			continue
		}
		out.Scoped = append(out.Scoped, provider.ScopedWindow{
			Name:     name,
			Pct:      *lim.Percent,
			ResetsAt: lim.ResetsAt,
		})
	}

	if out.FiveHour == nil && out.SevenDay == nil && out.Spend == nil && len(out.Scoped) == 0 {
		return nil // mirrors build_usage_result's `return result if result else None`
	}
	return out
}

// claudeWindows projects fiveHour/sevenDay onto the neutral Windows list,
// tagging COPIES with their id and window length in minutes — never
// mutating the originals. Deliberately mirrors
// internal/tsamx/exec.go's claudeWindows (same name, same shape, same
// order) rather than sharing code with it: that function is in
// internal/tsamx, which this package must not import (P4 stays
// provider/policy-only and inert), and duplicating six lines is cheaper
// than creating a cross-package dependency for them.
func claudeWindows(fiveHour, sevenDay *provider.Window) []provider.Window {
	var out []provider.Window
	if fiveHour != nil {
		w := *fiveHour
		w.Id, w.WindowMinutes = "five_hour", fiveHourWindowMinutes
		out = append(out, w)
	}
	if sevenDay != nil {
		w := *sevenDay
		w.Id, w.WindowMinutes = "seven_day", sevenDayWindowMinutes
		out = append(out, w)
	}
	return out
}
