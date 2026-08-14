package command

import (
	"context"
	"errors"
	"fmt"

	"github.com/2kwanghee/AMX/ama-agent/internal/crypto"
	"github.com/2kwanghee/AMX/ama-agent/internal/provider"
	"github.com/2kwanghee/AMX/ama-agent/internal/reporter"
	"github.com/2kwanghee/AMX/ama-agent/internal/store"
	amxv1 "github.com/2kwanghee/AMX/contracts/gen/go"
	"google.golang.org/protobuf/types/known/timestamppb"
)

// tsamxAddRequest builds an AddRequest from a plaintext credential set. The
// caller wipes plaintext after Add returns; the bridge copies what it needs.
func tsamxAddRequest(ref *amxv1.AccountRef, organizationName string, plaintext []byte, enable bool) provider.AddRequest {
	return provider.AddRequest{
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
	// Enrollment promotion: persist the long-lived credential for later Register.
	// It MUST reach disk before we ack — AMS burns the one-shot enroll_token on
	// mint, so losing this to a crash would lock the agent out permanently.
	if sc := ss.GetServerCredential(); sc != "" {
		h.mu.Lock()
		h.serverCredential = sc
		h.mu.Unlock()
		if h.creds != nil {
			if err := h.creds.Save(sc); err != nil {
				return diverged(ack, "credential_persist", err)
			}
		}
	}
	// Install KEKs (memory only, §6.2). Each wrapped_key is a NaCl sealed box AMS
	// sealed to this session's ephemeral X25519 public key (C2 §7); unwrap it with
	// the matching private key. AMA always advertises a public key, so a raw KEK
	// (or one sealed to a stale key) fails to open and is rejected — downgrade
	// defense.
	pub, priv := h.sessionKeyPair()
	if len(ss.GetKeys()) > 0 && (pub == nil || priv == nil) {
		return reject(ack, "no_session_key", errors.New("SessionSetup carries keys but no session key pair is established"))
	}
	for _, wk := range ss.GetKeys() {
		raw, err := crypto.UnwrapKEK(wk.GetWrappedKey(), pub, priv)
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

	// Provider routing (P3): select the bridge for this account's provider BEFORE
	// any lock, decryption, or effect. An unregistered provider must not have its
	// credential decrypted or handed to any bridge — that fail-closed guard is the
	// security point of the shim — so bail with a DIVERGED provider_unsupported ack
	// while the credential is still an opaque envelope.
	br := h.bridgeFor(ref.GetProvider())
	if br == nil {
		out := diverged(ack, "provider_unsupported", fmt.Errorf("provider %q is not registered", ref.GetProvider()))
		h.record(out, "deliver", amsID, "")
		return out
	}

	// B1b: acquire the cross-process deliver lock BEFORE the engine lock. Its
	// acquisition is bounded and non-blocking (fail-open), so a runner holding the
	// shared lock delays only THIS deliver's lock — never the engine lock — and the
	// scheduler tick plus every other command keep running (the engine can never
	// freeze). The lock wraps the engine-locked swap below and, because it is
	// deferred first, is released AFTER h.engine.Unlock on return.
	releaseLock := br.DeliverLock(ctx)
	defer func() { _ = releaseLock() }()

	// Engine lock (R3): the whole deliver critical section is serialized against
	// the scheduler tick and other mutating commands (design decision 4).
	h.engine.Lock()
	defer h.engine.Unlock()
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
			if list, err := br.List(ctx); err == nil {
				for _, row := range list.Accounts {
					if row.Email == ref.GetEmail() && row.Disabled == !wantEnabled {
						h.setAccountState(ctx, br, ack, ref, desired)
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
		Provider:         ref.GetProvider(),
		AccountUUID:      ref.GetAccountUuid(),
		AllocationStatus: int32(desired),
		OrganizationName: d.GetOrganizationName(),
	}
	if err := h.store.Upsert(rec, plaintext); err != nil {
		wipe(plaintext)
		return diverged(ack, "manifest_upsert", err)
	}

	// Record the account the runner (Claude Code) is currently reading BEFORE Add.
	// `tsamx add` makes the freshly-staged slot active (exec.go Add), so if we do
	// not restore, the runner would be left on the NEW account and overcharged on
	// every deliver (§6.3 critical section warning). deliver only adds to the pool
	// and sets enable/disable — it never changes which account is live; activation
	// is auto/switch_now's job, regardless of desired ACTIVE/INACTIVE. The
	// credential-swap span (Add -> restore) is wrapped by the B1b deliver lock
	// acquired above and runs under the engine lock, so no scheduler tick can move
	// `active` mid-flight.
	//
	// A Status read FAILURE (statusErr) means we cannot know which account the
	// runner was on, so after Add the new account may be left active and we cannot
	// safely restore. Rather than report a false CONVERGED (silent over-charge), we
	// surface it as diverged below so AMS is alerted (B1b review item 4).
	before, statusErr := br.Status(ctx)
	prevActive := ""
	if statusErr == nil && before != nil {
		prevActive = before.ActiveEmail
	}

	// Install via the bridge (critical section, §6.3). CredentialJSON is the
	// plaintext set; the bridge writes it to the account's config home.
	addErr := br.Add(ctx, tsamxAddRequest(ref, d.GetOrganizationName(), plaintext, wantEnabled))
	wipe(plaintext) // wipe plaintext from memory (§6.3)
	if addErr != nil {
		h.setAccountState(ctx, br, ack, ref, desired)
		out := diverged(ack, "tsamx_add", addErr)
		h.record(out, "deliver", amsID, desired.String())
		return out
	}

	// Status was unreadable before Add: the new account may now be the live one and
	// we have no safe restore target. Report diverged so the possible over-charge is
	// not hidden behind a CONVERGED ack (B1b review item 4).
	if statusErr != nil {
		h.setAccountState(ctx, br, ack, ref, desired)
		out := diverged(ack, "active_unknown", statusErr)
		h.record(out, "deliver", amsID, desired.String())
		return out
	}

	// Restore the runner's previously-active account (§6.3 "tsamx switch <이전 활성>
	// 복귀"). Only when a *different* account was active before — the first-account
	// case (prevActive == "") has none to return to, so the new slot may stay
	// active. A failed restore leaves the runner on the new account, an overcharge
	// risk, so it is surfaced as diverged (not converged).
	if prevActive != "" && prevActive != ref.GetEmail() {
		if serr := br.Switch(ctx, prevActive); serr != nil {
			h.setAccountState(ctx, br, ack, ref, desired)
			out := diverged(ack, "tsamx_restore_active", serr)
			h.record(out, "deliver", amsID, desired.String())
			return out
		}
	}

	h.setAccountState(ctx, br, ack, ref, desired)
	converged(ack)
	h.record(ack, "deliver", amsID, desired.String())
	return ack
}

// otherSwitchTarget picks another managed account to move the runner onto
// before we remove the active one. Prefer an enabled (rotation-eligible) slot;
// fall back to any other managed account. Empty string = nothing else to
// switch to.
func (h *Handler) otherSwitchTarget(ctx context.Context, br provider.Bridge, exclude string) string {
	list, err := br.List(ctx)
	if err != nil || list == nil {
		return ""
	}
	fallback := ""
	for _, row := range list.Accounts {
		if row.Email == "" || row.Email == exclude {
			continue
		}
		if !row.Disabled {
			return row.Email
		}
		if fallback == "" {
			fallback = row.Email
		}
	}
	return fallback
}

// accountInPool reports whether the pool currently lists the account. Purge
// recall uses it to stay idempotent: an account already gone from the pool
// (operator removed it directly, or a prior recall partially succeeded) must
// converge, not fail on a not-found `tsamx remove`. On a List error it returns
// true so the removal is still attempted — List failure is a separate fault.
func (h *Handler) accountInPool(ctx context.Context, br provider.Bridge, email string) bool {
	list, err := br.List(ctx)
	if err != nil || list == nil {
		return true
	}
	for _, row := range list.Accounts {
		if row.Email == email {
			return true
		}
	}
	return false
}
// handleRecall implements O2. Since the 2026-08-14 change AMS always sends
// purge=true (recall = full detach): the account is removed from the pool and
// its manifest record deleted, history living only in the AMS-side detached row.
// The purge=false branch (disable + keep INACTIVE record) is retained for
// backward compatibility with any in-flight command from an older server.
func (h *Handler) handleRecall(ctx context.Context, cmd *amxv1.AmsCommand, r *amxv1.RecallAccount, ack *amxv1.CommandAck) *amxv1.CommandAck {
	h.engine.Lock()
	defer h.engine.Unlock()
	ref := r.GetAccount()
	if ref == nil || ref.GetAmsAccountId() == "" {
		return reject(ack, "bad_account", errors.New("recall missing account ref"))
	}
	amsID := ref.GetAmsAccountId()
	email := ref.GetEmail()

	// Provider routing (P3): an unregistered provider gets no bridge effect.
	br := h.bridgeFor(ref.GetProvider())
	if br == nil {
		out := diverged(ack, "provider_unsupported", fmt.Errorf("provider %q is not registered", ref.GetProvider()))
		h.record(out, "recall", amsID, "")
		return out
	}

	// Replay gate (§3): a previously-CONVERGED command_id is a no-op that
	// re-emits the convergence, so an in-window resend cannot re-purge/re-disable.
	if h.alreadyApplied(cmd.GetCommandId()) {
		status := amxv1.AllocationStatus_ALLOCATION_STATUS_INACTIVE
		if r.GetPurgeLocalCopy() {
			status = amxv1.AllocationStatus_ALLOCATION_STATUS_ABSENT
		}
		h.setAccountState(ctx, br, ack, ref, status)
		return converged(ack)
	}

	if r.GetPurgeLocalCopy() {
		// §6.3: if the account being purged is the one the runner is live on,
		// switch away FIRST. Neither `tsamx remove` nor `tsamx disable`
		// auto-switches (a disabled/removed slot stays live until switched away),
		// so removing the active account without this would leave the runner
		// reading a deleted credential. With no other account to move to
		// (single-account host) we proceed to remove — the runner losing its
		// account is the meaning of recall there.
		if before, statusErr := br.Status(ctx); statusErr == nil && before != nil && before.ActiveEmail == email {
			if target := h.otherSwitchTarget(ctx, br, email); target != "" {
				if serr := br.Switch(ctx, target); serr != nil {
					out := diverged(ack, "tsamx_switch_before_purge", serr)
					h.record(out, "recall", amsID, "purge")
					return out
				}
			}
		}
		// 멱등: 대상이 이미 풀에 없으면 remove를 건너뛴다. 회수의 목적은 부재이므로
		// 이미 부재면(운영자가 tsamx에서 직접 지웠거나 이전 회수가 부분 성공한 경우)
		// remove의 not-found 실패로 recalling에 갇히지 않고 성공으로 수렴해야 한다.
		if h.accountInPool(ctx, br, email) {
			if err := br.Remove(ctx, email); err != nil {
				out := diverged(ack, "tsamx_remove", err)
				h.record(out, "recall", amsID, "purge")
				return out
			}
		}
		if err := h.store.Remove(amsID); err != nil {
			out := diverged(ack, "manifest_remove", err)
			h.record(out, "recall", amsID, "purge")
			return out
		}
		h.setAccountState(ctx, br, ack, ref, amxv1.AllocationStatus_ALLOCATION_STATUS_ABSENT)
		converged(ack)
		h.record(ack, "recall", amsID, "purge")
		return ack
	}

	// Default: disable in the pool, keep the record but mark INACTIVE (§O2).
	if err := br.Disable(ctx, email); err != nil {
		out := diverged(ack, "tsamx_disable", err)
		h.record(out, "recall", amsID, "disable")
		return out
	}
	if err := h.store.SetStatus(amsID, int32(amxv1.AllocationStatus_ALLOCATION_STATUS_INACTIVE)); err != nil && !errors.Is(err, store.ErrNotFound) {
		out := diverged(ack, "manifest_status", err)
		h.record(out, "recall", amsID, "disable")
		return out
	}
	h.setAccountState(ctx, br, ack, ref, amxv1.AllocationStatus_ALLOCATION_STATUS_INACTIVE)
	converged(ack)
	h.record(ack, "recall", amsID, "disable")
	return ack
}

// handleSetActive maps activate/deactivate onto tsamx enable/disable and the
// manifest status.
func (h *Handler) handleSetActive(ctx context.Context, cmd *amxv1.AmsCommand, sa *amxv1.SetAccountActive, ack *amxv1.CommandAck) *amxv1.CommandAck {
	h.engine.Lock()
	defer h.engine.Unlock()
	ref := sa.GetAccount()
	if ref == nil || ref.GetAmsAccountId() == "" {
		return reject(ack, "bad_account", errors.New("set_active missing account ref"))
	}
	amsID := ref.GetAmsAccountId()
	email := ref.GetEmail()
	status := amxv1.AllocationStatus_ALLOCATION_STATUS_INACTIVE
	if sa.GetActive() {
		status = amxv1.AllocationStatus_ALLOCATION_STATUS_ACTIVE
	}

	// Provider routing (P3): an unregistered provider gets no bridge effect.
	br := h.bridgeFor(ref.GetProvider())
	if br == nil {
		out := diverged(ack, "provider_unsupported", fmt.Errorf("provider %q is not registered", ref.GetProvider()))
		h.record(out, "set_active", amsID, status.String())
		return out
	}

	// Replay gate (§3): re-emit a previously-CONVERGED result without toggling
	// the pool again, so a resend cannot flip a state the operator has changed.
	if h.alreadyApplied(cmd.GetCommandId()) {
		h.setAccountState(ctx, br, ack, ref, status)
		return converged(ack)
	}

	var err error
	if sa.GetActive() {
		err = br.Enable(ctx, email)
	} else {
		err = br.Disable(ctx, email)
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
	h.setAccountState(ctx, br, ack, ref, status)
	converged(ack)
	h.record(ack, "set_active", amsID, status.String())
	return ack
}

// handleSetMode records the desired switch mode and starts/stops the scheduler:
// auto -> start the tick loop, manual -> stop it (design note §2). Not gated by
// the applied log — AMS re-asserts the mode every session (decision 5).
func (h *Handler) handleSetMode(_ context.Context, cmd *amxv1.AmsCommand, sm *amxv1.SetSwitchMode, ack *amxv1.CommandAck) *amxv1.CommandAck {
	mode := sm.GetMode()
	h.mu.Lock()
	h.switchMode = mode
	h.mu.Unlock()
	if h.switchCtl != nil {
		if mode == amxv1.SwitchMode_SWITCH_MODE_AUTO {
			h.switchCtl.Start()
		} else {
			h.switchCtl.Stop()
		}
	}
	ack.SwitchMode = mode
	converged(ack)
	h.record(ack, "set_mode", "", mode.String())
	return ack
}

// handleSetPolicy applies the central switching policy (O4-C + F4 O4-B):
// threshold, cooldown_seconds and hysteresis_pct are injected into the tsamx
// engine (config set autoswitch.*), and the default strategy is kept in memory
// for auto/switch_now.
//
// The central policy governs the auto-switch provider (claude) ONLY — it is the
// sole provider that rotates, and these settings are its autoswitch.* config.
// Non-rotating providers have no auto-switch, so SetPolicy carries no provider
// dimension and is applied unconditionally to the claude engine. Runs under the
// engine
// lock — a threshold change alters the criterion an in-flight tick evaluates, so
// it must be serialized. Memory-only and re-asserted each session, so it is NOT
// gated by the applied log (re-application is idempotent).
func (h *Handler) handleSetPolicy(ctx context.Context, cmd *amxv1.AmsCommand, sp *amxv1.SetPolicy, ack *amxv1.CommandAck) *amxv1.CommandAck {
	h.engine.Lock()
	defer h.engine.Unlock()

	// Replay monotonicity (R3): SetPolicy is not applied-log gated, so a captured
	// SetPolicy resent within the freshness window would otherwise re-run the
	// effect and rewind the live threshold to a stale value. issued_at is already
	// verified non-nil by checkFreshness. Ignore a SetPolicy strictly older than
	// the last one applied — its value is a past one the operator has since moved
	// past — while still applying an equal-or-newer re-assertion of the latest
	// policy. Only past-value rewind is blocked; re-assertion idempotency holds.
	issued := cmd.GetIssuedAt().AsTime()
	h.mu.Lock()
	stale := !h.lastPolicyIssuedAt.IsZero() && issued.Before(h.lastPolicyIssuedAt)
	h.mu.Unlock()
	if stale {
		// The live policy already holds the newer value, so the operator's intent
		// still stands: report CONVERGED without re-running the effect.
		converged(ack)
		h.record(ack, "set_policy", "", sp.GetDefaultStrategy().String())
		return ack
	}

	if pct := sp.GetThresholdPct(); pct > 0 {
		if err := h.bridge.ConfigSetThreshold(ctx, pct); err != nil {
			out := diverged(ack, "tsamx_config", err)
			h.record(out, "set_policy", "", "")
			return out
		}
	}
	// F4 (O4-B) full central policy. cooldown_seconds/hysteresis_pct use a
	// negative "unset" sentinel — 0 is a real value (proto SetPolicy 3/4), unlike
	// threshold above where 0 means "keep the local default". So inject on >= 0.
	// Keys are tsamx's camelCase config names (settings.py json_key), NOT the
	// proto's snake_case — `tsamx config set autoswitch.cooldown_seconds` errors.
	if cd := sp.GetCooldownSeconds(); cd >= 0 {
		if err := h.bridge.ConfigSet(ctx, "cooldownSeconds", cd); err != nil {
			out := diverged(ack, "tsamx_config", err)
			h.record(out, "set_policy", "", "")
			return out
		}
	}
	if hy := sp.GetHysteresisPct(); hy >= 0 {
		if err := h.bridge.ConfigSet(ctx, "hysteresisPct", hy); err != nil {
			out := diverged(ack, "tsamx_config", err)
			h.record(out, "set_policy", "", "")
			return out
		}
	}
	if ds := sp.GetDefaultStrategy(); ds != amxv1.SwitchNow_SWITCH_STRATEGY_UNSPECIFIED {
		h.mu.Lock()
		h.defaultStrategy = ds
		h.mu.Unlock()
	}
	h.mu.Lock()
	if issued.After(h.lastPolicyIssuedAt) {
		h.lastPolicyIssuedAt = issued
	}
	h.mu.Unlock()
	converged(ack)
	h.record(ack, "set_policy", "", sp.GetDefaultStrategy().String())
	return ack
}

// handleSwitchNow performs a manual switch (design note §3). Either an explicit
// account (`tsamx switch <email>`) or a strategy (`tsamx switch --strategy
// best|next-available`); an unspecified strategy falls back to the SetPolicy
// default. Runs under the engine lock. On success it emits a manual switch
// AccountEvent through the outbox and records last_switched_at.
func (h *Handler) handleSwitchNow(ctx context.Context, cmd *amxv1.AmsCommand, sn *amxv1.SwitchNow, ack *amxv1.CommandAck) *amxv1.CommandAck {
	h.engine.Lock()
	defer h.engine.Unlock()

	ref := sn.GetAccount()
	// Provider routing (P3): an explicit target's provider selects the bridge; a
	// strategy-only switch (no account) resolves to the default provider. An
	// unregistered provider gets no effect.
	br := h.bridgeFor(ref.GetProvider())
	if br == nil {
		return diverged(ack, "provider_unsupported", fmt.Errorf("provider %q is not registered", ref.GetProvider()))
	}
	// The switch stays within one provider's pool, so from/to events carry it.
	pk := provider.Normalize(ref.GetProvider())

	before, _ := br.Status(ctx)
	var fromEmail string
	if before != nil {
		fromEmail = before.ActiveEmail
	}

	if ref != nil && ref.GetEmail() != "" {
		if err := br.Switch(ctx, ref.GetEmail()); err != nil {
			return diverged(ack, "tsamx_switch", err)
		}
		h.setAccountState(ctx, br, ack, ref, amxv1.AllocationStatus_ALLOCATION_STATUS_ACTIVE)
		h.emitManualSwitch(pk, fromEmail, ref.GetEmail())
		converged(ack)
		h.record(ack, "switch_now", ref.GetAmsAccountId(), ref.GetEmail())
		return ack
	}

	// Strategy path: use the named strategy, else the SetPolicy default.
	strategy := sn.GetStrategy()
	if strategy == amxv1.SwitchNow_SWITCH_STRATEGY_UNSPECIFIED {
		h.mu.Lock()
		strategy = h.defaultStrategy
		h.mu.Unlock()
	}
	name := strategyName(strategy)
	if name == "" {
		ack.Detail = "switch_now: no target account and no strategy (explicit or default)"
		return diverged(ack, "unsupported_target", nil)
	}
	if err := br.SwitchStrategy(ctx, name); err != nil {
		return diverged(ack, "tsamx_switch", err)
	}
	after, _ := br.Status(ctx)
	toEmail := ""
	if after != nil {
		toEmail = after.ActiveEmail
	}
	if toEmail != "" {
		h.setAccountState(ctx, br, ack, &amxv1.AccountRef{Email: toEmail, Provider: pk}, amxv1.AllocationStatus_ALLOCATION_STATUS_ACTIVE)
	}
	h.emitManualSwitch(pk, fromEmail, toEmail)
	converged(ack)
	h.record(ack, "switch_now", "", name)
	return ack
}

// emitManualSwitch queues a manual (trigger=manual) switch AccountEvent and moves
// last_switched_at. Called while the engine lock is held.
func (h *Handler) emitManualSwitch(providerKey, fromEmail, toEmail string) {
	h.mu.Lock()
	h.lastSwitchedAt = h.now().UTC()
	h.mu.Unlock()
	if h.outbox == nil {
		return
	}
	ev := &amxv1.AccountEvent{
		SchemaVersion: 1,
		AgentId:       h.agentID,
		EventId:       reporter.NewEventID(),
		OccurredAt:    timestamppb.New(h.now().UTC()),
		Kind:          amxv1.AccountEvent_KIND_SWITCH,
		Trigger:       amxv1.AccountEvent_TRIGGER_MANUAL,
	}
	if fromEmail != "" {
		ev.From = &amxv1.AccountRef{Email: fromEmail, Provider: providerKey}
	}
	if toEmail != "" {
		ev.To = &amxv1.AccountRef{Email: toEmail, Provider: providerKey}
	}
	// The event is queued in memory regardless; a disk-append error only forfeits
	// restart durability for this one event (rare, e.g. disk full), so it is
	// non-fatal to the switch that already happened.
	_ = h.outbox.Enqueue(ev)
}

// strategyName maps the proto strategy enum to the tsamx CLI flag value.
func strategyName(s amxv1.SwitchNow_SwitchStrategy) string {
	switch s {
	case amxv1.SwitchNow_SWITCH_STRATEGY_BEST:
		return "best"
	case amxv1.SwitchNow_SWITCH_STRATEGY_NEXT_AVAILABLE:
		return "next-available"
	default:
		return ""
	}
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
