// Package command implements the AMA command dispatcher (design note §3, SSOT
// §6.3): verify signature -> freshness -> idempotency (applied.log) -> store /
// tsamx bridge -> convergence ack. Every command is idempotent; a re-send whose
// effect is already in place re-emits CONVERGED without re-running the effect.
package command

import (
	"context"
	"crypto/ed25519"
	"errors"
	"fmt"
	"sync"
	"time"

	"github.com/2kwanghee/AMX/ama-agent/internal/crypto"
	"github.com/2kwanghee/AMX/ama-agent/internal/store"
	"github.com/2kwanghee/AMX/ama-agent/internal/tsamx"
	amxv1 "github.com/2kwanghee/AMX/contracts/gen/go"
	"google.golang.org/protobuf/types/known/timestamppb"
)

// DefaultAcceptanceWindow bounds how stale a signed command's issued_at may be
// before it is rejected as a possible replay (SSOT §6.3 / proto AmsCommand.issued_at).
const DefaultAcceptanceWindow = 5 * time.Minute

// Handler processes AmsCommands. It is safe for use from a single session
// goroutine; state it mutates (store, applied log, KEK holder) is itself locked.
type Handler struct {
	agentID string
	pub     ed25519.PublicKey
	store   *store.Store
	keks    *store.KEKHolder
	applied *store.AppliedLog
	bridge  tsamx.Bridge

	window time.Duration
	now    func() time.Time

	mu               sync.Mutex
	serverCredential string // persisted from SessionSetup; presented on Register
	switchMode       amxv1.SwitchMode
}

// Config assembles a Handler.
type Config struct {
	AgentID          string
	PublicKey        ed25519.PublicKey
	Store            *store.Store
	KEKs             *store.KEKHolder
	Applied          *store.AppliedLog
	Bridge           tsamx.Bridge
	AcceptanceWindow time.Duration // 0 -> DefaultAcceptanceWindow
	Now              func() time.Time
}

// New validates cfg and returns a Handler.
func New(cfg Config) (*Handler, error) {
	if cfg.AgentID == "" {
		return nil, errors.New("command: empty AgentID")
	}
	if len(cfg.PublicKey) != ed25519.PublicKeySize {
		return nil, errors.New("command: invalid public key")
	}
	if cfg.Store == nil || cfg.KEKs == nil || cfg.Applied == nil || cfg.Bridge == nil {
		return nil, errors.New("command: nil dependency")
	}
	w := cfg.AcceptanceWindow
	if w == 0 {
		w = DefaultAcceptanceWindow
	}
	now := cfg.Now
	if now == nil {
		now = time.Now
	}
	return &Handler{
		agentID: cfg.AgentID,
		pub:     cfg.PublicKey,
		store:   cfg.Store,
		keks:    cfg.KEKs,
		applied: cfg.Applied,
		bridge:  cfg.Bridge,
		window:  w,
		now:     now,
	}, nil
}

// ServerCredential returns the long-lived credential last delivered by
// SessionSetup, presented on the next Register (empty until enrollment promotes).
func (h *Handler) ServerCredential() string {
	h.mu.Lock()
	defer h.mu.Unlock()
	return h.serverCredential
}

// SwitchMode returns the last mode set by SetSwitchMode.
func (h *Handler) SwitchMode() amxv1.SwitchMode {
	h.mu.Lock()
	defer h.mu.Unlock()
	return h.switchMode
}

// Handle processes one command and returns the convergence ack.
func (h *Handler) Handle(ctx context.Context, cmd *amxv1.AmsCommand) *amxv1.CommandAck {
	ack := &amxv1.CommandAck{
		CommandId:  cmd.GetCommandId(),
		AgentId:    h.agentID,
		ObservedAt: timestamppb.New(h.now().UTC()),
	}

	// 1. Signature. Authority is enforced by signature, not by encryption (§6.2).
	if err := crypto.VerifyCommand(h.pub, cmd); err != nil {
		return reject(ack, "signature_invalid", err)
	}
	// 2. Freshness (replay defense).
	if err := h.checkFreshness(cmd); err != nil {
		return reject(ack, "stale_command", err)
	}

	switch c := cmd.GetCmd().(type) {
	case *amxv1.AmsCommand_SessionSetup:
		return h.handleSessionSetup(ctx, cmd, c.SessionSetup, ack)
	case *amxv1.AmsCommand_Deliver:
		return h.handleDeliver(ctx, cmd, c.Deliver, ack)
	case *amxv1.AmsCommand_Recall:
		return h.handleRecall(ctx, cmd, c.Recall, ack)
	case *amxv1.AmsCommand_SetActive:
		return h.handleSetActive(ctx, cmd, c.SetActive, ack)
	case *amxv1.AmsCommand_SetMode:
		return h.handleSetMode(ctx, cmd, c.SetMode, ack)
	case *amxv1.AmsCommand_SwitchNow:
		return h.handleSwitchNow(ctx, cmd, c.SwitchNow, ack)
	case *amxv1.AmsCommand_ReqReport:
		return h.handleReqReport(ctx, cmd, c.ReqReport, ack)
	default:
		return reject(ack, "unknown_command", errors.New("empty or unknown command payload"))
	}
}

func (h *Handler) checkFreshness(cmd *amxv1.AmsCommand) error {
	issued := cmd.GetIssuedAt()
	if issued == nil {
		// Nothing to evaluate; a stricter deployment may require issued_at.
		return nil
	}
	t := issued.AsTime()
	skew := h.now().UTC().Sub(t)
	if skew < 0 {
		skew = -skew
	}
	if skew > h.window {
		return fmt.Errorf("issued_at %s outside acceptance window %s", t, h.window)
	}
	return nil
}

// --- helpers ---------------------------------------------------------------

func reject(ack *amxv1.CommandAck, code string, err error) *amxv1.CommandAck {
	ack.Convergence = amxv1.CommandAck_CONVERGENCE_REJECTED
	ack.ErrorCode = code
	if err != nil {
		ack.Detail = err.Error()
	}
	return ack
}

func diverged(ack *amxv1.CommandAck, code string, err error) *amxv1.CommandAck {
	ack.Convergence = amxv1.CommandAck_CONVERGENCE_DIVERGED
	ack.ErrorCode = code
	if err != nil {
		ack.Detail = err.Error()
	}
	return ack
}

func converged(ack *amxv1.CommandAck) *amxv1.CommandAck {
	ack.Convergence = amxv1.CommandAck_CONVERGENCE_CONVERGED
	return ack
}

// record logs the command in the applied.log sidecar (idempotency + Register).
func (h *Handler) record(ack *amxv1.CommandAck, kind, target, desired string) {
	_ = h.applied.Append(store.AppliedEntry{
		CommandID:   ack.CommandId,
		Kind:        kind,
		Target:      target,
		Desired:     desired,
		Convergence: ack.Convergence.String(),
		AppliedAt:   h.now().UTC(),
	})
}

// setAccountState fills ack.account_state from the manifest record + live pool.
func (h *Handler) setAccountState(ctx context.Context, ack *amxv1.CommandAck, ref *amxv1.AccountRef, status amxv1.AllocationStatus) {
	au := &amxv1.AccountUsage{
		Account:          ref,
		AllocationStatus: status,
	}
	if list, err := h.bridge.List(ctx); err == nil {
		for _, row := range list.Accounts {
			if row.Email == ref.GetEmail() {
				au.IsCurrent = row.Active
				break
			}
		}
	}
	ack.AccountState = au
}
