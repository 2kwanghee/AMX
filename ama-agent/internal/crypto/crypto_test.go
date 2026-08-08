package crypto

import (
	"bytes"
	"crypto/ed25519"
	"encoding/hex"
	"testing"

	amxv1 "github.com/2kwanghee/AMX/contracts/gen/go"
	"golang.org/x/crypto/nacl/box"
)

func sampleCommand() *amxv1.AmsCommand {
	return &amxv1.AmsCommand{
		CommandId: "cmd-1",
		Cmd: &amxv1.AmsCommand_Deliver{Deliver: &amxv1.DeliverAccount{
			AssignmentId: "asg-1",
			Account:      &amxv1.AccountRef{AmsAccountId: "acc-1", Email: "a@x.io"},
		}},
	}
}

func TestSignVerifyRoundtrip(t *testing.T) {
	pub, priv, err := ed25519.GenerateKey(nil)
	if err != nil {
		t.Fatal(err)
	}
	cmd := sampleCommand()
	sig, err := SignCommand(priv, cmd)
	if err != nil {
		t.Fatal(err)
	}
	cmd.Signature = sig
	if err := VerifyCommand(pub, cmd); err != nil {
		t.Fatalf("valid signature rejected: %v", err)
	}
}

func TestVerifyRejectsTamperedPayload(t *testing.T) {
	pub, priv, _ := ed25519.GenerateKey(nil)
	cmd := sampleCommand()
	sig, _ := SignCommand(priv, cmd)
	cmd.Signature = sig
	// Mutate a signed field after signing.
	cmd.GetDeliver().GetAccount().Email = "attacker@x.io"
	if err := VerifyCommand(pub, cmd); err == nil {
		t.Fatal("tampered command verified")
	}
}

func TestVerifyRejectsForeignKey(t *testing.T) {
	_, priv, _ := ed25519.GenerateKey(nil)
	otherPub, _, _ := ed25519.GenerateKey(nil)
	cmd := sampleCommand()
	sig, _ := SignCommand(priv, cmd)
	cmd.Signature = sig
	if err := VerifyCommand(otherPub, cmd); err == nil {
		t.Fatal("signature from foreign key verified")
	}
}

func TestAESGCMRoundtrip(t *testing.T) {
	key := bytes.Repeat([]byte{0x2a}, KEKSize)
	nonce, err := NewNonce()
	if err != nil {
		t.Fatal(err)
	}
	aad := []byte("acc-1\x1fama_dev")
	pt := []byte(`{"accessToken":"secret"}`)
	ct, err := Seal(key, nonce, pt, aad)
	if err != nil {
		t.Fatal(err)
	}
	got, err := Open(key, nonce, ct, aad)
	if err != nil {
		t.Fatalf("open: %v", err)
	}
	if !bytes.Equal(got, pt) {
		t.Fatalf("roundtrip mismatch: %q != %q", got, pt)
	}
}

func TestAESGCMWrongAADFails(t *testing.T) {
	key := bytes.Repeat([]byte{0x2a}, KEKSize)
	nonce, _ := NewNonce()
	ct, _ := Seal(key, nonce, []byte("secret"), []byte("acc-1\x1fagentA"))
	if _, err := Open(key, nonce, ct, []byte("acc-1\x1fagentB")); err == nil {
		t.Fatal("open succeeded with wrong AAD")
	}
}

func TestParsePublicKeyFormats(t *testing.T) {
	pub, _, _ := ed25519.GenerateKey(nil)
	// hex
	if _, err := ParsePublicKey(hexEncode(pub)); err != nil {
		t.Fatalf("hex parse: %v", err)
	}
	if _, err := ParsePublicKey("not-a-key"); err == nil {
		t.Fatal("garbage parsed as key")
	}
}

func TestGenerateSessionKeyPairUnique(t *testing.T) {
	pub1, priv1, err := GenerateSessionKeyPair()
	if err != nil {
		t.Fatal(err)
	}
	pub2, priv2, err := GenerateSessionKeyPair()
	if err != nil {
		t.Fatal(err)
	}
	if len(pub1) != X25519KeySize {
		t.Fatalf("public key len = %d, want %d", len(pub1), X25519KeySize)
	}
	if bytes.Equal(pub1[:], pub2[:]) {
		t.Fatal("two generated public keys are identical")
	}
	if bytes.Equal(priv1[:], priv2[:]) {
		t.Fatal("two generated private keys are identical")
	}
}

// TestUnwrapKEKSealedRoundtrip: a KEK sealed to the agent's public key (as AMS's
// nacl.SealedBox(pub).encrypt(kek) would) opens back to the exact KEK.
func TestUnwrapKEKSealedRoundtrip(t *testing.T) {
	pub, priv, err := GenerateSessionKeyPair()
	if err != nil {
		t.Fatal(err)
	}
	kek := bytes.Repeat([]byte{0x33}, KEKSize)
	sealed, err := box.SealAnonymous(nil, kek, pub, nil)
	if err != nil {
		t.Fatal(err)
	}
	got, err := UnwrapKEK(sealed, pub, priv)
	if err != nil {
		t.Fatalf("unwrap: %v", err)
	}
	if !bytes.Equal(got, kek) {
		t.Fatalf("unwrapped KEK mismatch")
	}
}

// TestUnwrapKEKRejectsRawKEK: a raw (unsealed) 32-byte KEK must be rejected —
// this is the C2 downgrade defense (AMA advertises a public key, so it accepts
// only sealed boxes).
func TestUnwrapKEKRejectsRawKEK(t *testing.T) {
	pub, priv, _ := GenerateSessionKeyPair()
	raw := bytes.Repeat([]byte{0x33}, KEKSize)
	if _, err := UnwrapKEK(raw, pub, priv); err == nil {
		t.Fatal("raw KEK accepted (downgrade not prevented)")
	}
}

// TestUnwrapKEKRejectsWrongKey: a KEK sealed to a different public key cannot be
// opened with this session's private key.
func TestUnwrapKEKRejectsWrongKey(t *testing.T) {
	pub, priv, _ := GenerateSessionKeyPair()
	otherPub, _, _ := GenerateSessionKeyPair()
	sealed, err := box.SealAnonymous(nil, bytes.Repeat([]byte{0x33}, KEKSize), otherPub, nil)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := UnwrapKEK(sealed, pub, priv); err == nil {
		t.Fatal("KEK sealed to a foreign key was opened")
	}
}

// TestUnwrapKEKRejectsWrongLength: a validly sealed payload of the wrong length
// is rejected (a KEK must be exactly AES-256).
func TestUnwrapKEKRejectsWrongLength(t *testing.T) {
	pub, priv, _ := GenerateSessionKeyPair()
	sealed, err := box.SealAnonymous(nil, bytes.Repeat([]byte{0x33}, 16), pub, nil)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := UnwrapKEK(sealed, pub, priv); err == nil {
		t.Fatal("undersized KEK accepted")
	}
}

func TestUnwrapKEKNilKeyPair(t *testing.T) {
	if _, err := UnwrapKEK([]byte("x"), nil, nil); err == nil {
		t.Fatal("nil key pair accepted")
	}
}

// -- Cross-language sealed-box fixed vector (direction A: Python seal -> Go open).
//
// These constants are a TEST-ONLY fixed X25519 key pair and KEK — NOT real
// secrets. The sealed envelope below was produced ONCE by Python
// nacl.public.SealedBox(PublicKey(pub)).encrypt(kek) over these exact fixed
// (priv, pub, kek) values and hard-coded here (a sealed box embeds an ephemeral
// sender key, so its output is non-deterministic — only the open direction can
// be pinned as a fixed vector). This guards Python<->Go sealed-box compatibility
// as a unit test, independent of the e2e binaries. If nacl SealedBox and Go
// box.OpenAnonymous ever drift on the wire, this fails.
const (
	fixedVecPrivHex = "0102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f20"
	fixedVecPubHex  = "07a37cbc142093c8b755dc1b10e86cb426374ad16aa853ed0bdfc0b2b86d1c7c"
	fixedVecKEKHex  = "ababababababababababababababababcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcd"
	// Produced by Python SealedBox(pub).encrypt(kek) over the fixed values above.
	fixedVecPythonSealedHex = "749727b506e639aecac4abd633c8278adc3ad146cb4e0e8f18b3d2b85b40de01" +
		"d0b031190f166ac83d8ee419880bb783b8f344a55c1113e7cc28c4c1f2c717d4" +
		"9114732cf68dfe277fcb6bde3db5d1d7"
)

func mustHex(t *testing.T, s string) []byte {
	t.Helper()
	b, err := hex.DecodeString(s)
	if err != nil {
		t.Fatalf("bad fixture hex: %v", err)
	}
	return b
}

func fixedVecKeyPair(t *testing.T) (pub, priv *[X25519KeySize]byte) {
	t.Helper()
	pub, priv = new([X25519KeySize]byte), new([X25519KeySize]byte)
	copy(pub[:], mustHex(t, fixedVecPubHex))
	copy(priv[:], mustHex(t, fixedVecPrivHex))
	return pub, priv
}

// TestCrossLangSealedVectorPythonToGo: a Python-produced sealed box opens with
// Go and recovers the exact fixed KEK.
func TestCrossLangSealedVectorPythonToGo(t *testing.T) {
	pub, priv := fixedVecKeyPair(t)
	sealed := mustHex(t, fixedVecPythonSealedHex)
	kek, err := UnwrapKEK(sealed, pub, priv)
	if err != nil {
		t.Fatalf("Python-sealed KEK failed to open in Go: %v", err)
	}
	if !bytes.Equal(kek, mustHex(t, fixedVecKEKHex)) {
		t.Fatalf("cross-lang KEK mismatch: got %x", kek)
	}
}

// TestCrossLangSealedVectorTamperRejected: flipping one bit of the Python-sealed
// envelope makes it fail authentication (Poly1305), so the KEK is never returned.
func TestCrossLangSealedVectorTamperRejected(t *testing.T) {
	pub, priv := fixedVecKeyPair(t)
	sealed := mustHex(t, fixedVecPythonSealedHex)
	sealed[len(sealed)-1] ^= 0x01 // flip one bit of the tag
	if _, err := UnwrapKEK(sealed, pub, priv); err == nil {
		t.Fatal("tampered sealed box opened (Poly1305 not enforced)")
	}
}

func hexEncode(b []byte) string {
	const hexdigits = "0123456789abcdef"
	out := make([]byte, len(b)*2)
	for i, c := range b {
		out[i*2] = hexdigits[c>>4]
		out[i*2+1] = hexdigits[c&0xf]
	}
	return string(out)
}
