package command

import (
	"context"
	"crypto/ed25519"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"time"

	amxv1 "github.com/2kwanghee/AMX/contracts/gen/go"
)

// manifestSigDomain MUST byte-match ams-server app/api/download.py
// MANIFEST_SIG_DOMAIN. AMS signs MANIFEST_SIG_DOMAIN + <manifest file bytes>
// with the same Ed25519 key it signs gRPC commands with; verifying without the
// prefix would let a command signature masquerade as a manifest one. The
// envelope's `algorithm` names the scheme ("ed25519:amx-manifest-v1").
var manifestSigDomain = []byte("amx-manifest-v1\x00")

// maxArtifactBytes caps a downloaded body. The agent binary is ~17 MiB; the cap
// keeps a hostile or broken AMS from streaming an unbounded response into
// memory before the sha256 (which would catch it) ever runs.
const maxArtifactBytes = 256 << 20

// manifestFetchTimeout bounds the whole manifest+artifact download. Generous for
// a LAN transfer of a ~17 MiB binary, short enough that a wedged AMS does not
// park the command forever (the git path bounds each step the same way).
const manifestFetchTimeout = 5 * time.Minute

// manifestEnvelope is the /download/manifest.json response (download.py
// get_manifest). Manifest is the manifest file's EXACT text: the signature
// covers those bytes, so it is verified verbatim and never re-serialized.
type manifestEnvelope struct {
	Manifest  string `json:"manifest"`
	Signature string `json:"signature"`
	Algorithm string `json:"algorithm"`
}

// releaseManifest is the parsed manifest text (deploy/build-artifacts.sh).
type releaseManifest struct {
	Version struct {
		Commit  string `json:"commit"`
		BuiltAt string `json:"builtAt"`
		Wheel   string `json:"wheel"`
	} `json:"version"`
	Artifacts map[string]struct {
		Sha256 string `json:"sha256"`
		Size   int64  `json:"size"`
	} `json:"artifacts"`
}

// artifactName is the ama binary for this platform, matching the names
// build-artifacts.sh records (ama-<goos>-<goarch>, plus .exe on windows).
func artifactName() string {
	n := "ama-" + runtime.GOOS + "-" + runtime.GOARCH
	if runtime.GOOS == "windows" {
		n += ".exe"
	}
	return n
}

// newBinaryPath is where the downloaded artifact is staged: next to the running
// binary (same filesystem, so the swap's os.Rename is atomic). On Windows the
// .exe extension is preserved so the smoke test can actually execute the staged
// file.
func newBinaryPath(binPath string) string {
	if runtime.GOOS == "windows" && strings.HasSuffix(strings.ToLower(binPath), ".exe") {
		return strings.TrimSuffix(binPath, filepath.Ext(binPath)) + newBinarySuffix + ".exe"
	}
	return binPath + newBinarySuffix
}

// handleSelfUpdateBinary is the package-install self_update path. Instead of
// rebuilding a git tree it downloads a signed manifest and a prebuilt binary
// from AMS, verifies the Ed25519 signature over the manifest and the sha256 of
// the binary, smoke-tests it, then hands off to the SHARED swapAndRestart. Only
// the "acquire a new binary" front half differs from the git path; everything
// irreversible is common code.
func (h *Handler) handleSelfUpdateBinary(ctx context.Context, cmd *amxv1.AmsCommand, su *amxv1.SelfUpdate, ack *amxv1.CommandAck) *amxv1.CommandAck {
	cfg := h.selfUpdate
	runner := cfg.Runner
	if runner == nil {
		runner = OSSelfUpdateRunner{}
	}
	binPath := cfg.BinaryPath
	if binPath == "" {
		p, err := os.Executable()
		if err != nil {
			return h.finishSelfUpdate(h.divergedSelfUpdate(ack, "preflight_failed", err))
		}
		binPath = p
	}
	fail := func(code string, err error) *amxv1.CommandAck {
		return h.finishSelfUpdate(h.divergedSelfUpdate(ack, code, err))
	}
	if len(cfg.PubKey) != ed25519.PublicKeySize {
		return fail("preflight_failed", errors.New("no AMS signing key configured for binary self_update"))
	}

	// Preflight: room for the download plus a second copy next to the running
	// binary. Same floor and rationale as the git path.
	free, err := runner.FreeBytes(filepath.Dir(binPath))
	if err != nil {
		return fail("preflight_failed", fmt.Errorf("free space: %w", err))
	}
	if free < cfg.minFree() {
		return fail("preflight_failed", fmt.Errorf("free space %d bytes below the %d byte floor", free, cfg.minFree()))
	}

	base := strings.TrimRight(cfg.AMSURL, "/")

	// --- Fetch + verify the manifest. The signature is a veto on everything
	// that follows, so it is checked before a single artifact byte is fetched. --
	envRaw, err := h.fetch(ctx, cfg, base+"/download/manifest.json")
	if err != nil {
		return fail("manifest_fetch_failed", err)
	}
	var env manifestEnvelope
	if err := json.Unmarshal(envRaw, &env); err != nil {
		return fail("manifest_invalid", fmt.Errorf("decode manifest envelope: %w", err))
	}
	sig, err := base64.StdEncoding.DecodeString(strings.TrimSpace(env.Signature))
	if err != nil {
		return fail("manifest_signature_invalid", fmt.Errorf("decode signature: %w", err))
	}
	signed := make([]byte, 0, len(manifestSigDomain)+len(env.Manifest))
	signed = append(signed, manifestSigDomain...)
	signed = append(signed, env.Manifest...)
	if !ed25519.Verify(cfg.PubKey, signed, sig) {
		return fail("manifest_signature_invalid", errors.New("manifest signature does not verify against the pinned AMS key"))
	}

	var man releaseManifest
	if err := json.Unmarshal([]byte(env.Manifest), &man); err != nil {
		return fail("manifest_invalid", fmt.Errorf("decode manifest: %w", err))
	}
	newCommit := strings.TrimSpace(man.Version.Commit)
	if newCommit == "" {
		return fail("manifest_invalid", errors.New("manifest names no commit"))
	}
	if strings.TrimSpace(man.Version.BuiltAt) == "" {
		return fail("manifest_invalid", errors.New("manifest carries no builtAt"))
	}

	// Pin: a signed command may veto which commit the agent becomes (same meaning
	// as the git path's expected_commit check against the remote tip).
	if want := strings.TrimSpace(su.GetExpectedCommit()); want != "" && !commitMatches(newCommit, want) {
		return fail("commit_mismatch", fmt.Errorf("expected_commit %q but the manifest names %q", want, newCommit))
	}

	// Rollback guard: refuse a manifest not strictly newer than the running
	// binary. AMS here is reached over a plaintext LAN, where an attacker can
	// replay an OLD but validly-signed manifest+binary to force a downgrade to a
	// known-vulnerable build; a monotonic builtAt makes that replay a no-op. With
	// no known current builtAt (a dev build with no -ldflags stamp) the pin above
	// is the only guard.
	if err := ensureNotRollback(cfg.CurrentBuiltAt, man.Version.BuiltAt); err != nil {
		return fail("rollback_refused", err)
	}

	// --- Download the artifact for this platform and match its sha256 to the
	// signed manifest. A mismatch means the bytes are not what AMS signed. ------
	name := artifactName()
	entry, ok := man.Artifacts[name]
	if !ok {
		return fail("artifact_missing", fmt.Errorf("manifest lists no artifact %q for this platform", name))
	}
	body, err := h.fetch(ctx, cfg, base+"/download/"+name)
	if err != nil {
		return fail("artifact_fetch_failed", err)
	}
	sum := sha256.Sum256(body)
	if got := hex.EncodeToString(sum[:]); !strings.EqualFold(got, strings.TrimSpace(entry.Sha256)) {
		return fail("sha256_mismatch", fmt.Errorf("artifact %s sha256 %s does not match the manifest", name, got))
	}

	// Stage the validated bytes next to the running binary.
	newBin := newBinaryPath(binPath)
	_ = runner.Remove(newBin)
	if err := runner.WriteFile(newBin, body, 0o755); err != nil {
		return fail("stage_failed", err)
	}

	// Smoke: the downloaded binary must run and name the commit it claims to be —
	// the same check the git path applies to its freshly built binary, and what
	// proves a downloaded blob is an ama that understands --version before it is
	// ever installed.
	smokeOut, serr := runner.Run(ctx, RunSpec{
		Dir:     filepath.Dir(binPath),
		Name:    newBin,
		Args:    []string{"--version"},
		Env:     smokeEnv(),
		Timeout: smokeStepTimeout,
	})
	if serr != nil {
		_ = runner.Remove(newBin)
		code := "smoke_failed"
		if errors.Is(serr, ErrRunTimeout) {
			code = "timeout_smoke"
		}
		return fail(code, fmt.Errorf("%w: %s", serr, tail(smokeOut)))
	}
	if short := shortCommit(newCommit); !strings.Contains(smokeOut, short) {
		_ = runner.Remove(newBin)
		return fail("smoke_failed",
			fmt.Errorf("--version output does not name the built commit %s: %s", short, tail(smokeOut)))
	}

	return h.swapAndRestart(runner, cfg, ack, binPath, newBin, newCommit)
}

// ensureNotRollback returns an error when manifestBuiltAt is not strictly newer
// than currentBuiltAt. Both are RFC3339. An empty/unparseable current baseline
// is treated as "no baseline" (accept — the commit pin is then the only guard);
// an unparseable manifest builtAt is a hard error, since the manifest is signed
// and should always be well-formed.
func ensureNotRollback(currentBuiltAt, manifestBuiltAt string) error {
	mt, err := time.Parse(time.RFC3339, strings.TrimSpace(manifestBuiltAt))
	if err != nil {
		return fmt.Errorf("manifest builtAt %q is not RFC3339: %w", manifestBuiltAt, err)
	}
	currentBuiltAt = strings.TrimSpace(currentBuiltAt)
	if currentBuiltAt == "" {
		return nil // no known baseline; the commit pin is the guard
	}
	ct, err := time.Parse(time.RFC3339, currentBuiltAt)
	if err != nil {
		return nil // do not block on an unparseable local baseline
	}
	if !mt.After(ct) {
		return fmt.Errorf("manifest builtAt %s is not newer than the running binary %s (replay/rollback refused)",
			strings.TrimSpace(manifestBuiltAt), currentBuiltAt)
	}
	return nil
}

// fetch retrieves url's body, using the injected Fetch when set (tests) and a
// bounded net/http client otherwise.
func (h *Handler) fetch(ctx context.Context, cfg *SelfUpdateConfig, url string) ([]byte, error) {
	if cfg.Fetch != nil {
		return cfg.Fetch(ctx, url)
	}
	return defaultFetch(ctx, url)
}

func defaultFetch(ctx context.Context, url string) ([]byte, error) {
	ctx, cancel := context.WithTimeout(ctx, manifestFetchTimeout)
	defer cancel()
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, url, nil)
	if err != nil {
		return nil, err
	}
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("GET %s: unexpected status %d", url, resp.StatusCode)
	}
	body, err := io.ReadAll(io.LimitReader(resp.Body, maxArtifactBytes+1))
	if err != nil {
		return nil, err
	}
	if int64(len(body)) > maxArtifactBytes {
		return nil, fmt.Errorf("GET %s: body exceeds the %d byte cap", url, maxArtifactBytes)
	}
	return body, nil
}
