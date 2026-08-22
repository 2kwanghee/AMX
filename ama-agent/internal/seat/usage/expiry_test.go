package usage

import (
	"strings"
	"testing"
	"time"
)

func credJSON(expiresAtMS *int64, accessToken, refreshToken string) []byte {
	var expires string
	if expiresAtMS != nil {
		expires = `"expiresAt":` + itoa(*expiresAtMS) + `,`
	}
	return []byte(`{"claudeAiOauth":{` + expires + `"accessToken":"` + accessToken + `","refreshToken":"` + refreshToken + `"}}`)
}

func itoa(v int64) string {
	if v == 0 {
		return "0"
	}
	neg := v < 0
	if neg {
		v = -v
	}
	var buf [20]byte
	i := len(buf)
	for v > 0 {
		i--
		buf[i] = byte('0' + v%10)
		v /= 10
	}
	if neg {
		i--
		buf[i] = '-'
	}
	return string(buf[i:])
}

// --- JudgeIdleExpiry (C1: token_expired vs relogin_required must not conflate) ---

func TestJudgeIdleExpiry_NotYetExpired_HasRefreshToken(t *testing.T) {
	now := time.Date(2026, 8, 23, 12, 0, 0, 0, time.UTC)
	exp := now.Add(1 * time.Hour).UnixMilli()
	status := JudgeIdleExpiry(credJSON(&exp, "at-real", "rt-real"), now)
	if !status.Judgeable || status.Status != "" {
		t.Fatalf("status = %+v, want Judgeable=true Status=\"\" (healthy, not yet expired)", status)
	}
}

// A `claude setup token` account: long-lived accessToken, NO refreshToken,
// not yet expired. Must read as healthy (Status ""), never
// StatusReloginRequired merely for lacking a refresh token — see
// internal/provider/claude.Driver.Fingerprint's doc on this account shape,
// and JudgeIdleExpiry's doc (C1) on why this exact case is the false
// positive the first version of this function would have produced for.
func TestJudgeIdleExpiry_NotYetExpired_NoRefreshToken_IsHealthy(t *testing.T) {
	now := time.Date(2026, 8, 23, 12, 0, 0, 0, time.UTC)
	exp := now.Add(1 * time.Hour).UnixMilli()
	status := JudgeIdleExpiry(credJSON(&exp, "at-real", ""), now)
	if !status.Judgeable || status.Status != "" {
		t.Fatalf("status = %+v, want Judgeable=true Status=\"\" (setup-token shape, still valid)", status)
	}
}

// The core C1 fix: an expired access token with a refresh token present is
// TRANSIENT (token_expired), not a quarantine signal.
func TestJudgeIdleExpiry_Expired_HasRefreshToken_IsTokenExpiredNotRelogin(t *testing.T) {
	now := time.Date(2026, 8, 23, 12, 0, 0, 0, time.UTC)
	exp := now.Add(-1 * time.Hour).UnixMilli()
	status := JudgeIdleExpiry(credJSON(&exp, "at-real", "rt-real"), now)
	if !status.Judgeable || status.Status != StatusTokenExpired {
		t.Fatalf("status = %+v, want Judgeable=true Status=%q", status, StatusTokenExpired)
	}
}

// Expired AND no refresh token at all: nothing can recover it without a
// human re-login. This is the one relogin_required case decidable without a
// network call.
func TestJudgeIdleExpiry_Expired_NoRefreshToken_IsReloginRequired(t *testing.T) {
	now := time.Date(2026, 8, 23, 12, 0, 0, 0, time.UTC)
	exp := now.Add(-1 * time.Hour).UnixMilli()
	status := JudgeIdleExpiry(credJSON(&exp, "at-real", ""), now)
	if !status.Judgeable || status.Status != StatusReloginRequired {
		t.Fatalf("status = %+v, want Judgeable=true Status=%q", status, StatusReloginRequired)
	}
}

func TestJudgeIdleExpiry_WithinBufferCountsAsExpired(t *testing.T) {
	now := time.Date(2026, 8, 23, 12, 0, 0, 0, time.UTC)
	exp := now.Add(4 * time.Minute).UnixMilli() // inside the 5-minute ExpiryBufferMS
	status := JudgeIdleExpiry(credJSON(&exp, "at-real", "rt-real"), now)
	if !status.Judgeable || status.Status != StatusTokenExpired {
		t.Fatalf("status = %+v, want Judgeable=true Status=%q (within buffer)", status, StatusTokenExpired)
	}
}

func TestJudgeIdleExpiry_JustOutsideBufferIsNotExpired(t *testing.T) {
	now := time.Date(2026, 8, 23, 12, 0, 0, 0, time.UTC)
	exp := now.Add(5*time.Minute + time.Second).UnixMilli() // just outside ExpiryBufferMS
	status := JudgeIdleExpiry(credJSON(&exp, "at-real", "rt-real"), now)
	if !status.Judgeable || status.Status != "" {
		t.Fatalf("status = %+v, want Judgeable=true Status=\"\"", status)
	}
}

func TestJudgeIdleExpiry_NoExpiresAtIsUnjudgeable(t *testing.T) {
	now := time.Date(2026, 8, 23, 12, 0, 0, 0, time.UTC)
	status := JudgeIdleExpiry(credJSON(nil, "at-real", "rt-real"), now)
	if status.Judgeable {
		t.Fatal("Judgeable = true, want false (no expiresAt claim)")
	}
	if status.Status != "" {
		t.Fatalf("Status = %q, want \"\" when unjudgeable (must not false-positive quarantine)", status.Status)
	}
}

func TestJudgeIdleExpiry_UnparseableJSONIsUnjudgeable(t *testing.T) {
	now := time.Date(2026, 8, 23, 12, 0, 0, 0, time.UTC)
	status := JudgeIdleExpiry([]byte(`not json at all`), now)
	if status.Judgeable || status.Status != "" {
		t.Fatalf("status = %+v, want Judgeable=false Status=\"\"", status)
	}
}

func TestJudgeIdleExpiry_MissingClaudeAiOauthBlockIsUnjudgeable(t *testing.T) {
	now := time.Date(2026, 8, 23, 12, 0, 0, 0, time.UTC)
	status := JudgeIdleExpiry([]byte(`{"apiKey":"sk-something"}`), now)
	if status.Judgeable {
		t.Fatal("Judgeable = true, want false (no claudeAiOauth block at all)")
	}
}

// --- ParseCredentialExpiry: never returns token bytes ---

func TestParseCredentialExpiry_NeverReturnsTokenBytes(t *testing.T) {
	exp := int64(1234567890000)
	cred := credJSON(&exp, "sk-ant-oat01-SECRET-ACCESS", "sk-ant-ort01-SECRET-REFRESH")
	got, err := ParseCredentialExpiry(cred)
	if err != nil {
		t.Fatalf("ParseCredentialExpiry failed: %v", err)
	}
	if !got.HasAccessToken || !got.HasRefreshToken {
		t.Fatalf("got = %+v, want both HasAccessToken and HasRefreshToken true", got)
	}
	dump := boolToStr(got.HasAccessToken) + boolToStr(got.HasRefreshToken)
	if strings.Contains(dump, "SECRET") {
		t.Fatalf("dump unexpectedly carries token material: %q", dump)
	}
}

func boolToStr(b bool) string {
	if b {
		return "true"
	}
	return "false"
}

func TestCredentialExpiry_IsExpired_UnknownIsNotExpired(t *testing.T) {
	now := time.Date(2026, 8, 23, 12, 0, 0, 0, time.UTC)
	var c CredentialExpiry
	if c.IsExpired(now) {
		t.Fatal("zero-value CredentialExpiry.IsExpired = true, want false (no expiresAt)")
	}
}

// --- ExtractOAuthTokens (C2) ---

func TestExtractOAuthTokens_ReturnsBothTokensAndExpiry(t *testing.T) {
	now := time.Date(2026, 8, 23, 12, 0, 0, 0, time.UTC)
	exp := now.Add(1 * time.Hour).UnixMilli()
	access, refresh, expiry, err := ExtractOAuthTokens(credJSON(&exp, "at-123", "rt-456"))
	if err != nil {
		t.Fatalf("ExtractOAuthTokens failed: %v", err)
	}
	if access != "at-123" || refresh != "rt-456" {
		t.Fatalf("access=%q refresh=%q, want at-123/rt-456", access, refresh)
	}
	if expiry.ExpiresAtMS == nil || *expiry.ExpiresAtMS != exp {
		t.Fatalf("expiry = %+v, want ExpiresAtMS=%d", expiry, exp)
	}
	if !expiry.HasAccessToken || !expiry.HasRefreshToken {
		t.Fatalf("expiry = %+v, want both Has* true", expiry)
	}
}

func TestExtractOAuthTokens_MissingFieldsReturnEmptyStringsNotError(t *testing.T) {
	access, refresh, expiry, err := ExtractOAuthTokens([]byte(`{"claudeAiOauth":{}}`))
	if err != nil {
		t.Fatalf("ExtractOAuthTokens failed: %v", err)
	}
	if access != "" || refresh != "" {
		t.Fatalf("access=%q refresh=%q, want both empty", access, refresh)
	}
	if expiry.HasAccessToken || expiry.HasRefreshToken || expiry.ExpiresAtMS != nil {
		t.Fatalf("expiry = %+v, want all zero-ish", expiry)
	}
}

func TestExtractOAuthTokens_UnparseableJSONErrors(t *testing.T) {
	_, _, _, err := ExtractOAuthTokens([]byte(`not json`))
	if err == nil {
		t.Fatal("expected an error for unparseable JSON")
	}
}

// TestExtractOAuthTokens_NeverLeaksIntoErrors is the leak-prevention test C2
// asked for: the one function in this package that returns token material
// must never fold that material into an error value, for any input shape
// (malformed structure around a real-looking token, wrong types, etc.).
func TestExtractOAuthTokens_NeverLeaksIntoErrors(t *testing.T) {
	const secret = "sk-ant-oat01-EXTRACT-LEAK-CHECK-7d2e"
	inputs := [][]byte{
		[]byte(`{"claudeAiOauth":{"accessToken":"` + secret + `","refreshToken":"rt","expiresAt":"not-a-number"}}`),
		[]byte(`{"claudeAiOauth":"` + secret + `"}`),                // wrong type for the whole block
		[]byte(`{"claudeAiOauth":{"accessToken":` + `12345` + `}}`), // wrong type for accessToken itself
	}
	for _, in := range inputs {
		_, _, _, err := ExtractOAuthTokens(in)
		if err != nil && strings.Contains(err.Error(), secret) {
			t.Fatalf("error leaked token material: %q", err.Error())
		}
	}
}
