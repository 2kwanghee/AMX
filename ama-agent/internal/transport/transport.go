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

// errSessionEnded resolves a confirmed send whose event was still queued (or
// mid-flight) when the session tore down, so the awaiting drain never deadlocks
// (C1 teardown resolve). The event stays on disk and is retried on reconnect.
var errSessionEnded = errors.New("transport: session ended before send confirmed")

// sendItem is one queued upstream message. done, when non-nil, is a buffered
// channel that receives the stream.Send result (or errSessionEnded on teardown):
// this is the C1 confirmation the outbox waits on before deleting an event.
// Fire-and-forget messages (usage, acks, register) leave done nil.
type sendItem struct {
	msg  *amxv1.AmaMessage
	done chan error
}

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
		// Both paths share one tls.Config so the TLS floor (MinVersion) and server
		// verification are identical; only the client certificate differs.
		// ServerName empty => gRPC fills it from the dial host (prior behaviour).
		caPEM, err := os.ReadFile(ca)
		if err != nil {
			return nil, fmt.Errorf("transport: read TLS CA %q: %w", ca, err)
		}
		pool := x509.NewCertPool()
		if !pool.AppendCertsFromPEM(caPEM) {
			return nil, fmt.Errorf("transport: no certificates parsed from TLS CA %q", ca)
		}
		cfg := &tls.Config{
			RootCAs:    pool,
			ServerName: os.Getenv(EnvTLSServerName),
			MinVersion: tls.VersionTLS12,
		}
		if clientCert != "" {
			// Mutual TLS: additionally present our own cert.
			cert, err := tls.LoadX509KeyPair(clientCert, clientKey)
			if err != nil {
				return nil, fmt.Errorf("transport: load client key pair: %w", err)
			}
			cfg.Certificates = []tls.Certificate{cert}
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

	sendCh chan sendItem
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
		sendCh: make(chan sendItem, 64),
		recvCh: make(chan *amxv1.AmsCommand, 64),
		closed: make(chan struct{}),
	}
}

// Send queues a message for the current stream. It blocks only if the buffer is
// full; it returns an error after Close. Fire-and-forget: no delivery confirmation.
func (c *Client) Send(msg *amxv1.AmaMessage) error {
	select {
	case <-c.closed:
		return errors.New("transport: closed")
	case c.sendCh <- sendItem{msg: msg}:
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
	case c.sendCh <- sendItem{msg: msg}:
		return true
	default:
		return false
	}
}

// SendConfirmed queues msg and returns a channel that yields the eventual
// stream.Send result: nil once the message is on the wire, or an error (including
// errSessionEnded on a teardown) if it was not. The single upstream goroutine is
// the only caller of stream.Send, so ordering and the gRPC "one sender" rule hold.
// ok is false only after Close. This is the C1 confirmation path for AccountEvents:
// the outbox deletes an event from disk only when the returned channel yields nil.
func (c *Client) SendConfirmed(msg *amxv1.AmaMessage) (<-chan error, bool) {
	// Check closed first: with buffer space free, a bare select would pick the
	// enqueue and the closed cases at random, letting a send slip in after Close.
	select {
	case <-c.closed:
		return nil, false
	default:
	}
	done := make(chan error, 1)
	select {
	case <-c.closed:
		return nil, false
	case c.sendCh <- sendItem{msg: msg, done: done}:
		return done, true
	}
}

// resolve delivers err to a confirmed send's waiter. done is buffered (cap 1) and
// each item is consumed exactly once (by the pump or by the teardown drain), so
// this never blocks and never double-sends.
func resolve(it sendItem, err error) {
	if it.done != nil {
		it.done <- err
	}
}

// drainSendQueue empties any buffered sendItems, failing each confirmed send with
// cause so no outbox drain deadlocks waiting on a session that has ended. It must
// run only when no upstream goroutine is pulling sendCh (after the pump exits, or
// after Run's loop stops), so an item is resolved exactly once.
func (c *Client) drainSendQueue(cause error) {
	for {
		select {
		case it := <-c.sendCh:
			resolve(it, cause)
		default:
			return
		}
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
	// Once the run loop exits, no session will ever pull sendCh again; resolve any
	// stragglers so a confirmed send queued during the final gap cannot deadlock.
	defer c.drainSendQueue(errSessionEnded)
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
	var wg sync.WaitGroup
	// Downstream: commands -> recvCh.
	wg.Add(1)
	go func() {
		defer wg.Done()
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
	// Upstream: the sole caller of stream.Send. Each item's confirmation channel is
	// resolved with the stream.Send result so a confirmed send learns its fate.
	wg.Add(1)
	go func() {
		defer wg.Done()
		for {
			select {
			case it := <-c.sendCh:
				err := stream.Send(it.msg)
				resolve(it, err)
				if err != nil {
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

	var sessErr error
	select {
	case sessErr = <-errCh:
	case <-c.closed:
		sessErr = nil
	}
	// Tear down: stop both goroutines, wait until the upstream pump has stopped
	// pulling sendCh, then fail any items it left buffered. Ordering matters — the
	// drain must run only after the pump is gone so each item is resolved once.
	// This is the C1 teardown-resolve guarantee that keeps the outbox drain from
	// deadlocking on a session that has died.
	cancel()
	wg.Wait()
	c.drainSendQueue(errSessionEnded)
	return sessErr
}

func jitter(d time.Duration) time.Duration {
	if d <= 0 {
		return 0
	}
	// full jitter in [d/2, d]
	half := d / 2
	return half + time.Duration(rand.Int63n(int64(half)+1))
}
