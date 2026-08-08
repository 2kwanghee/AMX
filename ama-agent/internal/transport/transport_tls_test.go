package transport

import (
	"context"
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/tls"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/pem"
	"math/big"
	"net"
	"os"
	"path/filepath"
	"testing"
	"time"

	amxv1 "github.com/2kwanghee/AMX/contracts/gen/go"
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials"
)

// This file is the B4 TLS end-to-end test: it drives the real
// SecurityDialOption credential selection against a live gRPC server over an
// actual TLS handshake (localhost TCP, self-signed certs generated in-test and
// never committed). It covers one-way TLS, mutual TLS, and the negative cases a
// misconfigured or hostile peer would hit (§7 in-transit).

// stubControlPlane is a minimal AmxControlPlane: it reads one Register from the
// stream and replies with a SessionSetup, which is exactly the first roundtrip a
// real agent performs (SSOT §5.4).
type stubControlPlane struct {
	amxv1.UnimplementedAmxControlPlaneServer
	gotRegister chan *amxv1.Register
}

func (s *stubControlPlane) Session(stream amxv1.AmxControlPlane_SessionServer) error {
	msg, err := stream.Recv()
	if err != nil {
		return err
	}
	if reg := msg.GetRegister(); reg != nil {
		select {
		case s.gotRegister <- reg:
		default:
		}
	}
	return stream.Send(&amxv1.AmsCommand{
		Cmd: &amxv1.AmsCommand_SessionSetup{
			SessionSetup: &amxv1.SessionSetup{ServerCredential: "srv-cred-roundtrip"},
		},
	})
}

// certPaths holds the on-disk PEM files a test needs.
type certPaths struct {
	caFile      string
	otherCAFile string // an unrelated CA, for the "wrong CA" negative
	serverCert  string
	serverKey   string
	clientCert  string
	clientKey   string
	expiredCert string // server cert already expired, valid CA
	expiredKey  string
}

func writePEM(t *testing.T, dir, name, blockType string, der []byte) string {
	t.Helper()
	path := filepath.Join(dir, name)
	f, err := os.Create(path)
	if err != nil {
		t.Fatalf("create %s: %v", path, err)
	}
	defer f.Close()
	if err := pem.Encode(f, &pem.Block{Type: blockType, Bytes: der}); err != nil {
		t.Fatalf("encode %s: %v", path, err)
	}
	return path
}

func keyPEM(t *testing.T, dir, name string, key *ecdsa.PrivateKey) string {
	t.Helper()
	der, err := x509.MarshalECPrivateKey(key)
	if err != nil {
		t.Fatalf("marshal key: %v", err)
	}
	return writePEM(t, dir, name, "EC PRIVATE KEY", der)
}

// makeCA returns a self-signed CA cert template + key.
func makeCA(t *testing.T, cn string) (*x509.Certificate, *ecdsa.PrivateKey) {
	t.Helper()
	key, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		t.Fatalf("ca key: %v", err)
	}
	tmpl := &x509.Certificate{
		SerialNumber:          big.NewInt(time.Now().UnixNano()),
		Subject:               pkix.Name{CommonName: cn},
		NotBefore:             time.Now().Add(-time.Hour),
		NotAfter:              time.Now().Add(24 * time.Hour),
		KeyUsage:              x509.KeyUsageCertSign | x509.KeyUsageDigitalSignature,
		BasicConstraintsValid: true,
		IsCA:                  true,
	}
	der, err := x509.CreateCertificate(rand.Reader, tmpl, tmpl, &key.PublicKey, key)
	if err != nil {
		t.Fatalf("ca cert: %v", err)
	}
	ca, err := x509.ParseCertificate(der)
	if err != nil {
		t.Fatalf("parse ca: %v", err)
	}
	return ca, key
}

// makeLeaf issues a leaf cert (server or client) signed by ca.
func makeLeaf(t *testing.T, dir, prefix string, ca *x509.Certificate, caKey *ecdsa.PrivateKey, server bool, notAfter time.Time) (certFile, keyFile string) {
	t.Helper()
	key, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		t.Fatalf("leaf key: %v", err)
	}
	tmpl := &x509.Certificate{
		SerialNumber: big.NewInt(time.Now().UnixNano()),
		Subject:      pkix.Name{CommonName: prefix},
		NotBefore:    time.Now().Add(-time.Hour),
		NotAfter:     notAfter,
		KeyUsage:     x509.KeyUsageDigitalSignature,
	}
	if server {
		tmpl.ExtKeyUsage = []x509.ExtKeyUsage{x509.ExtKeyUsageServerAuth}
		tmpl.IPAddresses = []net.IP{net.ParseIP("127.0.0.1"), net.IPv6loopback}
		tmpl.DNSNames = []string{"localhost"}
	} else {
		tmpl.ExtKeyUsage = []x509.ExtKeyUsage{x509.ExtKeyUsageClientAuth}
	}
	der, err := x509.CreateCertificate(rand.Reader, tmpl, ca, &key.PublicKey, caKey)
	if err != nil {
		t.Fatalf("leaf cert: %v", err)
	}
	certFile = writePEM(t, dir, prefix+".crt", "CERTIFICATE", der)
	keyFile = keyPEM(t, dir, prefix+".key", key)
	return certFile, keyFile
}

func makeCerts(t *testing.T) certPaths {
	t.Helper()
	dir := t.TempDir()
	ca, caKey := makeCA(t, "amx-test-ca")
	caFile := writePEM(t, dir, "ca.crt", "CERTIFICATE", ca.Raw)

	other, _ := makeCA(t, "amx-other-ca")
	otherCAFile := writePEM(t, dir, "other-ca.crt", "CERTIFICATE", other.Raw)

	serverCert, serverKey := makeLeaf(t, dir, "server", ca, caKey, true, time.Now().Add(24*time.Hour))
	clientCert, clientKey := makeLeaf(t, dir, "client", ca, caKey, false, time.Now().Add(24*time.Hour))
	expiredCert, expiredKey := makeLeaf(t, dir, "server-expired", ca, caKey, true, time.Now().Add(-time.Minute))

	return certPaths{
		caFile:      caFile,
		otherCAFile: otherCAFile,
		serverCert:  serverCert,
		serverKey:   serverKey,
		clientCert:  clientCert,
		clientKey:   clientKey,
		expiredCert: expiredCert,
		expiredKey:  expiredKey,
	}
}

// startTLSServer boots a gRPC server with the given server TLS config on a
// loopback port and returns its address plus the stub for assertions.
func startTLSServer(t *testing.T, cfg *tls.Config) (string, *stubControlPlane) {
	t.Helper()
	lis, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("listen: %v", err)
	}
	srv := grpc.NewServer(grpc.Creds(credentials.NewTLS(cfg)))
	stub := &stubControlPlane{gotRegister: make(chan *amxv1.Register, 1)}
	amxv1.RegisterAmxControlPlaneServer(srv, stub)
	go func() { _ = srv.Serve(lis) }()
	t.Cleanup(srv.Stop)
	return lis.Addr().String(), stub
}

func serverKeyPair(t *testing.T, certFile, keyFile string) tls.Certificate {
	t.Helper()
	pair, err := tls.LoadX509KeyPair(certFile, keyFile)
	if err != nil {
		t.Fatalf("server key pair: %v", err)
	}
	return pair
}

func certPool(t *testing.T, caFile string) *x509.CertPool {
	t.Helper()
	pem, err := os.ReadFile(caFile)
	if err != nil {
		t.Fatalf("read ca: %v", err)
	}
	pool := x509.NewCertPool()
	if !pool.AppendCertsFromPEM(pem) {
		t.Fatalf("append ca")
	}
	return pool
}

// clearTLSEnv resets every transport-security variable so each subtest starts
// from a known-empty baseline (t.Setenv restores prior values on cleanup).
func clearTLSEnv(t *testing.T) {
	t.Helper()
	for _, k := range []string{EnvTLSCA, EnvTLSServerName, EnvTLSClientCert, EnvTLSClientKey, EnvAllowInsecure} {
		t.Setenv(k, "")
	}
}

// dialAndRoundtrip drives the real Client (SecurityDialOption creds) to send a
// Register and awaits the server's SessionSetup, returning it or an error.
func dialAndRoundtrip(t *testing.T, addr string) (*amxv1.AmsCommand, error) {
	t.Helper()
	opt, err := SecurityDialOption()
	if err != nil {
		return nil, err
	}
	c := Dial(addr, opt)
	defer c.Close()
	c.OnConnect = func(send func(*amxv1.AmaMessage) error) error {
		return send(&amxv1.AmaMessage{Msg: &amxv1.AmaMessage_Register{
			Register: &amxv1.Register{AgentId: "ama-tls-test"},
		}})
	}
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go func() { _ = c.Run(ctx) }()
	select {
	case cmd := <-c.Recv():
		return cmd, nil
	case <-time.After(5 * time.Second):
		return nil, context.DeadlineExceeded
	}
}

// rawDialExpectFail builds the credentials via SecurityDialOption but skips the
// reconnecting Client (which would retry forever): it makes one Session attempt
// and asserts the TLS handshake fails. Used for the negative cases.
func rawDialExpectFail(t *testing.T, addr string) error {
	t.Helper()
	opt, err := SecurityDialOption()
	if err != nil {
		return err
	}
	conn, err := grpc.NewClient(addr, opt)
	if err != nil {
		return err
	}
	defer conn.Close()
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	client := amxv1.NewAmxControlPlaneClient(conn)
	stream, err := client.Session(ctx)
	if err != nil {
		return err
	}
	if err := stream.Send(&amxv1.AmaMessage{Msg: &amxv1.AmaMessage_Register{
		Register: &amxv1.Register{AgentId: "ama-tls-neg"},
	}}); err != nil {
		return err
	}
	_, err = stream.Recv()
	return err
}

// TestTLSOneWayRoundtrip: server presents a CA-signed cert, the agent verifies
// it with the CA and presents no client cert. Register -> SessionSetup succeeds.
func TestTLSOneWayRoundtrip(t *testing.T) {
	certs := makeCerts(t)
	addr, stub := startTLSServer(t, &tls.Config{
		Certificates: []tls.Certificate{serverKeyPair(t, certs.serverCert, certs.serverKey)},
		MinVersion:   tls.VersionTLS12,
	})

	clearTLSEnv(t)
	t.Setenv(EnvTLSCA, certs.caFile)

	cmd, err := dialAndRoundtrip(t, addr)
	if err != nil {
		t.Fatalf("one-way TLS roundtrip failed: %v", err)
	}
	if cmd.GetSessionSetup().GetServerCredential() != "srv-cred-roundtrip" {
		t.Fatalf("unexpected downstream command: %v", cmd)
	}
	select {
	case reg := <-stub.gotRegister:
		if reg.GetAgentId() != "ama-tls-test" {
			t.Fatalf("server saw wrong Register: %v", reg)
		}
	default:
		t.Fatal("server never received Register")
	}
}

// TestTLSMutualRoundtrip: the server requires and verifies a client cert; the
// agent presents its CA-signed client cert. Full mTLS roundtrip succeeds.
func TestTLSMutualRoundtrip(t *testing.T) {
	certs := makeCerts(t)
	addr, stub := startTLSServer(t, &tls.Config{
		Certificates: []tls.Certificate{serverKeyPair(t, certs.serverCert, certs.serverKey)},
		ClientAuth:   tls.RequireAndVerifyClientCert,
		ClientCAs:    certPool(t, certs.caFile),
		MinVersion:   tls.VersionTLS12,
	})

	clearTLSEnv(t)
	t.Setenv(EnvTLSCA, certs.caFile)
	t.Setenv(EnvTLSClientCert, certs.clientCert)
	t.Setenv(EnvTLSClientKey, certs.clientKey)

	cmd, err := dialAndRoundtrip(t, addr)
	if err != nil {
		t.Fatalf("mTLS roundtrip failed: %v", err)
	}
	if cmd.GetSessionSetup().GetServerCredential() != "srv-cred-roundtrip" {
		t.Fatalf("unexpected downstream command: %v", cmd)
	}
	select {
	case <-stub.gotRegister:
	default:
		t.Fatal("server never received Register over mTLS")
	}
}

// TestTLSMutualServerRejectsAnonymousClient: an mTLS server refuses an agent
// that only does one-way TLS (CA set, no client cert).
func TestTLSMutualServerRejectsAnonymousClient(t *testing.T) {
	certs := makeCerts(t)
	addr, _ := startTLSServer(t, &tls.Config{
		Certificates: []tls.Certificate{serverKeyPair(t, certs.serverCert, certs.serverKey)},
		ClientAuth:   tls.RequireAndVerifyClientCert,
		ClientCAs:    certPool(t, certs.caFile),
		MinVersion:   tls.VersionTLS12,
	})

	clearTLSEnv(t)
	t.Setenv(EnvTLSCA, certs.caFile) // one-way only: no client cert

	if err := rawDialExpectFail(t, addr); err == nil {
		t.Fatal("mTLS server accepted a client that presented no certificate")
	}
}

// TestTLSWrongCARejected: the agent trusts a different CA than the one that
// signed the server cert, so the handshake fails.
func TestTLSWrongCARejected(t *testing.T) {
	certs := makeCerts(t)
	addr, _ := startTLSServer(t, &tls.Config{
		Certificates: []tls.Certificate{serverKeyPair(t, certs.serverCert, certs.serverKey)},
		MinVersion:   tls.VersionTLS12,
	})

	clearTLSEnv(t)
	t.Setenv(EnvTLSCA, certs.otherCAFile)

	if err := rawDialExpectFail(t, addr); err == nil {
		t.Fatal("agent accepted a server cert signed by an untrusted CA")
	}
}

// TestTLSExpiredServerCertRejected: a server whose cert has expired is rejected
// even though it was signed by the trusted CA.
func TestTLSExpiredServerCertRejected(t *testing.T) {
	certs := makeCerts(t)
	addr, _ := startTLSServer(t, &tls.Config{
		Certificates: []tls.Certificate{serverKeyPair(t, certs.expiredCert, certs.expiredKey)},
		MinVersion:   tls.VersionTLS12,
	})

	clearTLSEnv(t)
	t.Setenv(EnvTLSCA, certs.caFile)

	if err := rawDialExpectFail(t, addr); err == nil {
		t.Fatal("agent accepted an expired server certificate")
	}
}

// TestTLSInsecureClientRejected: an agent that opted into plaintext
// (AMX_GRPC_ALLOW_INSECURE=1, no CA) cannot talk to a TLS server.
func TestTLSInsecureClientRejected(t *testing.T) {
	certs := makeCerts(t)
	addr, _ := startTLSServer(t, &tls.Config{
		Certificates: []tls.Certificate{serverKeyPair(t, certs.serverCert, certs.serverKey)},
		MinVersion:   tls.VersionTLS12,
	})

	clearTLSEnv(t)
	t.Setenv(EnvAllowInsecure, "1")

	if err := rawDialExpectFail(t, addr); err == nil {
		t.Fatal("plaintext client completed a request against a TLS server")
	}
}

// TestTLSOneWayRejectsTLS11Server: the one-way path pins MinVersion TLS 1.2, so
// a server that only offers TLS 1.1 is rejected regardless of the gRPC default.
func TestTLSOneWayRejectsTLS11Server(t *testing.T) {
	certs := makeCerts(t)
	addr, _ := startTLSServer(t, &tls.Config{
		Certificates: []tls.Certificate{serverKeyPair(t, certs.serverCert, certs.serverKey)},
		MaxVersion:   tls.VersionTLS11,
	})

	clearTLSEnv(t)
	t.Setenv(EnvTLSCA, certs.caFile) // one-way, no client cert

	if err := rawDialExpectFail(t, addr); err == nil {
		t.Fatal("one-way client negotiated TLS 1.1 despite MinVersion 1.2")
	}
}

// TestSecurityDialOptionHalfClientCertFailsClosed: setting only one half of the
// client-cert pair is a misconfiguration that must fail closed rather than
// silently degrade to anonymous one-way TLS.
func TestSecurityDialOptionHalfClientCertFailsClosed(t *testing.T) {
	certs := makeCerts(t)
	clearTLSEnv(t)
	t.Setenv(EnvTLSCA, certs.caFile)
	t.Setenv(EnvTLSClientCert, certs.clientCert) // key deliberately absent

	if _, err := SecurityDialOption(); err == nil {
		t.Fatal("expected an error when only the client cert (not key) is set")
	}
}
