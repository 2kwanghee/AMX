// Package crypto provides the AMA agent's cryptographic primitives (design
// note §4, SSOT §6.2/§7):
//
//   - Ed25519 verification of AMS commands (authority is enforced by signature,
//     not by encryption).
//   - AES-256-GCM seal/open for the manifest credential records.
//   - KEK unwrap for keys delivered in SessionSetup.
//
// Invariants:
//   - The AMS public key is NEVER hardcoded — it is loaded from the environment
//     or a file (§7).
//   - Key material, nonces, plaintext, and ciphertext are NEVER logged (§7).
package crypto

import (
	"crypto/aes"
	"crypto/cipher"
	"crypto/ed25519"
	"crypto/rand"
	"encoding/base64"
	"encoding/hex"
	"errors"
	"fmt"
	"os"
	"strings"

	amxv1 "github.com/2kwanghee/AMX/contracts/gen/go"
	"golang.org/x/crypto/nacl/box"
	"google.golang.org/protobuf/proto"
)

const (
	// KEKSize is the AES-256 key length in bytes.
	KEKSize = 32
	// NonceSize is the 96-bit GCM nonce length in bytes.
	NonceSize = 12
	// X25519KeySize is the raw X25519 public/private key length in bytes (C2 §7).
	// The public key advertised in Register.agent_public_key is these 32 raw
	// bytes — NOT a PEM/DER wrapper.
	X25519KeySize = 32
)

// Environment sources for the AMS Ed25519 verification key. Hardcoding the key
// is forbidden (§7): an operator must supply it out of band.
const (
	EnvAMSPubKey     = "AMX_AMS_PUBKEY"      // inline base64 or hex, 32 bytes decoded
	EnvAMSPubKeyFile = "AMX_AMS_PUBKEY_FILE" // path to a file holding the encoded key
)

// LoadAMSPublicKey reads the AMS signing public key from AMX_AMS_PUBKEY, or from
// the file named by AMX_AMS_PUBKEY_FILE. It returns an error rather than a
// default when neither is set — the agent must not run with an unknown signer.
func LoadAMSPublicKey() (ed25519.PublicKey, error) {
	raw := strings.TrimSpace(os.Getenv(EnvAMSPubKey))
	if raw == "" {
		if p := strings.TrimSpace(os.Getenv(EnvAMSPubKeyFile)); p != "" {
			b, err := os.ReadFile(p)
			if err != nil {
				return nil, fmt.Errorf("read AMS pubkey file: %w", err)
			}
			raw = strings.TrimSpace(string(b))
		}
	}
	if raw == "" {
		return nil, fmt.Errorf("AMS public key not configured: set %s or %s", EnvAMSPubKey, EnvAMSPubKeyFile)
	}
	return ParsePublicKey(raw)
}

// ParsePublicKey decodes a 32-byte Ed25519 public key from base64 (std or raw)
// or hex.
func ParsePublicKey(encoded string) (ed25519.PublicKey, error) {
	encoded = strings.TrimSpace(encoded)
	for _, dec := range []func(string) ([]byte, error){
		base64.StdEncoding.DecodeString,
		base64.RawStdEncoding.DecodeString,
		hex.DecodeString,
	} {
		if b, err := dec(encoded); err == nil && len(b) == ed25519.PublicKeySize {
			return ed25519.PublicKey(b), nil
		}
	}
	return nil, fmt.Errorf("AMS pubkey must decode (base64/hex) to %d bytes", ed25519.PublicKeySize)
}

// VerifyCommand verifies the Ed25519 signature over the canonical serialization
// of cmd with the signature field cleared (proto AmsCommand.signature, §6.2). It
// returns nil iff the signature is valid; cmd is not mutated.
func VerifyCommand(pub ed25519.PublicKey, cmd *amxv1.AmsCommand) error {
	if cmd == nil {
		return errors.New("nil command")
	}
	if len(pub) != ed25519.PublicKeySize {
		return errors.New("invalid AMS public key")
	}
	if len(cmd.Signature) != ed25519.SignatureSize {
		return errors.New("missing or malformed signature")
	}
	payload, err := SigningBytes(cmd)
	if err != nil {
		return err
	}
	if !ed25519.Verify(pub, payload, cmd.Signature) {
		return errors.New("signature verification failed")
	}
	return nil
}

// SignCommand produces the detached signature for cmd. It is the AMS-side
// operation; it lives here so signer and verifier share one definition of the
// canonical serialization (and so tests can forge/authenticate commands).
func SignCommand(priv ed25519.PrivateKey, cmd *amxv1.AmsCommand) ([]byte, error) {
	payload, err := SigningBytes(cmd)
	if err != nil {
		return nil, err
	}
	return ed25519.Sign(priv, payload), nil
}

// SigningBytes returns the deterministic serialization signed by AMS: cmd with
// its signature field cleared.
func SigningBytes(cmd *amxv1.AmsCommand) ([]byte, error) {
	clone, ok := proto.Clone(cmd).(*amxv1.AmsCommand)
	if !ok {
		return nil, errors.New("clone command")
	}
	clone.Signature = nil
	return proto.MarshalOptions{Deterministic: true}.Marshal(clone)
}

// Seal encrypts plaintext with AES-256-GCM under key, using the given 96-bit
// nonce and additional authenticated data. The returned ciphertext embeds the
// GCM tag. Never log key, nonce, plaintext, or ciphertext (§7).
func Seal(key, nonce, plaintext, aad []byte) ([]byte, error) {
	gcm, err := newGCM(key)
	if err != nil {
		return nil, err
	}
	if len(nonce) != gcm.NonceSize() {
		return nil, fmt.Errorf("nonce must be %d bytes", gcm.NonceSize())
	}
	return gcm.Seal(nil, nonce, plaintext, aad), nil
}

// Open decrypts ciphertext produced by Seal. Authentication failure — wrong key,
// nonce, or AAD (e.g. an AAD over-binding attempt on a relocated record) —
// returns an error and no plaintext.
func Open(key, nonce, ciphertext, aad []byte) ([]byte, error) {
	gcm, err := newGCM(key)
	if err != nil {
		return nil, err
	}
	if len(nonce) != gcm.NonceSize() {
		return nil, fmt.Errorf("nonce must be %d bytes", gcm.NonceSize())
	}
	pt, err := gcm.Open(nil, nonce, ciphertext, aad)
	if err != nil {
		// Deliberately opaque: do not distinguish causes or echo inputs (§7).
		return nil, errors.New("AEAD authentication failed")
	}
	return pt, nil
}

func newGCM(key []byte) (cipher.AEAD, error) {
	if len(key) != KEKSize {
		return nil, fmt.Errorf("key must be %d bytes (AES-256), got %d", KEKSize, len(key))
	}
	block, err := aes.NewCipher(key)
	if err != nil {
		return nil, err
	}
	return cipher.NewGCM(block)
}

// NewNonce returns a fresh cryptographically-random 96-bit nonce.
func NewNonce() ([]byte, error) {
	n := make([]byte, NonceSize)
	if _, err := rand.Read(n); err != nil {
		return nil, err
	}
	return n, nil
}

// WireAAD derives the additional authenticated data for a credential envelope
// received from AMS (proto EncryptedCredential §6.2). Both components come from
// values the agent already holds — the ams_account_id of the command being
// processed and its own agent_id — never from the wire aad_* fields.
//
// The NUL separator is the one AMS seals with (ams-server app/grpc/signing.py
// build_aad). It must not drift from that definition or every deliver fails
// authentication, and it is deliberately distinct from the manifest's own local
// AAD (internal/store), which never crosses a process boundary.
func WireAAD(amsAccountID, agentID string) []byte {
	return []byte(amsAccountID + "\x00" + agentID)
}

// GenerateSessionKeyPair creates a fresh ephemeral X25519 key pair for one AMA
// session (C2 §7). AMA advertises the public key in Register.agent_public_key;
// AMS seals every per-agent KEK to it with a NaCl sealed box. A new pair is
// generated on each (re)connect and the private key lives in session-scoped
// memory only — it is never persisted or logged (§7). The returned keys are the
// raw 32-byte X25519 values box uses.
func GenerateSessionKeyPair() (pub, priv *[X25519KeySize]byte, err error) {
	return box.GenerateKey(rand.Reader)
}

// UnwrapKEK recovers the raw AES-256 KEK sealed in
// SessionSetup.WrappedKey.wrapped_key by opening a NaCl sealed box (anonymous
// box: X25519 + XSalsa20-Poly1305). AMS produced it as
// SealedBox(agent_public_key).encrypt(kek); the sealed envelope already carries
// the ephemeral sender public key and nonce, so no side channel is needed.
//
// pub/priv are this session's ephemeral key pair (from GenerateSessionKeyPair).
// A raw (unsealed) KEK, a KEK sealed to a different public key, or any tampered
// envelope fails to open and is rejected — this is the C2 downgrade defense:
// because AMA always advertises a public key, it accepts ONLY sealed boxes and
// never a raw KEK. Never log the KEK or the private key (§7).
func UnwrapKEK(sealed []byte, pub, priv *[X25519KeySize]byte) ([]byte, error) {
	if pub == nil || priv == nil {
		return nil, errors.New("no session key pair for KEK unwrap")
	}
	kek, ok := box.OpenAnonymous(nil, sealed, pub, priv)
	if !ok {
		// Opaque: do not distinguish causes or echo inputs (§7).
		return nil, errors.New("sealed-box KEK unwrap failed")
	}
	if len(kek) != KEKSize {
		for i := range kek {
			kek[i] = 0
		}
		return nil, fmt.Errorf("unwrapped KEK must be %d bytes, got %d", KEKSize, len(kek))
	}
	return kek, nil
}
