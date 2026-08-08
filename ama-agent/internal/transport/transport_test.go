package transport

import "testing"

// TestSecurityDialOptionFailsClosed: with neither a TLS CA nor the insecure
// opt-in, the agent refuses to build a dial option rather than defaulting to
// plaintext (§7 in-transit).
func TestSecurityDialOptionFailsClosed(t *testing.T) {
	t.Setenv(EnvTLSCA, "")
	t.Setenv(EnvAllowInsecure, "")
	if _, err := SecurityDialOption(); err == nil {
		t.Fatal("expected an error when neither TLS nor the insecure opt-in is set")
	}
}

// TestSecurityDialOptionInsecureOptIn: the explicit opt-in yields a usable
// (insecure) dial option, which is how local E2E keeps working.
func TestSecurityDialOptionInsecureOptIn(t *testing.T) {
	t.Setenv(EnvTLSCA, "")
	t.Setenv(EnvAllowInsecure, "1")
	opt, err := SecurityDialOption()
	if err != nil {
		t.Fatalf("insecure opt-in errored: %v", err)
	}
	if opt == nil {
		t.Fatal("insecure opt-in returned a nil dial option")
	}
}
