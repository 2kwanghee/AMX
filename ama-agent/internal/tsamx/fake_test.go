package tsamx

import (
	"context"
	"testing"

	"github.com/2kwanghee/AMX/ama-agent/internal/provider"
)

func TestFakeLifecycle(t *testing.T) {
	ctx := context.Background()
	f := NewFake()
	if err := f.Add(ctx, provider.AddRequest{Email: "a@x.io", Enable: true}); err != nil {
		t.Fatal(err)
	}
	if err := f.Add(ctx, provider.AddRequest{Email: "b@x.io", Enable: false}); err != nil {
		t.Fatal(err)
	}
	if d, _ := f.Disabled("b@x.io"); !d {
		t.Fatal("b should be disabled after add with Enable=false")
	}
	if err := f.Switch(ctx, "a@x.io"); err != nil {
		t.Fatal(err)
	}
	st, err := f.Status(ctx)
	if err != nil {
		t.Fatal(err)
	}
	if st.ActiveEmail != "a@x.io" {
		t.Fatalf("active = %q", st.ActiveEmail)
	}
	if err := f.Remove(ctx, "a@x.io"); err != nil {
		t.Fatal(err)
	}
	if f.Has("a@x.io") {
		t.Fatal("account still present after remove")
	}
	// Remove is idempotent.
	if err := f.Remove(ctx, "a@x.io"); err != nil {
		t.Fatalf("second remove: %v", err)
	}
	list, _ := f.List(ctx)
	if len(list.Accounts) != 1 {
		t.Fatalf("accounts = %d, want 1", len(list.Accounts))
	}
}

func TestFakeSwitchUnknownFails(t *testing.T) {
	f := NewFake()
	if err := f.Switch(context.Background(), "nobody@x.io"); err == nil {
		t.Fatal("switch to unknown account should error")
	}
}
