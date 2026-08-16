package command

import (
	"context"
	"crypto/ed25519"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"fmt"
	"os"
	"path/filepath"
	"testing"

	amxv1 "github.com/2kwanghee/AMX/contracts/gen/go"
)

// binManifestText builds a manifest naming the current platform's artifact with
// the sha256 of body. builtAt is caller-controlled so a test can force a
// rollback.
func binManifestText(t *testing.T, commit, builtAt string, body []byte) string {
	t.Helper()
	sum := sha256.Sum256(body)
	return fmt.Sprintf(
		`{"version":{"commit":"%s","builtAt":"%s","wheel":"tsamx-0.0.1-py3-none-any.whl"},`+
			`"artifacts":{"%s":{"sha256":"%s","size":%d}}}`,
		commit, builtAt, artifactName(), hex.EncodeToString(sum[:]), len(body))
}

// binEnvelope wraps manifestText in the signed envelope AMS serves, signing
// signKey over (domain prefix + manifest bytes) unless dropDomain is set (which
// signs the bare bytes, i.e. a signature missing the domain separation).
func binEnvelope(t *testing.T, signKey ed25519.PrivateKey, manifestText string, dropDomain bool) []byte {
	t.Helper()
	msg := []byte(manifestText)
	if !dropDomain {
		msg = append(append([]byte{}, manifestSigDomain...), msg...)
	}
	sig := ed25519.Sign(signKey, msg)
	return []byte(fmt.Sprintf(`{"manifest":%q,"signature":%q,"algorithm":"ed25519:amx-manifest-v1"}`,
		manifestText, base64.StdEncoding.EncodeToString(sig)))
}

// binarySelfUpdateEnv installs a package-mode SelfUpdateConfig and returns the
// installed binary path plus a mutable fetch map (url -> bytes) the test tunes.
func binarySelfUpdateEnv(t *testing.T, hn *harness, f *fakeRunner) (binPath string, fetch map[string][]byte) {
	t.Helper()
	binPath = filepath.Join(t.TempDir(), "ama")
	if err := os.WriteFile(binPath, []byte("OLD-BINARY"), 0o755); err != nil {
		t.Fatal(err)
	}
	fetch = map[string][]byte{}
	pub := hn.priv.Public().(ed25519.PublicKey)
	hn.h.selfUpdate = &SelfUpdateConfig{
		AMSURL:         "http://ams.test",
		InstallRoot:    filepath.Dir(binPath),
		PubKey:         pub,
		CurrentBuiltAt: "2026-08-01T00:00:00Z",
		BinaryPath:     binPath,
		Runner:         f,
		Fetch: func(_ context.Context, url string) ([]byte, error) {
			if b, ok := fetch[url]; ok {
				return b, nil
			}
			return nil, fmt.Errorf("unexpected fetch: %s", url)
		},
	}
	// The downloaded binary's --version names the built commit (smoke passes).
	f.out[key(newBinaryPath(binPath), []string{"--version"})] = "p3+" + shortCommit(testCommit) + "\n"
	return binPath, fetch
}

// seedHealthyManifest fills fetch with a valid signed manifest (builtAt newer
// than the current baseline) and its artifact body.
func seedHealthyManifest(t *testing.T, hn *harness, fetch map[string][]byte, body []byte) {
	t.Helper()
	text := binManifestText(t, testCommit, "2026-08-10T00:00:00Z", body)
	fetch["http://ams.test/download/manifest.json"] = binEnvelope(t, hn.priv, text, false)
	fetch["http://ams.test/download/"+artifactName()] = body
}

func TestSelfUpdateBinaryHappyPath(t *testing.T) {
	hn := newHarness(t)
	f := newFakeRunner()
	binPath, fetch := binarySelfUpdateEnv(t, hn, f)
	seedHealthyManifest(t, hn, fetch, []byte("NEW-BINARY"))

	ack := hn.apply(t, selfUpdateCmd(t, hn, "su-bin-ok", ""))
	if ack.GetConvergence() != amxv1.CommandAck_CONVERGENCE_CONVERGED {
		t.Fatalf("want CONVERGED, got %v/%q (%s)", ack.GetConvergence(), ack.GetErrorCode(), ack.GetDetail())
	}
	if f.execArgv != binPath {
		t.Fatalf("exec argv0 = %q, want %q", f.execArgv, binPath)
	}
	got, err := os.ReadFile(binPath)
	if err != nil {
		t.Fatal(err)
	}
	if string(got) != "NEW-BINARY" {
		t.Fatalf("installed binary = %q, want NEW-BINARY", got)
	}
	// Package (binary) installs have no checkout to build tsamx from — the tsamx
	// reinstall is a git-mode-only step and must not fire here.
	if f.ran("uv tool install") {
		t.Fatalf("binary mode must not reinstall tsamx (ran %v)", f.calls)
	}
}

func TestSelfUpdateBinaryRejectsForgedAndMalformedSignature(t *testing.T) {
	for _, tc := range []struct {
		name string
		// mutate rewrites the manifest.json envelope in fetch.
		mutate func(t *testing.T, hn *harness, fetch map[string][]byte, text string)
	}{
		{"forged signature (wrong key)", func(t *testing.T, _ *harness, fetch map[string][]byte, text string) {
			_, wrong, _ := ed25519.GenerateKey(nil)
			fetch["http://ams.test/download/manifest.json"] = binEnvelope(t, wrong, text, false)
		}},
		{"signature missing domain prefix", func(t *testing.T, hn *harness, fetch map[string][]byte, text string) {
			fetch["http://ams.test/download/manifest.json"] = binEnvelope(t, hn.priv, text, true)
		}},
	} {
		t.Run(tc.name, func(t *testing.T) {
			hn := newHarness(t)
			f := newFakeRunner()
			binPath, fetch := binarySelfUpdateEnv(t, hn, f)
			body := []byte("NEW-BINARY")
			text := binManifestText(t, testCommit, "2026-08-10T00:00:00Z", body)
			fetch["http://ams.test/download/"+artifactName()] = body
			tc.mutate(t, hn, fetch, text)

			ack := hn.apply(t, selfUpdateCmd(t, hn, "su-bin-sig", ""))
			assertBinaryUnchanged(t, ack, "manifest_signature_invalid", binPath, f)
		})
	}
}

func TestSelfUpdateBinaryRejectsSha256Mismatch(t *testing.T) {
	hn := newHarness(t)
	f := newFakeRunner()
	binPath, fetch := binarySelfUpdateEnv(t, hn, f)
	// Manifest signed over the sha of "NEW-BINARY", but the served artifact is
	// different bytes -> the digest cannot match.
	text := binManifestText(t, testCommit, "2026-08-10T00:00:00Z", []byte("NEW-BINARY"))
	fetch["http://ams.test/download/manifest.json"] = binEnvelope(t, hn.priv, text, false)
	fetch["http://ams.test/download/"+artifactName()] = []byte("TAMPERED-BINARY")

	ack := hn.apply(t, selfUpdateCmd(t, hn, "su-bin-sha", ""))
	assertBinaryUnchanged(t, ack, "sha256_mismatch", binPath, f)
}

func TestSelfUpdateBinaryRefusesRollback(t *testing.T) {
	hn := newHarness(t)
	f := newFakeRunner()
	binPath, fetch := binarySelfUpdateEnv(t, hn, f)
	body := []byte("OLDER-BINARY")
	// builtAt predates CurrentBuiltAt (2026-08-01) -> replay/rollback refused.
	text := binManifestText(t, testCommit, "2026-07-01T00:00:00Z", body)
	fetch["http://ams.test/download/manifest.json"] = binEnvelope(t, hn.priv, text, false)
	fetch["http://ams.test/download/"+artifactName()] = body

	ack := hn.apply(t, selfUpdateCmd(t, hn, "su-bin-rollback", ""))
	assertBinaryUnchanged(t, ack, "rollback_refused", binPath, f)
}

func TestSelfUpdateBinaryEnforcesExpectedCommitPin(t *testing.T) {
	hn := newHarness(t)
	f := newFakeRunner()
	binPath, fetch := binarySelfUpdateEnv(t, hn, f)
	seedHealthyManifest(t, hn, fetch, []byte("NEW-BINARY"))

	// Pin a commit the manifest does not name.
	ack := hn.apply(t, selfUpdateCmd(t, hn, "su-bin-pin", "0000000deadbeef"))
	assertBinaryUnchanged(t, ack, "commit_mismatch", binPath, f)
}

func TestSelfUpdateBinarySmokeFailureKeepsOldBinary(t *testing.T) {
	hn := newHarness(t)
	f := newFakeRunner()
	binPath, fetch := binarySelfUpdateEnv(t, hn, f)
	seedHealthyManifest(t, hn, fetch, []byte("NEW-BINARY"))
	// The downloaded binary runs but --version does not name the built commit.
	f.out[key(newBinaryPath(binPath), []string{"--version"})] = "p3+badc0mmit\n"

	ack := hn.apply(t, selfUpdateCmd(t, hn, "su-bin-smoke", ""))
	assertBinaryUnchanged(t, ack, "smoke_failed", binPath, f)
	if _, err := os.Stat(newBinaryPath(binPath)); !os.IsNotExist(err) {
		t.Fatalf("staged .new binary should have been removed on smoke failure")
	}
}

func TestSelfUpdateBinaryRefusesWhenNoBaselineAndNoPin(t *testing.T) {
	hn := newHarness(t)
	f := newFakeRunner()
	binPath, fetch := binarySelfUpdateEnv(t, hn, f)
	// Abnormal unstamped binary: no builtAt baseline to compare against.
	hn.h.selfUpdate.CurrentBuiltAt = ""
	seedHealthyManifest(t, hn, fetch, []byte("NEW-BINARY"))

	// No expected_commit pin either -> nothing can vouch for the manifest.
	ack := hn.apply(t, selfUpdateCmd(t, hn, "su-bin-nobaseline", ""))
	assertBinaryUnchanged(t, ack, "rollback_baseline_missing", binPath, f)
}

func TestSelfUpdateBinaryNoBaselineButPinnedIsAccepted(t *testing.T) {
	hn := newHarness(t)
	f := newFakeRunner()
	binPath, fetch := binarySelfUpdateEnv(t, hn, f)
	hn.h.selfUpdate.CurrentBuiltAt = ""
	seedHealthyManifest(t, hn, fetch, []byte("NEW-BINARY"))

	// A matching pin is the guard when there is no builtAt baseline.
	ack := hn.apply(t, selfUpdateCmd(t, hn, "su-bin-pinned", testCommit))
	if ack.GetConvergence() != amxv1.CommandAck_CONVERGENCE_CONVERGED {
		t.Fatalf("want CONVERGED, got %v/%q (%s)", ack.GetConvergence(), ack.GetErrorCode(), ack.GetDetail())
	}
	if got, _ := os.ReadFile(binPath); string(got) != "NEW-BINARY" {
		t.Fatalf("installed binary = %q, want NEW-BINARY", got)
	}
}

// TestSelfUpdateModeDispatch proves the install marker, not a fallback chain,
// selects the path: package env -> binary, repo env -> git, neither -> nil.
func TestSelfUpdateModeDispatch(t *testing.T) {
	if !(&SelfUpdateConfig{AMSURL: "http://x"}).binaryMode() {
		t.Fatal("AMSURL set should be binary mode")
	}
	if (&SelfUpdateConfig{RepoDir: "/tmp/x"}).binaryMode() {
		t.Fatal("RepoDir-only must not be binary mode")
	}
	// Neither configured -> unsupported reject (no binary path taken).
	hn := newHarness(t)
	hn.h.selfUpdate = nil
	ack := hn.apply(t, selfUpdateCmd(t, hn, "su-none-2", ""))
	if ack.GetConvergence() != amxv1.CommandAck_CONVERGENCE_REJECTED || ack.GetErrorCode() != "unsupported_command" {
		t.Fatalf("want REJECTED/unsupported_command, got %v/%q", ack.GetConvergence(), ack.GetErrorCode())
	}
}

// assertBinaryUnchanged checks a DIVERGED ack with the wanted code, the running
// binary still holding its original bytes, and no exec having fired.
func assertBinaryUnchanged(t *testing.T, ack *amxv1.CommandAck, wantCode, binPath string, f *fakeRunner) {
	t.Helper()
	if ack.GetConvergence() != amxv1.CommandAck_CONVERGENCE_DIVERGED || ack.GetErrorCode() != wantCode {
		t.Fatalf("want DIVERGED/%s, got %v/%q (%s)", wantCode, ack.GetConvergence(), ack.GetErrorCode(), ack.GetDetail())
	}
	got, err := os.ReadFile(binPath)
	if err != nil {
		t.Fatal(err)
	}
	if string(got) != "OLD-BINARY" {
		t.Fatalf("running binary was modified (%q); a failed update must leave it intact", got)
	}
	if f.execArgv != "" {
		t.Fatalf("exec must not fire on a failed update (argv0=%q)", f.execArgv)
	}
}
