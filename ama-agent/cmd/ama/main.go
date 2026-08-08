// Command ama is the AMA daemon entrypoint. P2 wires transport, store, command,
// bridge, and reporter together and opens the Session (Register -> SessionSetup
// -> command loop). The scheduler / auto-switch tick is a P3 concern and is NOT
// driven here (design note §6, §7).
package main

import (
	"context"
	"log"
	"os"
	"os/signal"
	"path/filepath"
	"syscall"
	"time"

	"github.com/2kwanghee/AMX/ama-agent/internal/command"
	"github.com/2kwanghee/AMX/ama-agent/internal/crypto"
	"github.com/2kwanghee/AMX/ama-agent/internal/reporter"
	"github.com/2kwanghee/AMX/ama-agent/internal/store"
	"github.com/2kwanghee/AMX/ama-agent/internal/transport"
	"github.com/2kwanghee/AMX/ama-agent/internal/tsamx"
	amxv1 "github.com/2kwanghee/AMX/contracts/gen/go"
)

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

	bridge := tsamx.NewExecBridge()

	handler, err := command.New(command.Config{
		AgentID:   agentID,
		PublicKey: pub,
		Store:     st,
		KEKs:      keks,
		Applied:   applied,
		Bridge:    bridge,
	})
	if err != nil {
		return err
	}
	rep := reporter.New(agentID, bridge, time.Now)

	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()

	client := transport.Dial(amsAddr)
	defer client.Close()

	// On (re)connect, send Register first (SSOT §5.4). The auth is the enroll
	// token on first contact, else the server credential from a prior SessionSetup.
	client.OnConnect = func(send func(*amxv1.AmaMessage) error) error {
		reg := &amxv1.Register{
			AgentId:           agentID,
			ServerId:          serverID,
			Hostname:          hostname(),
			AgentVersion:      "p2",
			AppliedCommandIds: applied.RecentIDs(),
		}
		if sc := handler.ServerCredential(); sc != "" {
			reg.Auth = &amxv1.Register_ServerCredential{ServerCredential: sc}
		} else if enrollToken != "" {
			reg.Auth = &amxv1.Register_EnrollToken{EnrollToken: enrollToken}
		}
		return send(&amxv1.AmaMessage{Msg: &amxv1.AmaMessage_Register{Register: reg}})
	}

	go func() {
		if err := client.Run(ctx); err != nil && ctx.Err() == nil {
			log.Printf("transport: %v", err)
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
