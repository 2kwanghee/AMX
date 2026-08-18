// Command ama is the AMA daemon entrypoint. It wires transport, store, command,
// bridge, reporter, and the P3 scheduler together and opens the Session (Register
// -> SessionSetup -> command loop). The scheduler drives the auto-switch tick
// when the server is in mode=auto (design note §2, §6).
package main

import (
	"context"
	"errors"
	"fmt"
	"log"
	"os"
	"os/signal"
	"path/filepath"
	"sync"
	"syscall"
	"time"

	"github.com/2kwanghee/AMX/ama-agent/internal/command"
	"github.com/2kwanghee/AMX/ama-agent/internal/crypto"
	"github.com/2kwanghee/AMX/ama-agent/internal/metrics"
	"github.com/2kwanghee/AMX/ama-agent/internal/provider"
	"github.com/2kwanghee/AMX/ama-agent/internal/provider/claude"
	"github.com/2kwanghee/AMX/ama-agent/internal/provider/codex"
	"github.com/2kwanghee/AMX/ama-agent/internal/reporter"
	"github.com/2kwanghee/AMX/ama-agent/internal/resync"
	"github.com/2kwanghee/AMX/ama-agent/internal/scheduler"
	"github.com/2kwanghee/AMX/ama-agent/internal/store"
	"github.com/2kwanghee/AMX/ama-agent/internal/transport"
	"github.com/2kwanghee/AMX/ama-agent/internal/tsamx"
	amxv1 "github.com/2kwanghee/AMX/contracts/gen/go"
	"google.golang.org/protobuf/types/known/timestamppb"
)

// reportInterval is the usage-report cadence (SSOT §6.5, design note §1).
const reportInterval = 5 * time.Minute

// heartbeatInterval is the liveness cadence (design note §8): AMS marks a server
// offline after 3 missed beats (AMX_OFFLINE_AFTER = 3×30s), so this must stay at
// or below the server's AMX_HEARTBEAT_INTERVAL. AMX_HEARTBEAT_INTERVAL on the
// agent (a Go duration) overrides it for tests.
const heartbeatInterval = 30 * time.Second

// eventFlushInterval is how often the live outbox drain attempts delivery of
// queued AccountEvents on an already-connected session (see the drain goroutine).
const eventFlushInterval = 1 * time.Second

// errOutboxSendUnavailable re-queues an event when the transport send buffer is
// full or disconnected; the next flush (or OnConnect) retries it.
var errOutboxSendUnavailable = errors.New("ama: outbox send unavailable")

// selfUpdateAckWait bounds how long the self_update handler waits for its ack to
// be confirmed on the wire before it execs anyway. Long enough for a healthy
// stream to flush, short enough that a wedged one does not delay the restart.
const selfUpdateAckWait = 2 * time.Second

// version is the agent's release line. commit is stamped at build time with
// `-ldflags "-X main.commit=<sha>"` (deploy/agent-run.sh, and the self_update
// rebuild) and is empty for a plain `go build`. Together they form the
// agent_version string AMS records on Register, so the console can tell which
// commit each agent is actually running — the thing self_update exists to move.
var (
	version = "p3"
	commit  = ""
	// builtAt is the build timestamp (RFC3339), stamped with
	// `-ldflags "-X main.builtAt=<ts>"` by deploy/build-artifacts.sh. It is NOT
	// part of --version (that stays commit-only so the self_update smoke check is
	// unchanged); it is the monotonicity floor binary-mode self_update uses to
	// refuse a replayed older manifest. Empty for a plain `go build`.
	builtAt = ""
)

// agentVersion renders "<version>+<shortsha>", or just the version when the
// build carried no commit. The server column is a plain string, so no schema
// change is involved.
func agentVersion() string {
	if commit == "" {
		return version
	}
	short := commit
	if len(short) > 12 {
		short = short[:12]
	}
	return version + "+" + short
}

func main() {
	// --version must work before any state is opened: the self_update smoke test
	// runs the freshly built binary with this flag to prove it can execute at all
	// (internal/command/selfupdate.go), and it must not touch the live state dir.
	for _, a := range os.Args[1:] {
		if a == "--version" || a == "-version" {
			fmt.Println(agentVersion())
			return
		}
	}
	if err := run(); err != nil {
		log.Fatalf("ama: %v", err)
	}
}

func run() error {
	agentID := env("AMX_AGENT_ID", "ama_dev")
	serverID := env("AMX_SERVER_ID", "")
	amsAddr := env("AMX_AMS_ADDR", "localhost:50051")
	stateDir := env("AMX_STATE_DIR", filepath.Join(os.TempDir(), "ama-state"))
	enrollToken := os.Getenv("AMX_ENROLL_TOKEN")

	pub, err := crypto.LoadAMSPublicKey()
	if err != nil {
		return err
	}

	// The provider driver owns all vendor-specific credential/config-home
	// knowledge; every other package depends only on the neutral interface. One
	// Claude driver is wired here (the only provider today).
	drv := claude.New()

	keks := store.NewKEKHolder()
	st, err := store.Open(stateDir, agentID, keks, drv.Fingerprint)
	if err != nil {
		return err
	}
	applied, err := store.OpenAppliedLog(stateDir)
	if err != nil {
		return err
	}
	creds, err := store.OpenCredentialSidecar(stateDir)
	if err != nil {
		return err
	}

	// The claude bridge is the default/auto-switch/resync provider — the only one
	// that rotates — and the sole registry entry today.
	claudeBridge := tsamx.NewExecBridge(drv)
	bridge := claudeBridge

	// Provider registry: one Bridge per provider, listed explicitly. A second
	// provider is added as its own entry here (e.g. "codex": tsamx.NewExecBridge(
	// codex.New())) without touching the routing. bridgeFor routes a command to its
	// provider's bridge; an empty provider normalizes to "claude" and an
	// unregistered non-empty provider resolves to nil (the command handlers turn
	// that into a fail-closed provider_unsupported ack, never handing the credential
	// to a bridge).
	bridges := map[string]provider.Bridge{
		drv.Name(): claudeBridge,
	}
	// Codex is wired only when its config home (AMX_CODEX_HOME) is set. Unset
	// leaves it completely inactive — no bridge registered, no resyncer started —
	// so the claude-only behavior is unchanged. Codex does not rotate; it stays out
	// of PoolSummary/ActiveAccount (reporter summaryScope), contributing only its
	// accounts[] usage.
	codexDrv := codex.New()
	codexBridge := maybeRegisterCodex(bridges, codexDrv)
	if codexBridge != nil {
		log.Printf("codex provider enabled (config home %s)", codexDrv.ConfigHome())
	}
	bridgeFor := func(providerKey string) provider.Bridge {
		return bridges[provider.Normalize(providerKey)]
	}

	rep := reporter.New(agentID, bridges, time.Now)
	// Resolve each reported account's AMS identity from the manifest, keyed by
	// (provider, email) — a bridge knows only the email. Without ams_account_id in
	// the report, AMS reconcile treats every assigned account as absent and
	// redelivers it in a loop, rewriting the live credential file and racing the O9
	// re-sync.
	rep.SetIDResolver(func(providerKey, email string) (string, string, bool) {
		rec, ok := st.FindByProviderEmail(providerKey, email)
		if !ok {
			return "", "", false
		}
		return rec.AMSAccountID, rec.AccountUUID, true
	})
	// Disk-backed outbox: AccountEvents queued while AMS is unreachable survive an
	// agent restart (C1 W1) and are deleted only after a confirmed send (C1 W2).
	outbox, err := reporter.OpenOutbox(stateDir)
	if err != nil {
		return err
	}

	// Engine lock (R3): the single mutex serializing every tsamx mutation
	// sequence across the scheduler tick and the command handlers (decision 4).
	engine := &sync.Mutex{}
	// AMX_TICK_INTERVAL overrides the scheduler's base tick period (a Go
	// duration, e.g. "1s"). Unset/invalid keeps the DefaultInterval. Primarily a
	// test hook to force a prompt tick; production leaves it unset.
	var tickInterval time.Duration
	if v := os.Getenv("AMX_TICK_INTERVAL"); v != "" {
		if d, err := time.ParseDuration(v); err == nil {
			tickInterval = d
		} else {
			log.Printf("AMX_TICK_INTERVAL %q: %v (using default)", v, err)
		}
	}
	sched := scheduler.New(scheduler.Config{
		AgentID:  agentID,
		Bridge:   bridge,
		Reporter: rep,
		Outbox:   outbox,
		Engine:   engine,
		Interval: tickInterval,
		Now:      time.Now,
		Logf:     log.Printf,
	})

	// self_update (§6.3). The install method decides how a new binary is
	// acquired, and it is a marker in the daemon env — not a fallback chain:
	//   - package install (install.sh/ps1): AMX_INSTALL_METHOD=package plus
	//     AMX_AMS_URL — download a signed prebuilt binary from AMS and verify it.
	//   - git install (deploy/agent-run.sh): AMX_REPO_DIR — rebuild that tree.
	// With neither marker the command is rejected as unsupported rather than
	// guessing. AckSender is filled in below, once the transport client exists —
	// the ack has to go out before the exec replaces this process.
	var selfUpdate *command.SelfUpdateConfig
	if os.Getenv("AMX_INSTALL_METHOD") == "package" {
		if amsURL := os.Getenv("AMX_AMS_URL"); amsURL != "" {
			selfUpdate = &command.SelfUpdateConfig{
				AMSURL:         amsURL,
				InstallRoot:    os.Getenv("AMX_INSTALL_ROOT"),
				PubKey:         pub,
				CurrentBuiltAt: builtAt,
			}
		}
	} else if repoDir := os.Getenv("AMX_REPO_DIR"); repoDir != "" {
		selfUpdate = &command.SelfUpdateConfig{RepoDir: repoDir}
	}

	handler, err := command.New(command.Config{
		AgentID:          agentID,
		PublicKey:        pub,
		Store:            st,
		KEKs:             keks,
		Applied:          applied,
		Bridge:           bridge,
		BridgeFor:        bridgeFor,
		Creds:            creds,
		Engine:           engine,
		Outbox:           outbox,
		SwitchController: sched,
		SelfUpdate:       selfUpdate,
	})
	if err != nil {
		return err
	}

	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()
	defer sched.Stop()

	secOpt, err := transport.SecurityDialOption()
	if err != nil {
		return err
	}
	client := transport.Dial(amsAddr, secOpt)
	defer client.Close()

	// The self_update handler sends its own ack: syscall.Exec never returns, so
	// an ack left for the loop below would never be sent. Handle then returns nil
	// for that command and the loop skips its own send.
	//
	// SendConfirmed yields the stream.Send result, so we can wait for the write to
	// actually leave rather than only reaching the send buffer — otherwise the
	// exec routinely destroys the ack before the pump gets to it. The wait is
	// bounded: a stalled or disconnected stream must not hold up the restart,
	// because a lost ack is already covered (AMS re-queues the command_id and the
	// restarted agent answers CONVERGED from its applied log).
	if selfUpdate != nil {
		selfUpdate.Logf = log.Printf
		selfUpdate.AckSender = func(ack *amxv1.CommandAck) error {
			done, ok := client.SendConfirmed(&amxv1.AmaMessage{Msg: &amxv1.AmaMessage_Ack{Ack: ack}})
			if !ok {
				return errors.New("ama: transport closed")
			}
			select {
			case err := <-done:
				return err
			case <-time.After(selfUpdateAckWait):
				return errors.New("ama: ack send not confirmed in time")
			}
		}
	}

	// O9 credential re-sync (§5.7): watch the active account's live credential for
	// a local refresh-token rotation and push the refreshed set back to AMS. The
	// driver resolves both the config home and the credential file within it (the
	// home the bridge stages and tsamx refreshes in place). Empty home disables
	// re-sync.
	var credPath string
	if cfgDir := drv.ConfigHome(); cfgDir != "" {
		credPath = drv.CredentialPath(cfgDir)
	}
	// The material guard (§5.7) drops a token-less credential set with a log line
	// only, so the FIRST incident — the credential going dead under a live account —
	// is invisible to an operator until the account is later quarantined. Queue an
	// AccountEvent on that drop instead, so AMS opens a credential_unusable alert.
	// resync calls this edge-triggered (once per incident), and the Outbox carries
	// it across a disconnect. Identifiers only: no token, no hash, no path (§7), and
	// detail is a fixed human-readable string, not derived from the credential.
	unusableEvent := func(providerKey string) func(store.Record) {
		return func(rec store.Record) {
			ev := &amxv1.AccountEvent{
				SchemaVersion: 1,
				AgentId:       agentID,
				EventId:       reporter.NewEventID(),
				OccurredAt:    timestamppb.New(time.Now().UTC()),
				Kind:          amxv1.AccountEvent_KIND_CREDENTIAL_UNUSABLE,
				// The affected account rides in `from`, the same slot quarantine uses
				// (`to` stays unset — nothing became active).
				From: &amxv1.AccountRef{
					AmsAccountId: rec.AMSAccountID,
					Email:        rec.Email,
					AccountUuid:  rec.AccountUUID,
					Provider:     providerKey,
				},
				Detail: "on-disk credential carries no token material",
			}
			// A disk-append error only forfeits restart durability for this one event;
			// it is still queued in memory, so the live session still delivers it.
			_ = outbox.Enqueue(ev)
		}
	}
	resyncer := resync.New(resync.Config{
		AgentID:          agentID,
		Store:            st,
		KEKs:             keks,
		Bridge:           bridge,
		Provider:         drv.Name(),
		Engine:           engine,
		CredentialsPath:  credPath,
		Fingerprint:      drv.Fingerprint,
		HasMaterial:      drv.HasCredentialMaterial,
		OnUnusable:       unusableEvent(drv.Name()),
		ServerCredential: handler.ServerCredential,
		Send: func(u *amxv1.CredentialUpdate) bool {
			return client.TrySend(&amxv1.AmaMessage{Msg: &amxv1.AmaMessage_CredUpdate{CredUpdate: u}})
		},
		Now:  time.Now,
		Logf: log.Printf,
	})
	// One resyncer per rotating/credential-bearing provider; each watches its own
	// live credential file for a local rotation and pushes the refreshed set back
	// up, scoped to its provider. Codex is appended only when its bridge is wired
	// (AMX_CODEX_HOME set); unset leaves this a claude-only list, unchanged.
	resyncers := []*resync.Resyncer{resyncer}
	if codexBridge != nil {
		var codexCred string
		if cfgDir := codexDrv.ConfigHome(); cfgDir != "" {
			codexCred = codexDrv.CredentialPath(cfgDir)
		}
		resyncers = append(resyncers, resync.New(resync.Config{
			AgentID:          agentID,
			Store:            st,
			KEKs:             keks,
			Bridge:           codexBridge,
			Provider:         codexDrv.Name(),
			Engine:           engine,
			CredentialsPath:  codexCred,
			Fingerprint:      codexDrv.Fingerprint,
			HasMaterial:      codexDrv.HasCredentialMaterial,
			OnUnusable:       unusableEvent(codexDrv.Name()),
			ServerCredential: handler.ServerCredential,
			Send: func(u *amxv1.CredentialUpdate) bool {
				return client.TrySend(&amxv1.AmaMessage{Msg: &amxv1.AmaMessage_CredUpdate{CredUpdate: u}})
			},
			Now:  time.Now,
			Logf: log.Printf,
		}))
	}

	// On (re)connect, send Register first (SSOT §5.4). The auth is the enroll
	// token on first contact, else the server credential from a prior SessionSetup.
	client.OnConnect = func(send func(*amxv1.AmaMessage) error) error {
		// Fresh ephemeral X25519 key pair per connection (C2 §7): AMS seals every
		// per-agent KEK to this public key, and the matching private key (held in
		// session-scoped memory) unwraps them in SessionSetup. A new pair every
		// reconnect makes a captured KEK envelope unusable across sessions.
		agentPub, kerr := handler.NewSession()
		if kerr != nil {
			return kerr
		}
		reg := &amxv1.Register{
			AgentId:           agentID,
			ServerId:          serverID,
			Hostname:          hostname(),
			AgentVersion:      agentVersion(),
			SwitchMode:        handler.SwitchMode(),
			AppliedCommandIds: applied.RecentIDs(),
			AgentPublicKey:    agentPub,
		}
		// Seed local reality so AMS can reconcile immediately (§5.4). `list` reads
		// without a KEK, so this works even before SessionSetup.
		if r, rerr := rep.BuildUsageReport(ctx, amxv1.UsageReport_TRIGGER_SCHEDULE); rerr == nil {
			reg.Accounts = r.GetAccounts()
		}
		if sc := handler.ServerCredential(); sc != "" {
			reg.Auth = &amxv1.Register_ServerCredential{ServerCredential: sc}
		} else if enrollToken != "" {
			reg.Auth = &amxv1.Register_EnrollToken{EnrollToken: enrollToken}
		}
		if err := send(&amxv1.AmaMessage{Msg: &amxv1.AmaMessage_Register{Register: reg}}); err != nil {
			return err
		}
		// Flush AccountEvents queued while disconnected (Outbox, dedupe by
		// event_id). Unsent remainder is retried on the next reconnect.
		return outbox.Flush(func(ev *amxv1.AccountEvent) error {
			return send(&amxv1.AmaMessage{Msg: &amxv1.AmaMessage_Event{Event: ev}})
		})
	}

	go func() {
		if err := client.Run(ctx); err != nil && ctx.Err() == nil {
			log.Printf("transport: %v", err)
		}
	}()

	// Usage report ticker (§6.5): every 5 minutes, project the local pool and
	// send it non-blocking. A drop while disconnected is harmless — the next tick
	// supersedes it (design note §8). AMX_REPORT_INTERVAL overrides the cadence
	// (a Go duration); unset/invalid keeps the 5-minute default. A test hook to
	// force a prompt report/resync tick — production leaves it unset.
	repInterval := reportInterval
	if v := os.Getenv("AMX_REPORT_INTERVAL"); v != "" {
		if d, err := time.ParseDuration(v); err == nil && d > 0 {
			repInterval = d
		} else {
			log.Printf("AMX_REPORT_INTERVAL %q: invalid (using default)", v)
		}
	}
	go func() {
		t := time.NewTicker(repInterval)
		defer t.Stop()
		for {
			select {
			case <-ctx.Done():
				return
			case <-t.C:
				// Credential re-sync detection shares this cadence (§5.7): rotation is
				// rare and cross-server re-assignment is not latency-critical, so a
				// 5-minute detection window is ample and adds no extra goroutine.
				for _, rs := range resyncers {
					rs.Tick(ctx)
				}
				r, rerr := rep.BuildUsageReport(ctx, amxv1.UsageReport_TRIGGER_SCHEDULE)
				if rerr != nil {
					log.Printf("usage report: %v", rerr)
					continue
				}
				client.TrySend(&amxv1.AmaMessage{Msg: &amxv1.AmaMessage_Usage{Usage: r}})
			}
		}
	}()

	// Heartbeat ticker (design note §8): AMS touches last_seen only on hb, so
	// without this the server flips offline 90s after Register even though the
	// stream is healthy (usage reports do not count as liveness). TrySend drops
	// the beat while disconnected — harmless, the next one supersedes it.
	hbInterval := heartbeatInterval
	if v := os.Getenv("AMX_HEARTBEAT_INTERVAL"); v != "" {
		if d, err := time.ParseDuration(v); err == nil && d > 0 {
			hbInterval = d
		} else {
			log.Printf("AMX_HEARTBEAT_INTERVAL %q: invalid (using default)", v)
		}
	}
	// Host-metrics sampler for the heartbeat (§8): stateful for the CPU delta,
	// so one instance is shared across beats. Collection is best-effort — a
	// sample failure (or a non-Linux host) leaves Metrics nil and the beat still
	// carries liveness; AMS then keeps its previous columns rather than seeing 0%.
	// AMX_METRICS_DISK_PATH points DISK% at a specific volume (default "/").
	sampler := metrics.NewSampler(os.Getenv("AMX_METRICS_DISK_PATH"))
	go func() {
		t := time.NewTicker(hbInterval)
		defer t.Stop()
		for {
			select {
			case <-ctx.Done():
				return
			case <-t.C:
				hb := &amxv1.Heartbeat{
					AgentId:     agentID,
					SwitchMode:  handler.SwitchMode(),
					OutboxDepth: uint32(outbox.Depth()),
				}
				if s, serr := sampler.Sample(); serr == nil {
					hb.Metrics = &amxv1.Heartbeat_SystemMetrics{
						CpuPct:  s.CPUPct,
						MemPct:  s.MemPct,
						DiskPct: s.DiskPct,
					}
				}
				client.TrySend(&amxv1.AmaMessage{Msg: &amxv1.AmaMessage_Hb{Hb: hb}})
			}
		}
	}()

	// Live AccountEvent drain. The scheduler tick and the manual-switch handler
	// enqueue events to the Outbox; OnConnect flushes on reconnect, but a
	// long-lived session also needs a live drain so an at-limit switch reaches AMS
	// promptly rather than waiting for a reconnect that may not come. The send is
	// confirmed: SendConfirmed yields the stream.Send result, and the outbox
	// deletes an event from disk only on nil (C1 W2). A teardown resolves the wait
	// with an error, so the event stays on disk and is retried on reconnect.
	go func() {
		t := time.NewTicker(eventFlushInterval)
		defer t.Stop()
		for {
			select {
			case <-ctx.Done():
				return
			case <-t.C:
				if outbox.Depth() == 0 {
					continue
				}
				_ = outbox.Flush(func(ev *amxv1.AccountEvent) error {
					done, ok := client.SendConfirmed(&amxv1.AmaMessage{Msg: &amxv1.AmaMessage_Event{Event: ev}})
					if !ok {
						return errOutboxSendUnavailable
					}
					return <-done
				})
			}
		}
	}()

	// Command loop: verify -> apply -> ack.
	for {
		select {
		case <-ctx.Done():
			return nil
		case cmd := <-client.Recv():
			// A nil ack means the handler already sent it (self_update, which acks
			// from inside its critical section and then execs).
			if ack := handler.Handle(ctx, cmd); ack != nil {
				if err := client.Send(&amxv1.AmaMessage{Msg: &amxv1.AmaMessage_Ack{Ack: ack}}); err != nil {
					log.Printf("ack send: %v", err)
				}
			}
			// A RequestReport also produces a UsageReport (reading is idempotent).
			if _, ok := cmd.GetCmd().(*amxv1.AmsCommand_ReqReport); ok {
				if r, rerr := rep.BuildUsageReport(ctx, amxv1.UsageReport_TRIGGER_AMS_QUERY); rerr == nil {
					r.InResponseToCommandId = cmd.GetCommandId()
					_ = client.Send(&amxv1.AmaMessage{Msg: &amxv1.AmaMessage_Usage{Usage: r}})
				}
			}
		}
	}
}

func env(key, def string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return def
}

func hostname() string {
	h, _ := os.Hostname()
	return h
}

// maybeRegisterCodex wires the codex provider into the bridge registry only when
// its config home is set (driver.ConfigHome, from AMX_CODEX_HOME). It returns the
// registered bridge, or nil when codex is unconfigured — in which case the map is
// left untouched, so the agent stays claude-only exactly as before. Split out so
// the env gate is unit-testable without standing up the whole daemon.
func maybeRegisterCodex(bridges map[string]provider.Bridge, drv provider.Driver) provider.Bridge {
	if drv.ConfigHome() == "" {
		return nil
	}
	b := codex.NewBridge(drv)
	bridges[drv.Name()] = b
	return b
}
