// Package transport is the AMA-side gRPC adapter for the long-lived Session
// stream (design note §1, SSOT §5.4). AMA dials AMS outbound; the stream carries
// AmaMessages up and AmsCommands down. On disconnect it reconnects with
// exponential backoff (1s -> 30s, jittered).
package transport

import (
	"context"
	"errors"
	"math/rand"
	"sync"
	"time"

	amxv1 "github.com/2kwanghee/AMX/contracts/gen/go"
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"
)

const (
	backoffMin = 1 * time.Second
	backoffMax = 30 * time.Second
)

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

// Dial constructs a Client for addr.
//
// TODO(P2): TLS is mandatory in production (SSOT §5.4 / §7 in-transit). P2 allows
// insecure transport for local E2E only; a deployment MUST supply TLS creds via
// WithDialOptions and this insecure default must not ship.
func Dial(addr string, extra ...grpc.DialOption) *Client {
	opts := []grpc.DialOption{
		grpc.WithTransportCredentials(insecure.NewCredentials()), // TODO(P2): replace with TLS
	}
	opts = append(opts, extra...)
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
