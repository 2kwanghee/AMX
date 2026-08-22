// Package provider defines the vendor-neutral account driver boundary. A Driver
// is the single owner of one vendor's credential- and config-home knowledge
// (file layout, identity keys, fingerprint scheme, config-home env, pool binary);
// the tsamx bridge, store, and resync depend only on this interface, so a new
// vendor is added by writing a Driver, not by editing those packages.
package provider

// AddMeta is the vendor-neutral identity staged alongside a credential set. It
// carries only what any driver may need to name the account it is about to
// capture; how (and whether) each field is used is the driver's concern.
type AddMeta struct {
	Email            string
	AccountUUID      string
	OrganizationName string
}

// Driver owns all vendor-specific credential/config-home knowledge behind a
// neutral surface. Implementations MUST NOT log credential material.
type Driver interface {
	// Name identifies the vendor (e.g. "claude").
	Name() string
	// ConfigHome resolves the vendor's config home from the environment (empty
	// when unset). It is the directory credentials are staged into and watched in.
	ConfigHome() string
	// CredentialPath returns the live credential file inside configDir — the file
	// resync watches for a local rotation.
	CredentialPath(configDir string) string
	// StageCredential writes the credential set (and any identity/config files the
	// vendor's capture verb reads) into configDir, atomically. credentialJSON is
	// plaintext and MUST NEVER be logged.
	StageCredential(configDir string, credentialJSON []byte, meta AddMeta) error
	// Fingerprint returns the stable identity hash of a credential set (a one-way
	// hash, never the credential itself; empty only for empty input).
	Fingerprint(credentialJSON []byte) string
	// HasCredentialMaterial reports whether a credential set carries any usable
	// token material. It is deliberately conservative: false ONLY when the set is
	// definitely token-less (a logged-out shell), true whenever the shape cannot be
	// judged (an opaque api_key, an unknown schema). The caller uses it to DROP an
	// upstream re-sync, so a false negative would strand AMS on a stale copy while
	// a false positive costs only a redundant push. MUST NOT log credential
	// material.
	HasCredentialMaterial(credentialJSON []byte) bool
	// DefaultConfigHome is the vendor's conventional config home when no explicit
	// directory is configured (e.g. ~/.claude). Used ONLY to resolve the deliver
	// lock path so the daemon and the vendor's runner wrapper flock the same file;
	// it must NOT be used to enable staging/resync when ConfigHome is unset.
	DefaultConfigHome() string
	// Env returns the process environment entries that point the pool binary at
	// configDir (the vendor's config-home variable set to configDir).
	Env(configDir string) []string
	// BinaryName is the pool CLI executable for this vendor.
	BinaryName() string
	// Identity reads back the account identity (currently just the email) that
	// StageCredential recorded into configDir, without re-deriving it from an
	// AccountKey (that hash is one-way — see profile.AccountKey). It exists so a
	// caller holding only a configDir (e.g. profile.Store.GetActive's result)
	// can answer "whose profile is this" — closing the P2 "email -> accountKey
	// reverse index" gap (design note P3) — and so the reverse mapping uses the
	// SAME, un-normalized email string the manifest store keys on
	// (store.Store.FindByProviderEmail is case-sensitive exact match; AccountKey
	// lowercases before hashing), rather than a second, independently-derived
	// value that could drift from it. Returns ("", err) when configDir holds no
	// recorded identity yet (e.g. Create()d but never Stage()d). MUST NOT log
	// credential material (it never needs to touch any).
	Identity(configDir string) (email string, err error)
}
