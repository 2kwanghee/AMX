// Command ama is the AMA daemon entrypoint. It wires transport, store, command,
// bridge, reporter, and the P3 scheduler together and opens the Session (Register
// -> SessionSetup -> command loop). The scheduler drives the auto-switch tick
// when the server is in mode=auto (design note §2, §6).
package main

import (
	"context"
	"errors"
	"log"
	"os"
	"os/signal"
	"path/filepath"
	"sync"
	"syscall"
	"time"

	"github.com/2kwanghee/AMX/ama-agent/internal/command"
	"github.com/2kwanghee/AMX/ama-agent/internal/crypto"
	"github.com/2kwanghee/AMX/ama-agent/internal/reporter"
	"github.com/2kwanghee/AMX/ama-agent/internal/resync"
	"github.com/2kwanghee/AMX/ama-agent/internal/scheduler"
	"github.com/2kwanghee/AMX/ama-agent/internal/store"
	"github.com/2kwanghee/AMX/ama-agent/internal/transport"
	"github.com/2kwanghee/AMX/ama-agent/internal/tsamx"
	amxv1 "github.com/2kwanghee/AMX/contracts/gen/go"
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

func main() {
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

	keks := store.NewKEKHolder()
	st, err := store.Open(stateDir, agentID, keks)
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

	bridge := tsamx.NewExecBridge()
	rep := reporter.New(agentID, bridge, time.Now)
	// Resolve each reported account's AMS identity from the manifest (tsamx knows
	// only the email). Without ams_account_id in the report, AMS reconcile treats
	// every assigned account as absent and redelivers it in a loop, rewriting the
	// live credential file and racing the O9 re-sync.
	rep.SetIDResolver(func(email string) (string, string, bool) {
		rec, ok := st.FindByEmail(email)
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

	handler, err := command.New(command.Config{
		AgentID:          agentID,
		PublicKey:        pub,
		Store:            st,
		KEKs:             keks,
		Applied:          applied,
		Bridge:           bridge,
		Creds:            creds,
		Engine:           engine,
		Outbox:           outbox,
		SwitchController: sched,
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

	// O9 credential re-sync (§5.7): watch the active account's live credential for
	// a local refresh-token rotation and push the refreshed set back to AMS. The
	// credential file is CLAUDE_CONFIG_DIR/.credentials.json (the home the bridge
	// stages and tsamx refreshes in place). Empty config home disables re-sync.
	var credPath string
	if cfgDir := os.Getenv("CLAUDE_CONFIG_DIR"); cfgDir != "" {
		credPath = filepath.Join(cfgDir, ".credentials.json")
	}
	resyncer := resync.New(resync.Config{
		AgentID:          agentID,
		Store:            st,
		KEKs:             keks,
		Bridge:           bridge,
		Engine:           engine,
		CredentialsPath:  credPath,
		ServerCredential: handler.ServerCredential,
		Send: func(u *amxv1.CredentialUpdate) bool {
			return client.TrySend(&amxv1.AmaMessage{Msg: &amxv1.AmaMessage_CredUpdate{CredUpdate: u}})
		},
		Now:  time.Now,
		Logf: log.Printf,
	})

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
			AgentVersion:      "p3",
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
				resyncer.Tick(ctx)
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
	go func() {
		t := time.NewTicker(hbInterval)
		defer t.Stop()
		for {
			select {
			case <-ctx.Done():
				return
			case <-t.C:
				client.TrySend(&amxv1.AmaMessage{Msg: &amxv1.AmaMessage_Hb{Hb: &amxv1.Heartbeat{
					AgentId:     agentID,
					SwitchMode:  handler.SwitchMode(),
					OutboxDepth: uint32(outbox.Depth()),
				}}})
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
			ack := handler.Handle(ctx, cmd)
			if err := client.Send(&amxv1.AmaMessage{Msg: &amxv1.AmaMessage_Ack{Ack: ack}}); err != nil {
				log.Printf("ack send: %v", err)
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
