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
	"github.com/2kwanghee/AMX/ama-agent/internal/reporter"
	"github.com/2kwanghee/AMX/ama-agent/internal/store"
	"github.com/2kwanghee/AMX/ama-agent/internal/tsamx"
	amxv1 "github.com/2kwanghee/AMX/contracts/gen/go"
	"google.golang.org/protobuf/types/known/timestamppb"
)

// SwitchController is the scheduler control surface the Handler drives from
// SetSwitchMode (auto -> Start, manual -> Stop). Kept as an interface so the
// command package does not import the scheduler (avoids an import cycle).
type SwitchController interface {
	Start()
	Stop()
}

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
	creds   *store.CredentialSidecar // nil -> memory-only (tests)

	// engine serializes every tsamx mutation sequence with the scheduler tick
	// (design decision 4, R3). Shared with the Scheduler; never nil.
	engine    *sync.Mutex
	outbox    *reporter.Outbox // AccountEvent sink for manual switches; may be nil
	switchCtl SwitchController // scheduler start/stop; may be nil (tests)

	window time.Duration
	now    func() time.Time

	mu sync.Mutex
	// serverCredential is the long-lived credential minted by AMS during enroll
	// promotion. It is presented on the next Register; it is persisted to the
	// credential sidecar (when configured) so a restart re-authenticates over
	// path B — AMS has already burned the one-shot enroll_token by then.
	serverCredential string
	switchMode       amxv1.SwitchMode
	// defaultStrategy is the strategy SetPolicy delivered; used by auto/switch_now
	// when no explicit strategy is named (O4-C). Memory-only, re-asserted each
	// session.
	defaultStrategy amxv1.SwitchNow_SwitchStrategy
	// lastSwitchedAt records the time of the last manual switch (informational,
	// design note §3). Memory-only.
	lastSwitchedAt time.Time
	// lastPolicyIssuedAt is the issued_at of the most recently applied SetPolicy.
	// SetPolicy is re-asserted every session and is NOT gated by the applied log
	// (re-application is idempotent), so this memory-only high-water mark is the
	// only thing that stops a captured older SetPolicy — resent inside the
	// freshness window — from rolling the live threshold BACK to a past value
	// (ADVERSARY R3: a threshold 90 recaptured after the operator lowered it to
	// 50). Only strictly-newer-or-equal issued_at is applied; strictly older is
	// ignored.
	lastPolicyIssuedAt time.Time
}

// Config assembles a Handler.
type Config struct {
	AgentID          string
	PublicKey        ed25519.PublicKey
	Store            *store.Store
	KEKs             *store.KEKHolder
	Applied          *store.AppliedLog
	Bridge           tsamx.Bridge
	Creds            *store.CredentialSidecar // optional; nil keeps the credential in memory only
	AcceptanceWindow time.Duration            // 0 -> DefaultAcceptanceWindow
	Now              func() time.Time
	// Engine is the shared tsamx serialization lock (R3). Nil -> the Handler
	// allocates its own (fine for tests with no scheduler).
	Engine *sync.Mutex
	// Outbox is where manual-switch AccountEvents are queued. Nil -> no event.
	Outbox *reporter.Outbox
	// SwitchController starts/stops the scheduler on SetSwitchMode. Nil -> mode is
	// only recorded (P2 behavior).
	SwitchController SwitchController
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
	engine := cfg.Engine
	if engine == nil {
		engine = &sync.Mutex{}
	}
	h := &Handler{
		agentID:   cfg.AgentID,
		pub:       cfg.PublicKey,
		store:     cfg.Store,
		keks:      cfg.KEKs,
		applied:   cfg.Applied,
		bridge:    cfg.Bridge,
		creds:     cfg.Creds,
		engine:    engine,
		outbox:    cfg.Outbox,
		switchCtl: cfg.SwitchController,
		window:    w,
		now:       now,
	}
	// Recover a credential persisted by a previous run so the first Register
	// after a restart authenticates over path B (§7 enroll handshake).
	if cfg.Creds != nil {
		if sc, err := cfg.Creds.Load(); err == nil && sc != "" {
			h.serverCredential = sc
		}
	}
	return h, nil
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

// DefaultStrategy returns the strategy last delivered by SetPolicy (O4-C).
func (h *Handler) DefaultStrategy() amxv1.SwitchNow_SwitchStrategy {
	h.mu.Lock()
	defer h.mu.Unlock()
	return h.defaultStrategy
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
	// 2. Recipient binding. target_agent_id is inside the signed payload; a
	// command minted for another agent (or an unbound one) must never execute
	// here, even with a valid AMS signature — this blocks cross-agent /
	// cross-tenant re-injection of a captured command. Applies to every command,
	// SessionSetup included.
	if cmd.GetTargetAgentId() != h.agentID {
		return reject(ack, "wrong_recipient", fmt.Errorf("target_agent_id %q is not this agent", cmd.GetTargetAgentId()))
	}
	// 3. Freshness (replay defense).
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
	case *amxv1.AmsCommand_SetPolicy:
		return h.handleSetPolicy(ctx, cmd, c.SetPolicy, ack)
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
		// issued_at is mandatory: without it there is no freshness bound, so a
		// captured command could be replayed indefinitely. Reject rather than
		// skip the check (ADVERSARY: absent issued_at bypassed freshness).
		return errors.New("issued_at is required")
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

// alreadyApplied reports whether cmdID is already recorded in the applied log
// with a CONVERGED result. A replay of such a command — a valid signature reused
// inside the freshness window — MUST re-emit the prior convergence WITHOUT
// re-running a non-idempotent effect (recall-purge, or a deactivate the operator
// has since reversed). ADVERSARY: an in-window replay otherwise re-ran the effect.
func (h *Handler) alreadyApplied(cmdID string) bool {
	entry, seen := h.applied.Lookup(cmdID)
	return seen && entry.Convergence == amxv1.CommandAck_CONVERGENCE_CONVERGED.String()
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
