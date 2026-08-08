package crypto

import (
	"bytes"
	"crypto/ed25519"
	"testing"

	amxv1 "github.com/2kwanghee/AMX/contracts/gen/go"
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

func hexEncode(b []byte) string {
	const hexdigits = "0123456789abcdef"
	out := make([]byte, len(b)*2)
	for i, c := range b {
		out[i*2] = hexdigits[c>>4]
		out[i*2+1] = hexdigits[c&0xf]
	}
	return string(out)
}
