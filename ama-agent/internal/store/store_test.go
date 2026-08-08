package store

import (
	"bytes"
	"testing"

	"github.com/2kwanghee/AMX/ama-agent/internal/crypto"
)

func keksWith(t *testing.T, id string) *KEKHolder {
	t.Helper()
	h := NewKEKHolder()
	h.Put(id, bytes.Repeat([]byte{0x11}, crypto.KEKSize))
	if !h.SetActive(id) {
		t.Fatal("SetActive failed")
	}
	return h
}

func TestManifestSealOpen(t *testing.T) {
	dir := t.TempDir()
	keks := keksWith(t, "k1")
	st, err := Open(dir, "ama_dev", keks)
	if err != nil {
		t.Fatal(err)
	}
	cred := []byte(`{"accessToken":"s","refreshToken":"r"}`)
	rec := Record{AMSAccountID: "acc-1", Email: "a@x.io", AllocationStatus: 2}
	if err := st.Upsert(rec, cred); err != nil {
		t.Fatal(err)
	}
	got, err := st.OpenCredential("acc-1")
	if err != nil {
		t.Fatalf("open credential: %v", err)
	}
	if !bytes.Equal(got, cred) {
		t.Fatalf("credential roundtrip mismatch")
	}
	// Reload from disk with the same agent id and key -> still opens.
	st2, err := Open(dir, "ama_dev", keks)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := st2.OpenCredential("acc-1"); err != nil {
		t.Fatalf("reopen after reload: %v", err)
	}
}

// TestAADOverBinding proves a record sealed for one agent cannot be opened by a
// different agent id, even with the same KEK (SSOT §6.2 AAD binding).
func TestAADOverBinding(t *testing.T) {
	dir := t.TempDir()
	keks := keksWith(t, "k1")
	st, err := Open(dir, "agentA", keks)
	if err != nil {
		t.Fatal(err)
	}
	if err := st.Upsert(Record{AMSAccountID: "acc-1", Email: "a@x.io"}, []byte("secret")); err != nil {
		t.Fatal(err)
	}
	// Same file + same KEK, but a different agent id -> AAD differs -> open fails.
	st2, err := Open(dir, "agentB", keks)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := st2.OpenCredential("acc-1"); err == nil {
		t.Fatal("record opened under a different agent id (AAD over-binding not enforced)")
	}
}

func TestUpsertWithoutKEK(t *testing.T) {
	dir := t.TempDir()
	st, err := Open(dir, "ama_dev", NewKEKHolder())
	if err != nil {
		t.Fatal(err)
	}
	if err := st.Upsert(Record{AMSAccountID: "acc-1"}, []byte("x")); err != ErrNoKEK {
		t.Fatalf("want ErrNoKEK, got %v", err)
	}
}

func TestSetStatusAndRemove(t *testing.T) {
	dir := t.TempDir()
	st, _ := Open(dir, "ama_dev", keksWith(t, "k1"))
	_ = st.Upsert(Record{AMSAccountID: "acc-1", Email: "a@x.io", AllocationStatus: 2}, []byte("s"))
	if err := st.SetStatus("acc-1", 3); err != nil {
		t.Fatal(err)
	}
	rec, ok := st.Get("acc-1")
	if !ok || rec.AllocationStatus != 3 {
		t.Fatalf("status not updated: %+v ok=%v", rec, ok)
	}
	if err := st.Remove("acc-1"); err != nil {
		t.Fatal(err)
	}
	if _, ok := st.Get("acc-1"); ok {
		t.Fatal("record still present after remove")
	}
	// Remove is idempotent.
	if err := st.Remove("acc-1"); err != nil {
		t.Fatalf("second remove errored: %v", err)
	}
}

func TestAppliedLogRingAndLookup(t *testing.T) {
	dir := t.TempDir()
	l, err := OpenAppliedLog(dir)
	if err != nil {
		t.Fatal(err)
	}
	for i := 0; i < appliedRingSize+50; i++ {
		if err := l.Append(AppliedEntry{CommandID: id(i), Convergence: "CONVERGENCE_CONVERGED"}); err != nil {
			t.Fatal(err)
		}
	}
	ids := l.RecentIDs()
	if len(ids) != appliedRingSize {
		t.Fatalf("RecentIDs len = %d, want %d", len(ids), appliedRingSize)
	}
	if ids[0] != id(appliedRingSize+49) {
		t.Fatalf("newest-first violated: %s", ids[0])
	}
	// Oldest 50 evicted.
	if _, ok := l.Lookup(id(0)); ok {
		t.Fatal("evicted entry still found")
	}
	if _, ok := l.Lookup(id(appliedRingSize + 49)); !ok {
		t.Fatal("recent entry missing")
	}
	// Persistence across reopen.
	l2, err := OpenAppliedLog(dir)
	if err != nil {
		t.Fatal(err)
	}
	if _, ok := l2.Lookup(id(appliedRingSize + 49)); !ok {
		t.Fatal("entry lost across reopen")
	}
}

func id(i int) string {
	return "cmd-" + itoa(i)
}

func itoa(i int) string {
	if i == 0 {
		return "0"
	}
	var b []byte
	for i > 0 {
		b = append([]byte{byte('0' + i%10)}, b...)
		i /= 10
	}
	return string(b)
}
