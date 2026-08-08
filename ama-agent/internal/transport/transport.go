// Package transport is the AMA-side gRPC adapter for the long-lived Session
// stream (design note §1, SSOT §5.4). AMA dials AMS outbound; the stream carries
// AmaMessages up and AmsCommands down. On disconnect it reconnects with
// exponential backoff (1s -> 30s, jittered).
package transport

import (
	"context"
	"crypto/tls"
	"crypto/x509"
	"errors"
	"fmt"
	"math/rand"
	"os"
	"sync"
	"time"

	amxv1 "github.com/2kwanghee/AMX/contracts/gen/go"
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials"
	"google.golang.org/grpc/credentials/insecure"
)

const (
	backoffMin = 1 * time.Second
	backoffMax = 30 * time.Second
)

// Transport-security environment (SSOT §5.4 / §7 in-transit).
const (
	// EnvTLSCA points at a PEM bundle of CA certs trusted for the AMS server.
	// Setting it enables TLS; without it, transport is plaintext.
	EnvTLSCA = "AMX_AMS_TLS_CA"
	// EnvTLSServerName overrides the SNI / certificate name verified against
	// EnvTLSCA (defaults to the dial host).
	EnvTLSServerName = "AMX_AMS_TLS_SERVER_NAME"
	// EnvTLSClientCert and EnvTLSClientKey point at a PEM client certificate and
	// its private key. When both are set (alongside EnvTLSCA) the agent presents
	// the certificate for mutual TLS; when absent the dial stays one-way TLS
	// (server verified, client anonymous). mTLS is defense-in-depth here — the
	// app layer already authenticates AMA with a server_credential (§AMA 인증) —
	// so one-way TLS already satisfies the in-transit requirement (§7) and the
	// client cert is an opt-in for deployments that also want transport-level
	// peer authentication.
	EnvTLSClientCert = "AMX_AMS_TLS_CLIENT_CERT"
	EnvTLSClientKey  = "AMX_AMS_TLS_CLIENT_KEY"
	// EnvAllowInsecure must be "1" to permit a plaintext dial when no TLS CA is
	// configured. Without it the agent refuses to connect rather than leak the
	// KEK to an eavesdropper (ADVERSARY).
	EnvAllowInsecure = "AMX_GRPC_ALLOW_INSECURE"
)

// SecurityDialOption chooses transport credentials from the environment: TLS
// when EnvTLSCA is set, otherwise an explicit insecure opt-in via
// EnvAllowInsecure. It errors when neither is configured, so a misconfigured
// deployment fails closed instead of dialing in plaintext by default.
func SecurityDialOption() (grpc.DialOption, error) {
	if ca := os.Getenv(EnvTLSCA); ca != "" {
		clientCert := os.Getenv(EnvTLSClientCert)
		clientKey := os.Getenv(EnvTLSClientKey)
		// A single half of the client-cert pair is always a misconfiguration:
		// fail closed rather than silently fall back to anonymous one-way TLS,
		// which would leave a deployment that intended mTLS unauthenticated.
		if (clientCert == "") != (clientKey == "") {
			return nil, fmt.Errorf(
				"transport: set both %s and %s for mutual TLS, or neither for one-way TLS",
				EnvTLSClientCert, EnvTLSClientKey)
		}
		if clientCert == "" {
			// One-way TLS: verify the server against the CA, present no cert.
			creds, err := credentials.NewClientTLSFromFile(ca, os.Getenv(EnvTLSServerName))
			if err != nil {
				return nil, err
			}
			return grpc.WithTransportCredentials(creds), nil
		}
		// Mutual TLS: verify the server against the CA and present our own cert.
		caPEM, err := os.ReadFile(ca)
		if err != nil {
			return nil, fmt.Errorf("transport: read TLS CA %q: %w", ca, err)
		}
		pool := x509.NewCertPool()
		if !pool.AppendCertsFromPEM(caPEM) {
			return nil, fmt.Errorf("transport: no certificates parsed from TLS CA %q", ca)
		}
		cert, err := tls.LoadX509KeyPair(clientCert, clientKey)
		if err != nil {
			return nil, fmt.Errorf("transport: load client key pair: %w", err)
		}
		cfg := &tls.Config{
			RootCAs:      pool,
			Certificates: []tls.Certificate{cert},
			ServerName:   os.Getenv(EnvTLSServerName),
			MinVersion:   tls.VersionTLS12,
		}
		return grpc.WithTransportCredentials(credentials.NewTLS(cfg)), nil
	}
	if os.Getenv(EnvAllowInsecure) == "1" {
		return grpc.WithTransportCredentials(insecure.NewCredentials()), nil
	}
	return nil, errors.New("transport: no TLS configured; set " + EnvTLSCA + " or opt in with " + EnvAllowInsecure + "=1")
}

// Client dials AMS and exposes Send/Recv channels over the reconnecting Session
// stream.
type Client struct {
	addr string
	opts []grpc.DialOption

	sendCh chan *amxv1.AmaMessage
	recvCh chan *amxv1.AmsCommand

	// OnConnect, if set, is called after each (re)connect with a fresh stream so
	// the caller can send the mandatory Register first (SSOT §5.4).
	OnConnect func(send func(*amxv1.AmaMessage) error) error

	closeOnce sync.Once
	closed    chan struct{}
}

// Dial constructs a Client for addr. Transport credentials are NOT injected
// here: the caller passes them (typically from SecurityDialOption), so the
// security choice is explicit and a plaintext dial cannot slip in by default
// (SSOT §5.4 / §7 in-transit).
func Dial(addr string, extra ...grpc.DialOption) *Client {
	opts := append([]grpc.DialOption(nil), extra...)
	return &Client{
		addr:   addr,
		opts:   opts,
		sendCh: make(chan *amxv1.AmaMessage, 64),
		recvCh: make(chan *amxv1.AmsCommand, 64),
		closed: make(chan struct{}),
	}
}

// Send queues a message for the current stream. It blocks only if the buffer is
// full; it returns an error after Close.
func (c *Client) Send(msg *amxv1.AmaMessage) error {
	select {
	case <-c.closed:
		return errors.New("transport: closed")
	case c.sendCh <- msg:
		return nil
	}
}

// TrySend queues a message without blocking. It returns false if the send buffer
// is full (drop the message) or the client is closed. Used for usage reports,
// which the next tick supersedes, so a drop when disconnected is harmless
// (design note §8: usage is not an event).
func (c *Client) TrySend(msg *amxv1.AmaMessage) bool {
	select {
	case <-c.closed:
		return false
	case c.sendCh <- msg:
		return true
	default:
		return false
	}
}

// Recv returns the channel of inbound commands. It stays open across reconnects
// and is closed only by Close.
func (c *Client) Recv() <-chan *amxv1.AmsCommand {
	return c.recvCh
}

// Close stops the run loop and releases the receive channel.
func (c *Client) Close() {
	c.closeOnce.Do(func() { close(c.closed) })
}

// Run drives connect -> Register (via OnConnect) -> pump, reconnecting with
// backoff until ctx is done or Close is called. It blocks; run it in a goroutine.
func (c *Client) Run(ctx context.Context) error {
	backoff := backoffMin
	for {
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-c.closed:
			return nil
		default:
		}

		err := c.session(ctx)
		if err == nil {
			backoff = backoffMin
		}

		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-c.closed:
			return nil
		case <-time.After(jitter(backoff)):
		}
		backoff *= 2
		if backoff > backoffMax {
			backoff = backoffMax
		}
	}
}

// session runs a single connection lifetime.
func (c *Client) session(ctx context.Context) error {
	conn, err := grpc.NewClient(c.addr, c.opts...)
	if err != nil {
		return err
	}
	defer conn.Close()

	client := amxv1.NewAmxControlPlaneClient(conn)
	stream, err := client.Session(ctx)
	if err != nil {
		return err
	}

	send := func(m *amxv1.AmaMessage) error { return stream.Send(m) }
	if c.OnConnect != nil {
		if err := c.OnConnect(send); err != nil {
			return err
		}
	}

	sctx, cancel := context.WithCancel(ctx)
	defer cancel()

	errCh := make(chan error, 2)
	// Downstream: commands -> recvCh.
	go func() {
		for {
			cmd, err := stream.Recv()
			if err != nil {
				errCh <- err
				return
			}
			select {
			case c.recvCh <- cmd:
			case <-sctx.Done():
				return
			case <-c.closed:
				return
			}
		}
	}()
	// Upstream: sendCh -> stream.
	go func() {
		for {
			select {
			case msg := <-c.sendCh:
				if err := stream.Send(msg); err != nil {
					errCh <- err
					return
				}
			case <-sctx.Done():
				errCh <- sctx.Err()
				return
			case <-c.closed:
				errCh <- nil
				return
			}
		}
	}()

	select {
	case err := <-errCh:
		return err
	case <-c.closed:
		return nil
	}
}

func jitter(d time.Duration) time.Duration {
	if d <= 0 {
		return 0
	}
	// full jitter in [d/2, d]
	half := d / 2
	return half + time.Duration(rand.Int63n(int64(half)+1))
}
