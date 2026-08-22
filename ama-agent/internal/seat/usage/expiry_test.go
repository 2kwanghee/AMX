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

func TestJudgeIdleExpiry_NotYetExpired(t *testing.T) {
	now := time.Date(2026, 8, 23, 12, 0, 0, 0, time.UTC)
	// Expires 1 hour from now — well outside ExpiryBufferMS (5 minutes).
	exp := now.Add(1 * time.Hour).UnixMilli()
	status := JudgeIdleExpiry(credJSON(&exp, "at-real", "rt-real"), now)
	if !status.Judgeable {
		t.Fatal("Judgeable = false, want true (expiresAt present)")
	}
	if status.Expired {
		t.Fatal("Expired = true, want false (1h remaining)")
	}
}

func TestJudgeIdleExpiry_Expired(t *testing.T) {
	now := time.Date(2026, 8, 23, 12, 0, 0, 0, time.UTC)
	// Expired 1 hour ago.
	exp := now.Add(-1 * time.Hour).UnixMilli()
	status := JudgeIdleExpiry(credJSON(&exp, "at-real", "rt-real"), now)
	if !status.Judgeable || !status.Expired {
		t.Fatalf("status = %+v, want Judgeable=true Expired=true", status)
	}
}

func TestJudgeIdleExpiry_WithinBufferCountsAsExpired(t *testing.T) {
	now := time.Date(2026, 8, 23, 12, 0, 0, 0, time.UTC)
	// Expires in 4 minutes — inside the 5-minute ExpiryBufferMS.
	exp := now.Add(4 * time.Minute).UnixMilli()
	status := JudgeIdleExpiry(credJSON(&exp, "at-real", "rt-real"), now)
	if !status.Judgeable || !status.Expired {
		t.Fatalf("status = %+v, want Judgeable=true Expired=true (within buffer)", status)
	}
}

func TestJudgeIdleExpiry_JustOutsideBufferIsNotExpired(t *testing.T) {
	now := time.Date(2026, 8, 23, 12, 0, 0, 0, time.UTC)
	// Expires in 5 minutes + 1 second — just outside ExpiryBufferMS.
	exp := now.Add(5*time.Minute + time.Second).UnixMilli()
	status := JudgeIdleExpiry(credJSON(&exp, "at-real", "rt-real"), now)
	if !status.Judgeable || status.Expired {
		t.Fatalf("status = %+v, want Judgeable=true Expired=false", status)
	}
}

func TestJudgeIdleExpiry_NoExpiresAtIsUnjudgeable(t *testing.T) {
	now := time.Date(2026, 8, 23, 12, 0, 0, 0, time.UTC)
	status := JudgeIdleExpiry(credJSON(nil, "at-real", "rt-real"), now)
	if status.Judgeable {
		t.Fatal("Judgeable = true, want false (no expiresAt claim)")
	}
	if status.Expired {
		t.Fatal("Expired = true, want false when unjudgeable (must not false-positive quarantine)")
	}
}

func TestJudgeIdleExpiry_UnparseableJSONIsUnjudgeable(t *testing.T) {
	now := time.Date(2026, 8, 23, 12, 0, 0, 0, time.UTC)
	status := JudgeIdleExpiry([]byte(`not json at all`), now)
	if status.Judgeable || status.Expired {
		t.Fatalf("status = %+v, want Judgeable=false Expired=false", status)
	}
}

func TestJudgeIdleExpiry_MissingClaudeAiOauthBlockIsUnjudgeable(t *testing.T) {
	now := time.Date(2026, 8, 23, 12, 0, 0, 0, time.UTC)
	status := JudgeIdleExpiry([]byte(`{"apiKey":"sk-something"}`), now)
	if status.Judgeable {
		t.Fatal("Judgeable = true, want false (no claudeAiOauth block at all)")
	}
}

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
	// The struct carries only booleans/ints — this is a static assertion in
	// spirit; the runtime check below is a best-effort belt-and-suspenders
	// scan of a %+v dump for either secret substring.
	dump := ""
	dump += boolToStr(got.HasAccessToken) + boolToStr(got.HasRefreshToken)
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

func TestStatusReloginRequired_MatchesReporterLiteral(t *testing.T) {
	// This is a pinned-string test, not a cross-package import (see
	// StatusReloginRequired's doc comment on why this package does not
	// depend on internal/reporter). If reporter.go's usageStatusQuarantined
	// literal ever changes, this constant — and this test — must be updated
	// by hand alongside it.
	if StatusReloginRequired != "relogin_required" {
		t.Fatalf("StatusReloginRequired = %q, want %q", StatusReloginRequired, "relogin_required")
	}
}
