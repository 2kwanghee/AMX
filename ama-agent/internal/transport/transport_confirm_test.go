package transport

import (
	"context"
	"errors"
	"net"
	"sync"
	"testing"
	"time"

	amxv1 "github.com/2kwanghee/AMX/contracts/gen/go"
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/test/bufconn"
)

// TestDrainSendQueueResolvesPending is the core C1 teardown-resolve invariant,
// tested white-box and without a server so it is deterministic: every queued
// confirmed send must resolve with an error when the session tears down, or an
// outbox drain awaiting the result would deadlock. A missed resolution shows up
// here as a timeout, not a hang.
func TestDrainSendQueueResolvesPending(t *testing.T) {
	c := Dial("passthrough:///unused")

	done1, ok1 := c.SendConfirmed(&amxv1.AmaMessage{})
	done2, ok2 := c.SendConfirmed(&amxv1.AmaMessage{})
	if !ok1 || !ok2 {
		t.Fatal("SendConfirmed refused to queue on an open client")
	}
	// A fire-and-forget message interleaved in the buffer must not block the drain.
	if err := c.Send(&amxv1.AmaMessage{}); err != nil {
		t.Fatalf("Send: %v", err)
	}

	// Simulate a session teardown: no pump is pulling sendCh, so the drain owns it.
	c.drainSendQueue(errSessionEnded)

	for i, done := range []<-chan error{done1, done2} {
		select {
		case err := <-done:
			if !errors.Is(err, errSessionEnded) {
				t.Fatalf("confirmed send %d resolved with %v, want errSessionEnded", i, err)
			}
		case <-time.After(2 * time.Second):
			t.Fatalf("confirmed send %d never resolved on teardown (deadlock)", i)
		}
	}
}

// TestSendConfirmedFailsClosedAfterClose: once closed, a confirmed send must fail
// fast rather than block forever waiting on a session that will never come.
func TestSendConfirmedFailsClosedAfterClose(t *testing.T) {
	c := Dial("passthrough:///unused")
	c.Close()
	if _, ok := c.SendConfirmed(&amxv1.AmaMessage{}); ok {
		t.Fatal("SendConfirmed queued after Close; want ok=false")
	}
}

// fakeServer records inbound messages and can be told to close each stream right
// after Register, to exercise session teardown.
type fakeServer struct {
	amxv1.UnimplementedAmxControlPlaneServer
	mu       sync.Mutex
	received []*amxv1.AmaMessage
	closeNow bool // return immediately after the first Recv (force teardown)
}

func (s *fakeServer) Session(stream grpc.BidiStreamingServer[amxv1.AmaMessage, amxv1.AmsCommand]) error {
	for {
		msg, err := stream.Recv()
		if err != nil {
			return err
		}
		s.mu.Lock()
		s.received = append(s.received, msg)
		closeNow := s.closeNow
		s.mu.Unlock()
		if closeNow {
			return errors.New("server closing stream")
		}
	}
}

func (s *fakeServer) events() []*amxv1.AccountEvent {
	s.mu.Lock()
	defer s.mu.Unlock()
	var out []*amxv1.AccountEvent
	for _, m := range s.received {
		if ev := m.GetEvent(); ev != nil {
			out = append(out, ev)
		}
	}
	return out
}

func startFakeServer(t *testing.T, srv *fakeServer) (*Client, func()) {
	t.Helper()
	lis := bufconn.Listen(1 << 20)
	gs := grpc.NewServer()
	amxv1.RegisterAmxControlPlaneServer(gs, srv)
	go func() { _ = gs.Serve(lis) }()

	dialer := grpc.WithContextDialer(func(ctx context.Context, _ string) (net.Conn, error) {
		return lis.DialContext(ctx)
	})
	c := Dial("passthrough:///bufnet", dialer, grpc.WithTransportCredentials(insecure.NewCredentials()))
	return c, func() {
		c.Close()
		gs.Stop()
		_ = lis.Close()
	}
}

// TestConfirmedSendDeliversExactlyOnce: on a healthy session a confirmed send
// resolves nil and the event reaches the server exactly once (C1 happy path).
func TestConfirmedSendDeliversExactlyOnce(t *testing.T) {
	srv := &fakeServer{}
	c, cleanup := startFakeServer(t, srv)
	defer cleanup()

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go func() { _ = c.Run(ctx) }()

	ev := &amxv1.AccountEvent{EventId: "evt-happy"}
	done, ok := c.SendConfirmed(&amxv1.AmaMessage{Msg: &amxv1.AmaMessage_Event{Event: ev}})
	if !ok {
		t.Fatal("SendConfirmed refused to queue")
	}
	select {
	case err := <-done:
		if err != nil {
			t.Fatalf("confirmed send resolved with error: %v", err)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("confirmed send never resolved on a healthy session")
	}

	// Give the server a moment to record the message, then assert exactly one.
	deadline := time.Now().Add(2 * time.Second)
	for time.Now().Before(deadline) {
		if len(srv.events()) >= 1 {
			break
		}
		time.Sleep(10 * time.Millisecond)
	}
	got := srv.events()
	if len(got) != 1 {
		t.Fatalf("server received %d events, want exactly 1", len(got))
	}
	if got[0].GetEventId() != "evt-happy" {
		t.Fatalf("server received event_id %q, want evt-happy", got[0].GetEventId())
	}
}

// TestTeardownResolvesNoDeadlock: when the server closes every stream right after
// Register, a confirmed send must still resolve (delivered, or errored on
// teardown) within a bounded time — never hang. Run with -race to catch a resolve
// race on the done channel.
func TestTeardownResolvesNoDeadlock(t *testing.T) {
	srv := &fakeServer{closeNow: true}
	c, cleanup := startFakeServer(t, srv)
	defer cleanup()

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go func() { _ = c.Run(ctx) }()

	done, ok := c.SendConfirmed(&amxv1.AmaMessage{Msg: &amxv1.AmaMessage_Event{Event: &amxv1.AccountEvent{EventId: "evt-teardown"}}})
	if !ok {
		t.Fatal("SendConfirmed refused to queue")
	}
	select {
	case <-done:
		// Resolved (nil or error) — the point is it did not deadlock.
	case <-time.After(5 * time.Second):
		t.Fatal("confirmed send never resolved across session teardown (deadlock)")
	}
}
