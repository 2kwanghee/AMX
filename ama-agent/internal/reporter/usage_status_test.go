package reporter

import (
	"testing"

	"github.com/2kwanghee/AMX/ama-agent/internal/seat/usage"
)

// TestUsageStatusQuarantined_MatchesSeatUsagePackage pins the ONE literal
// contract two independent packages must agree on byte-for-byte:
// usageStatusQuarantined here (this file's package, reporter.go:42 — what
// actually drives PoolSummary.Quarantined) and usage.StatusReloginRequired
// (internal/seat/usage, the future P5 seat engine's local judgement of an
// idle profile's credential — see that package's expiry.go).
//
// The dependency direction is deliberate (P4 review, M3): internal/seat/usage
// must NOT import internal/reporter (P4 stays provider/policy-only and
// inert; reporter is already wired), so the two packages cannot share the
// constant directly. This test lives on the reporter side instead, asserting
// EQUALITY against usage's independently exported constant — the opposite,
// and materially stronger, check from a version that used to live in
// internal/seat/usage and only compared usage.StatusReloginRequired against
// its own string literal (a tautology that could never catch drift on
// either side). If either literal ever changes without the other, this test
// fails here, not silently at runtime as an account that should have been
// quarantined but was not (or vice versa).
func TestUsageStatusQuarantined_MatchesSeatUsagePackage(t *testing.T) {
	if usageStatusQuarantined != usage.StatusReloginRequired {
		t.Fatalf("reporter.usageStatusQuarantined = %q, internal/seat/usage.StatusReloginRequired = %q — must match",
			usageStatusQuarantined, usage.StatusReloginRequired)
	}
}
