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
}
