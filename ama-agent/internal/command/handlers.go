package command

import (
	"context"
	"errors"
	"fmt"

	"github.com/2kwanghee/AMX/ama-agent/internal/crypto"
	"github.com/2kwanghee/AMX/ama-agent/internal/store"
	"github.com/2kwanghee/AMX/ama-agent/internal/tsamx"
	amxv1 "github.com/2kwanghee/AMX/contracts/gen/go"
)

// tsamxAddRequest builds an AddRequest from a plaintext credential set. The
// caller wipes plaintext after Add returns; the bridge copies what it needs.
func tsamxAddRequest(ref *amxv1.AccountRef, organizationName string, plaintext []byte, enable bool) tsamx.AddRequest {
	return tsamx.AddRequest{
		Email:            ref.GetEmail(),
		AccountUUID:      ref.GetAccountUuid(),
		OrganizationName: organizationName,
		CredentialJSON:   plaintext,
		Enable:           enable,
	}
}

// handleSessionSetup injects KEKs and persists any promoted server credential.
// Per design note §3 rule 1, SessionSetup is NEVER suppressed by the applied
// log — AMS re-issues it every session, and AMA always applies it.
func (h *Handler) handleSessionSetup(_ context.Context, cmd *amxv1.AmsCommand, ss *amxv1.SessionSetup, ack *amxv1.CommandAck) *amxv1.CommandAck {
	// Enrollment promotion: store the long-lived credential for later Register.
	if sc := ss.GetServerCredential(); sc != "" {
		h.mu.Lock()
		h.serverCredential = sc
		h.mu.Unlock()
	}
	// Install KEKs (memory only, §6.2).
	for _, wk := range ss.GetKeys() {
		raw, err := crypto.UnwrapKEK(wk.GetWrappedKey())
		if err != nil {
			return reject(ack, "kek_unwrap", fmt.Errorf("key %q: %w", wk.GetKeyId(), err))
		}
		h.keks.Put(wk.GetKeyId(), raw)
		for i := range raw {
			raw[i] = 0
		}
	}
	if id := ss.GetActiveKeyId(); id != "" {
		if !h.keks.SetActive(id) {
			return reject(ack, "active_key_missing", fmt.Errorf("active_key_id %q not held", id))
		}
	}
	if rev := ss.GetRevokedKeyIds(); len(rev) > 0 {
		h.keks.Revoke(rev...)
	}
	// Not gated by applied.log, but still recorded for the Register hint set.
	converged(ack)
	h.record(ack, "session_setup", "", "")
	return ack
}

// handleDeliver decrypts the credential set, upserts the manifest, and installs
// the account via the tsamx bridge (SSOT §6.3 deliver, single critical section).
func (h *Handler) handleDeliver(ctx context.Context, cmd *amxv1.AmsCommand, d *amxv1.DeliverAccount, ack *amxv1.CommandAck) *amxv1.CommandAck {
	ref := d.GetAccount()
	if ref == nil || ref.GetAmsAccountId() == "" || ref.GetEmail() == "" {
		return reject(ack, "bad_account", errors.New("deliver missing account ref"))
	}
	amsID := ref.GetAmsAccountId()
	desired := d.GetDesiredStatus()
	wantEnabled := desired == amxv1.AllocationStatus_ALLOCATION_STATUS_ACTIVE

	// AAD over-binding guard (proto EncryptedCredential warning): the received
	// aad_* fields are COMPARISON ONLY. They must equal the locally derived
	// components; a mismatch is a relocated/forged record -> REJECTED. The AEAD
	// itself is opened with a LOCALLY derived AAD (in the store), never these.
	ec := d.GetEncryptedCredential()
	if ec == nil {
		return reject(ack, "bad_envelope", errors.New("deliver missing encrypted_credential"))
	}
	if got := ec.GetAadAmsAccountId(); got != "" && got != amsID {
		return reject(ack, "aad_mismatch", fmt.Errorf("aad_ams_account_id %q != %q", got, amsID))
	}
	if got := ec.GetAadAgentId(); got != "" && got != h.agentID {
		return reject(ack, "aad_mismatch", fmt.Errorf("aad_agent_id %q != this agent", got))
	}

	// Idempotency: if already applied AND the account is present in the manifest
	// with the desired status AND present in the local pool with the desired
	// enabled state, re-emit CONVERGED without re-running the effect (§3).
	if _, seen := h.applied.Lookup(cmd.GetCommandId()); seen {
		if rec, ok := h.store.Get(amsID); ok && rec.AllocationStatus == int32(desired) {
			if list, err := h.bridge.List(ctx); err == nil {
				for _, row := range list.Accounts {
					if row.Email == ref.GetEmail() && row.Disabled == !wantEnabled {
						h.setAccountState(ctx, ack, ref, desired)
						return converged(ack)
					}
				}
			}
		}
	}

	// Decrypt the credential set. The store seals under the active KEK; here we
	// need the plaintext to hand to the bridge. We open a transient record: seal
	// with the active KEK into the manifest first, then open it back for install.
	if !h.keks.HasKeys() {
		return reject(ack, "no_kek", store.ErrNoKEK)
	}

	// Build the record and seal the credential the AMS sent. We decrypt the
	// wire envelope using the delivered KEK named by key_id, with a LOCALLY
	// derived AAD — NOT ec.aad_* (over-binding defense).
	kek, ok := h.keks.Get(ec.GetKeyId())
	if !ok {
		return reject(ack, "no_kek", fmt.Errorf("key_id %q not held", ec.GetKeyId()))
	}
	aad := crypto.WireAAD(amsID, h.agentID)
	plaintext, err := crypto.Open(kek, ec.GetNonce(), ec.GetCiphertext(), aad)
	for i := range kek {
		kek[i] = 0
	}
	if err != nil {
		// Authentication failure = relocated/tampered record. Reject.
		return reject(ack, "decrypt_failed", err)
	}

	// Persist to the manifest (re-sealed under the active KEK, own nonce).
	rec := store.Record{
		AMSAccountID:     amsID,
		Email:            ref.GetEmail(),
		AccountUUID:      ref.GetAccountUuid(),
		AllocationStatus: int32(desired),
		OrganizationName: d.GetOrganizationName(),
	}
	if err := h.store.Upsert(rec, plaintext); err != nil {
		wipe(plaintext)
		return diverged(ack, "manifest_upsert", err)
	}

	// Install via the bridge (critical section, §6.3). CredentialJSON is the
	// plaintext set; the bridge writes it to the account's config home.
	addErr := h.bridge.Add(ctx, tsamxAddRequest(ref, d.GetOrganizationName(), plaintext, wantEnabled))
	wipe(plaintext) // wipe plaintext from memory (§6.3)
	if addErr != nil {
		h.setAccountState(ctx, ack, ref, desired)
		out := diverged(ack, "tsamx_add", addErr)
		h.record(out, "deliver", amsID, desired.String())
		return out
	}

	h.setAccountState(ctx, ack, ref, desired)
	converged(ack)
	h.record(ack, "deliver", amsID, desired.String())
	return ack
}

// handleRecall implements O2: default (purge_local_copy=false) disables the
// account and KEEPS the manifest record (marked INACTIVE) for fast re-assignment;
// purge=true removes it from the pool and deletes the record.
func (h *Handler) handleRecall(ctx context.Context, cmd *amxv1.AmsCommand, r *amxv1.RecallAccount, ack *amxv1.CommandAck) *amxv1.CommandAck {
	ref := r.GetAccount()
	if ref == nil || ref.GetAmsAccountId() == "" {
		return reject(ack, "bad_account", errors.New("recall missing account ref"))
	}
	amsID := ref.GetAmsAccountId()
	email := ref.GetEmail()

	if r.GetPurgeLocalCopy() {
		if err := h.bridge.Remove(ctx, email); err != nil {
			out := diverged(ack, "tsamx_remove", err)
			h.record(out, "recall", amsID, "purge")
			return out
		}
		if err := h.store.Remove(amsID); err != nil {
			out := diverged(ack, "manifest_remove", err)
			h.record(out, "recall", amsID, "purge")
			return out
		}
		h.setAccountState(ctx, ack, ref, amxv1.AllocationStatus_ALLOCATION_STATUS_ABSENT)
		converged(ack)
		h.record(ack, "recall", amsID, "purge")
		return ack
	}

	// Default: disable in the pool, keep the record but mark INACTIVE (§O2).
	if err := h.bridge.Disable(ctx, email); err != nil {
		out := diverged(ack, "tsamx_disable", err)
		h.record(out, "recall", amsID, "disable")
		return out
	}
	if err := h.store.SetStatus(amsID, int32(amxv1.AllocationStatus_ALLOCATION_STATUS_INACTIVE)); err != nil && !errors.Is(err, store.ErrNotFound) {
		out := diverged(ack, "manifest_status", err)
		h.record(out, "recall", amsID, "disable")
		return out
	}
	h.setAccountState(ctx, ack, ref, amxv1.AllocationStatus_ALLOCATION_STATUS_INACTIVE)
	converged(ack)
	h.record(ack, "recall", amsID, "disable")
	return ack
}

// handleSetActive maps activate/deactivate onto tsamx enable/disable and the
// manifest status.
func (h *Handler) handleSetActive(ctx context.Context, cmd *amxv1.AmsCommand, sa *amxv1.SetAccountActive, ack *amxv1.CommandAck) *amxv1.CommandAck {
	ref := sa.GetAccount()
	if ref == nil || ref.GetAmsAccountId() == "" {
		return reject(ack, "bad_account", errors.New("set_active missing account ref"))
	}
	amsID := ref.GetAmsAccountId()
	email := ref.GetEmail()
	var status amxv1.AllocationStatus
	var err error
	if sa.GetActive() {
		err = h.bridge.Enable(ctx, email)
		status = amxv1.AllocationStatus_ALLOCATION_STATUS_ACTIVE
	} else {
		err = h.bridge.Disable(ctx, email)
		status = amxv1.AllocationStatus_ALLOCATION_STATUS_INACTIVE
	}
	if err != nil {
		out := diverged(ack, "tsamx_set_active", err)
		h.record(out, "set_active", amsID, status.String())
		return out
	}
	// clear_quarantine (recover path) is a P3 concern; the bridge has no verb
	// for it yet. Record the intent but do not fail the command.
	if serr := h.store.SetStatus(amsID, int32(status)); serr != nil && !errors.Is(serr, store.ErrNotFound) {
		out := diverged(ack, "manifest_status", serr)
		h.record(out, "set_active", amsID, status.String())
		return out
	}
	h.setAccountState(ctx, ack, ref, status)
	converged(ack)
	h.record(ack, "set_active", amsID, status.String())
	return ack
}

// handleSetMode records the desired switch mode. P2 skeleton: the scheduler tick
// is not driven here (P3) — only the mode is stored and echoed.
func (h *Handler) handleSetMode(_ context.Context, cmd *amxv1.AmsCommand, sm *amxv1.SetSwitchMode, ack *amxv1.CommandAck) *amxv1.CommandAck {
	h.mu.Lock()
	h.switchMode = sm.GetMode()
	h.mu.Unlock()
	ack.SwitchMode = sm.GetMode()
	converged(ack)
	h.record(ack, "set_mode", "", sm.GetMode().String())
	return ack
}

// handleSwitchNow performs a manual switch. P2 skeleton: only the explicit
// account target is honored (strategy ranking is P3).
func (h *Handler) handleSwitchNow(ctx context.Context, cmd *amxv1.AmsCommand, sn *amxv1.SwitchNow, ack *amxv1.CommandAck) *amxv1.CommandAck {
	ref := sn.GetAccount()
	if ref == nil || ref.GetEmail() == "" {
		// strategy target is P3; report not-yet-converged rather than reject.
		ack.Detail = "switch_now strategy targeting is deferred to P3"
		return diverged(ack, "unsupported_target", nil)
	}
	if err := h.bridge.Switch(ctx, ref.GetEmail()); err != nil {
		return diverged(ack, "tsamx_switch", err)
	}
	h.setAccountState(ctx, ack, ref, amxv1.AllocationStatus_ALLOCATION_STATUS_ACTIVE)
	converged(ack)
	h.record(ack, "switch_now", ref.GetAmsAccountId(), ref.GetEmail())
	return ack
}

// handleReqReport acknowledges an immediate report request. The report itself is
// produced by the reporter; reading is idempotent and always "converged".
func (h *Handler) handleReqReport(_ context.Context, cmd *amxv1.AmsCommand, _ *amxv1.RequestReport, ack *amxv1.CommandAck) *amxv1.CommandAck {
	converged(ack)
	return ack
}

func wipe(b []byte) {
	for i := range b {
		b[i] = 0
	}
}
