// Command ama is the AMA daemon entrypoint. It wires transport, store, command,
// bridge, reporter, and the P3 scheduler together and opens the Session (Register
// -> SessionSetup -> command loop). The scheduler drives the auto-switch tick
// when the server is in mode=auto (design note §2, §6).
package main

import (
	"context"
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
	"github.com/2kwanghee/AMX/ama-agent/internal/scheduler"
	"github.com/2kwanghee/AMX/ama-agent/internal/store"
	"github.com/2kwanghee/AMX/ama-agent/internal/transport"
	"github.com/2kwanghee/AMX/ama-agent/internal/tsamx"
	amxv1 "github.com/2kwanghee/AMX/contracts/gen/go"
)

// reportInterval is the usage-report cadence (SSOT §6.5, design note §1).
const reportInterval = 5 * time.Minute

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
	outbox := reporter.NewOutbox()

	// Engine lock (R3): the single mutex serializing every tsamx mutation
	// sequence across the scheduler tick and the command handlers (decision 4).
	engine := &sync.Mutex{}
	sched := scheduler.New(scheduler.Config{
		AgentID:  agentID,
		Bridge:   bridge,
		Reporter: rep,
		Outbox:   outbox,
		Engine:   engine,
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

	// On (re)connect, send Register first (SSOT §5.4). The auth is the enroll
	// token on first contact, else the server credential from a prior SessionSetup.
	client.OnConnect = func(send func(*amxv1.AmaMessage) error) error {
		reg := &amxv1.Register{
			AgentId:           agentID,
			ServerId:          serverID,
			Hostname:          hostname(),
			AgentVersion:      "p3",
			SwitchMode:        handler.SwitchMode(),
			AppliedCommandIds: applied.RecentIDs(),
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
	// supersedes it (design note §8).
	go func() {
		t := time.NewTicker(reportInterval)
		defer t.Stop()
		for {
			select {
			case <-ctx.Done():
				return
			case <-t.C:
				r, rerr := rep.BuildUsageReport(ctx, amxv1.UsageReport_TRIGGER_SCHEDULE)
				if rerr != nil {
					log.Printf("usage report: %v", rerr)
					continue
				}
				client.TrySend(&amxv1.AmaMessage{Msg: &amxv1.AmaMessage_Usage{Usage: r}})
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
