package store

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"os"
	"path/filepath"
	"testing"

	"github.com/2kwanghee/AMX/ama-agent/internal/crypto"
)

// testFP is a stand-in for the provider driver's fingerprint: a plain content
// hash is enough for the store's contract (distinct creds -> distinct baselines).
func testFP(b []byte) string {
	if len(b) == 0 {
		return ""
	}
	sum := sha256.Sum256(b)
	return hex.EncodeToString(sum[:])
}

func keksWith(t *testing.T, id string) *KEKHolder {
	t.Helper()
	h := NewKEKHolder()
	h.Put(id, bytes.Repeat([]byte{0x11}, crypto.KEKSize))
	if !h.SetActive(id) {
		t.Fatal("SetActive failed")
	}
	return h
}

// TestFindByProviderEmailMigratesLegacyManifest (P3): a manifest written before
// the provider field existed has records with no "provider" key. On load, such a
// record must resolve as the claude provider (empty == "claude"), so a claude (or
// empty) lookup finds it while another provider's lookup does not — the tolerant
// migration that keeps existing on-disk state working after the shim ships.
func TestFindByProviderEmailMigratesLegacyManifest(t *testing.T) {
	dir := t.TempDir()
	// No "provider" field on the record, exactly as a pre-multi-provider agent
	// wrote it. The metadata is plaintext JSON; the lookup reads only email/provider.
	legacy := `{"schemaVersion":1,"records":[{"amsAccountId":"acc-1","email":"a@x.io","allocationStatus":2}]}`
	if err := os.WriteFile(filepath.Join(dir, ManifestFileName), []byte(legacy), 0o600); err != nil {
		t.Fatal(err)
	}
	st, err := Open(dir, "ama_dev", keksWith(t, "k1"), testFP)
	if err != nil {
		t.Fatal(err)
	}
	if rec, ok := st.FindByProviderEmail("claude", "a@x.io"); !ok || rec.AMSAccountID != "acc-1" {
		t.Fatalf("legacy record not resolved as claude: rec=%+v ok=%v", rec, ok)
	}
	if _, ok := st.FindByProviderEmail("", "a@x.io"); !ok {
		t.Fatal("empty provider query must normalize to claude and match the legacy record")
	}
	if _, ok := st.FindByProviderEmail("codex", "a@x.io"); ok {
		t.Fatal("legacy claude record must not match a codex lookup")
	}
}

func TestManifestSealOpen(t *testing.T) {
	dir := t.TempDir()
	keks := keksWith(t, "k1")
	st, err := Open(dir, "ama_dev", keks, testFP)
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
	st2, err := Open(dir, "ama_dev", keks, testFP)
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
	st, err := Open(dir, "agentA", keks, testFP)
	if err != nil {
		t.Fatal(err)
	}
	if err := st.Upsert(Record{AMSAccountID: "acc-1", Email: "a@x.io"}, []byte("secret")); err != nil {
		t.Fatal(err)
	}
	// Same file + same KEK, but a different agent id -> AAD differs -> open fails.
	st2, err := Open(dir, "agentB", keks, testFP)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := st2.OpenCredential("acc-1"); err == nil {
		t.Fatal("record opened under a different agent id (AAD over-binding not enforced)")
	}
}

func TestUpsertWithoutKEK(t *testing.T) {
	dir := t.TempDir()
	st, err := Open(dir, "ama_dev", NewKEKHolder(), testFP)
	if err != nil {
		t.Fatal(err)
	}
	if err := st.Upsert(Record{AMSAccountID: "acc-1"}, []byte("x")); err != ErrNoKEK {
		t.Fatalf("want ErrNoKEK, got %v", err)
	}
}

func TestSetStatusAndRemove(t *testing.T) {
	dir := t.TempDir()
	st, _ := Open(dir, "ama_dev", keksWith(t, "k1"), testFP)
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

// TestUpdateBaselineDoesNotResurrect proves the O9 re-sync baseline advance
// (fix 4) never recreates a record a concurrent recall purged: after Remove, an
// UpdateBaseline for the same account returns ErrNotFound and leaves the record
// absent, so reconcile cannot re-inject a recalled credential.
func TestUpdateBaselineDoesNotResurrect(t *testing.T) {
	dir := t.TempDir()
	st, _ := Open(dir, "ama_dev", keksWith(t, "k1"), testFP)
	if err := st.Upsert(Record{AMSAccountID: "acc-1", Email: "a@x.io", AllocationStatus: 2}, []byte("old")); err != nil {
		t.Fatal(err)
	}
	// A concurrent purge=true recall deletes the record.
	if err := st.Remove("acc-1"); err != nil {
		t.Fatal(err)
	}
	// The in-flight re-sync's baseline advance lands after the purge.
	if err := st.UpdateBaseline("acc-1", []byte("new")); err != ErrNotFound {
		t.Fatalf("want ErrNotFound, got %v", err)
	}
	if _, ok := st.Get("acc-1"); ok {
		t.Fatal("record resurrected by UpdateBaseline after purge")
	}
	// Survives reload: nothing was persisted.
	st2, _ := Open(dir, "ama_dev", keksWith(t, "k1"), testFP)
	if _, ok := st2.Get("acc-1"); ok {
		t.Fatal("record resurrected on disk")
	}
}

// TestUpdateBaselinePreservesStatus proves the baseline advance restamps the
// fingerprint + envelope only, leaving AllocationStatus untouched: a concurrent
// recall=disable that flipped the record to inactive is not reverted to active.
func TestUpdateBaselinePreservesStatus(t *testing.T) {
	dir := t.TempDir()
	st, _ := Open(dir, "ama_dev", keksWith(t, "k1"), testFP)
	old := []byte(`{"refreshToken":"r-old"}`)
	if err := st.Upsert(Record{AMSAccountID: "acc-1", Email: "a@x.io", AllocationStatus: 2}, old); err != nil {
		t.Fatal(err)
	}
	before, _ := st.Get("acc-1")
	// A concurrent recall=disable marks it inactive (status 3).
	if err := st.SetStatus("acc-1", 3); err != nil {
		t.Fatal(err)
	}
	// The in-flight re-sync advances the baseline with a rotated credential.
	fresh := []byte(`{"refreshToken":"r-new"}`)
	if err := st.UpdateBaseline("acc-1", fresh); err != nil {
		t.Fatal(err)
	}
	rec, ok := st.Get("acc-1")
	if !ok {
		t.Fatal("record missing after UpdateBaseline")
	}
	if rec.AllocationStatus != 3 {
		t.Fatalf("AllocationStatus reverted: want 3 (inactive), got %d", rec.AllocationStatus)
	}
	// Fingerprint + credential were advanced to the fresh set.
	if rec.Fingerprint == before.Fingerprint {
		t.Fatal("fingerprint not advanced")
	}
	got, err := st.OpenCredential("acc-1")
	if err != nil {
		t.Fatalf("open after baseline: %v", err)
	}
	if !bytes.Equal(got, fresh) {
		t.Fatal("credential not re-sealed to the fresh set")
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
