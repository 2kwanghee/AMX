// 이 파일은 엔진 교체 시 양쪽이 통과해야 하는 계약을 고정한다: tsamx list --json의
// disabled/usageStatus/usage 리터럴이 PoolSummary로 유도되는 규칙(시트 엔진 재설계
// 기획서 docs/design-notes/seat-engine-plan.md P0-A 계약5) 가운데, 기존
// reporter_test.go가 다루지 않는 한 조각만 고정한다: disabled 계정은 active/eligible
// 집계에서 빠지면서도 all_exhausted를 해소한다는, reporter.go의 다소 직관에 반하는
// 분기다. 나머지(unmeasured 제외, relogin_required 격리 집계, claude 프로바이더
// 한정 집계 등)는 이미 reporter_test.go가 고정하고 있으므로 중복 작성하지 않는다.
package reporter

import (
	"context"
	"testing"
	"time"

	"github.com/2kwanghee/AMX/ama-agent/internal/provider"
	"github.com/2kwanghee/AMX/ama-agent/internal/tsamx"
	amxv1 "github.com/2kwanghee/AMX/contracts/gen/go"
)

// TestContractDisabledExcludedButRelievesAllExhausted locks 계약5's disabled
// branch (reporter.go BuildUsageReport: `case row.Disabled: relievesExhaustion =
// true`, no active/eligible/measured increment). A pool with one measured,
// over-threshold account and one disabled account must report AllExhausted =
// false — the disabled account "relieves" the signal even though it is itself
// unusable — because all_exhausted means "every ROTATION CANDIDATE is exhausted"
// and a disabled account is not a rotation candidate at all.
func TestContractDisabledExcludedButRelievesAllExhausted(t *testing.T) {
	ctx := context.Background()
	f := tsamx.NewFake()
	if err := f.Add(ctx, provider.AddRequest{Email: "hot@x.io", Enable: true}); err != nil {
		t.Fatal(err)
	}
	if err := f.Add(ctx, provider.AddRequest{Email: "off@x.io", Enable: true}); err != nil {
		t.Fatal(err)
	}
	if err := f.Switch(ctx, "hot@x.io"); err != nil {
		t.Fatal(err)
	}
	// hot: measured and over the 95pct exhaustion threshold.
	f.SetUsage("hot@x.io", &provider.Usage{FiveHour: &provider.Window{Pct: 99}})
	// off: disabled, no usage ever fetched (mirrors a real disabled/never-polled slot).
	if err := f.Disable(ctx, "off@x.io"); err != nil {
		t.Fatal(err)
	}

	r := New("ama_test", bridgeMap(f), func() time.Time { return time.Unix(1700000000, 0) })
	rep, err := r.BuildUsageReport(ctx, amxv1.UsageReport_TRIGGER_SCHEDULE)
	if err != nil {
		t.Fatal(err)
	}

	if rep.PoolSummary.Total != 2 {
		t.Fatalf("total = %d, want 2", rep.PoolSummary.Total)
	}
	if rep.PoolSummary.Active != 1 {
		t.Fatalf("active = %d, want 1 (disabled account excluded)", rep.PoolSummary.Active)
	}
	if rep.PoolSummary.Eligible != 0 {
		t.Fatalf("eligible = %d, want 0 (hot is over threshold, off is disabled)", rep.PoolSummary.Eligible)
	}
	if rep.PoolSummary.AllExhausted {
		t.Fatal("allExhausted should be false: the disabled account relieves it even though it is itself unusable")
	}

	var off *amxv1.AccountUsage
	for _, au := range rep.GetAccounts() {
		if au.GetAccount().GetEmail() == "off@x.io" {
			off = au
		}
	}
	if off == nil {
		t.Fatal("off@x.io missing from accounts[]")
	}
	if off.GetAllocationStatus() != amxv1.AllocationStatus_ALLOCATION_STATUS_INACTIVE {
		t.Fatalf("off allocation status = %v, want INACTIVE", off.GetAllocationStatus())
	}
}
