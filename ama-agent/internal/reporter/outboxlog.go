package reporter

import (
	"bufio"
	"encoding/base64"
	"encoding/json"
	"os"
	"path/filepath"
	"sync"

	amxv1 "github.com/2kwanghee/AMX/contracts/gen/go"
	"google.golang.org/protobuf/proto"
)

// OutboxLogFileName is the append-only sidecar that persists queued AccountEvents
// so an at-limit switch survives an agent restart (C1 W1). Events are
// credential-free (§7), so the log is plaintext, mirroring the applied.log sidecar
// pattern (store.AppliedLog): line-wise atomic append, corrupt-tail skip on load.
const OutboxLogFileName = "outbox.log"

// outboxCompactThreshold is how many delete tombstones may accumulate before the
// log is rewritten to just its live records, so append-only growth stays bounded
// on a long-lived agent that never restarts. A var (not const) only so tests can
// lower it to force compaction; production never changes it.
var outboxCompactThreshold = 256

// outboxItem is one queued event plus the monotonic sequence that keys its
// on-disk add/del records (event_id is not a stable delete key: emitters may omit
// it, and it only drives dedupe).
type outboxItem struct {
	seq uint64
	ev  *amxv1.AccountEvent
}

// outboxRecord is one JSON line in the log. "add" carries the base64 proto of the
// event; "del" is a tombstone that retires the add with the same seq.
type outboxRecord struct {
	Op  string `json:"op"`           // "add" | "del"
	Seq uint64 `json:"seq"`          // monotonic per-event key
	Ev  string `json:"ev,omitempty"` // base64(proto.Marshal(event)) for "add"
}

// outboxLog is the append-only disk backing for the Outbox. Add and del are
// durable (write + fsync) before returning so a crash cannot lose a queued event
// (W1) or resurrect a confirmed-and-deleted one on the happy path.
type outboxLog struct {
	mu   sync.Mutex
	path string
	f    *os.File // O_APPEND write handle
	dels int      // tombstones written since the last compaction
}

// openOutboxLog loads dir/outbox.log, compacts it to its live records, and
// returns the live items (ascending seq) plus the highest seq seen (so the Outbox
// can resume the monotonic counter without colliding with retired records).
func openOutboxLog(dir string) (*outboxLog, []outboxItem, uint64, error) {
	if err := os.MkdirAll(dir, 0o700); err != nil {
		return nil, nil, 0, err
	}
	path := filepath.Join(dir, OutboxLogFileName)
	items, maxSeq, err := loadOutbox(path)
	if err != nil {
		return nil, nil, 0, err
	}
	// Compact on load: collapse the add/del history to just the live adds so the
	// file cannot grow across restarts.
	if err := rewriteOutbox(path, items); err != nil {
		return nil, nil, 0, err
	}
	f, err := os.OpenFile(path, os.O_APPEND|os.O_CREATE|os.O_WRONLY, 0o600)
	if err != nil {
		return nil, nil, 0, err
	}
	return &outboxLog{path: path, f: f}, items, maxSeq, nil
}

// loadOutbox replays the log into the set of live (added but not deleted) events,
// preserving enqueue order. A corrupt trailing line (a torn write from a crash)
// fails to parse and is skipped, so recovery never aborts on it.
func loadOutbox(path string) ([]outboxItem, uint64, error) {
	f, err := os.Open(path)
	if err != nil {
		if os.IsNotExist(err) {
			return nil, 0, nil
		}
		return nil, 0, err
	}
	defer f.Close()

	sc := bufio.NewScanner(f)
	sc.Buffer(make([]byte, 0, 64*1024), 1<<20)
	live := make(map[uint64]*amxv1.AccountEvent)
	var order []uint64
	var maxSeq uint64
	for sc.Scan() {
		line := sc.Bytes()
		if len(line) == 0 {
			continue
		}
		var r outboxRecord
		if err := json.Unmarshal(line, &r); err != nil {
			continue // corrupt/torn tail line: skip rather than fail the log
		}
		if r.Seq > maxSeq {
			maxSeq = r.Seq
		}
		switch r.Op {
		case "add":
			raw, err := base64.StdEncoding.DecodeString(r.Ev)
			if err != nil {
				continue
			}
			ev := &amxv1.AccountEvent{}
			if err := proto.Unmarshal(raw, ev); err != nil {
				continue
			}
			if _, ok := live[r.Seq]; !ok {
				order = append(order, r.Seq)
			}
			live[r.Seq] = ev
		case "del":
			delete(live, r.Seq)
		}
	}
	items := make([]outboxItem, 0, len(live))
	for _, seq := range order {
		if ev, ok := live[seq]; ok {
			items = append(items, outboxItem{seq: seq, ev: ev})
		}
	}
	// A scanner error other than a torn tail is not fatal here: we return the
	// records recovered so far rather than dropping the whole queue.
	return items, maxSeq, nil
}

// appendAdd durably records a newly enqueued event.
func (l *outboxLog) appendAdd(seq uint64, ev *amxv1.AccountEvent) error {
	raw, err := proto.Marshal(ev)
	if err != nil {
		return err
	}
	return l.writeRecord(outboxRecord{Op: "add", Seq: seq, Ev: base64.StdEncoding.EncodeToString(raw)})
}

// appendDel durably retires a confirmed-and-delivered event.
func (l *outboxLog) appendDel(seq uint64) error {
	if err := l.writeRecord(outboxRecord{Op: "del", Seq: seq}); err != nil {
		return err
	}
	l.mu.Lock()
	l.dels++
	l.mu.Unlock()
	return nil
}

func (l *outboxLog) writeRecord(r outboxRecord) error {
	b, err := json.Marshal(r)
	if err != nil {
		return err
	}
	b = append(b, '\n')
	l.mu.Lock()
	defer l.mu.Unlock()
	if _, err := l.f.Write(b); err != nil {
		return err
	}
	return l.f.Sync()
}

// shouldCompact reports whether enough tombstones have accumulated to warrant a
// rewrite.
func (l *outboxLog) shouldCompact() bool {
	l.mu.Lock()
	defer l.mu.Unlock()
	return l.dels >= outboxCompactThreshold
}

// compactMidHook, when non-nil, is called inside compact after the log has been
// rewritten (old inode now unlinked) but before the append fd is swapped — the
// exact window in which a concurrent, unsynchronized Enqueue would write to the
// dead inode and lose its event. Test-only; nil in production.
var compactMidHook func()

// compact rewrites the log to just the given live items and reopens the append
// handle. The caller supplies the authoritative live set (the Outbox's in-memory
// queue) so compaction cannot race a concurrent replay.
func (l *outboxLog) compact(items []outboxItem) error {
	if err := rewriteOutbox(l.path, items); err != nil {
		return err
	}
	if compactMidHook != nil {
		compactMidHook()
	}
	f, err := os.OpenFile(l.path, os.O_APPEND|os.O_CREATE|os.O_WRONLY, 0o600)
	if err != nil {
		return err
	}
	l.mu.Lock()
	old := l.f
	l.f = f
	l.dels = 0
	l.mu.Unlock()
	if old != nil {
		_ = old.Close()
	}
	return nil
}

// rewriteOutbox atomically replaces path with add records for items (temp file +
// fsync + rename), so a crash mid-compaction leaves either the old or the new full
// log, never a truncated one.
func rewriteOutbox(path string, items []outboxItem) error {
	var buf []byte
	for _, it := range items {
		raw, err := proto.Marshal(it.ev)
		if err != nil {
			return err
		}
		b, err := json.Marshal(outboxRecord{Op: "add", Seq: it.seq, Ev: base64.StdEncoding.EncodeToString(raw)})
		if err != nil {
			return err
		}
		buf = append(buf, b...)
		buf = append(buf, '\n')
	}
	dir := filepath.Dir(path)
	tmp, err := os.CreateTemp(dir, ".outbox-*")
	if err != nil {
		return err
	}
	tmpName := tmp.Name()
	defer os.Remove(tmpName)
	if err := tmp.Chmod(0o600); err != nil {
		tmp.Close()
		return err
	}
	if _, err := tmp.Write(buf); err != nil {
		tmp.Close()
		return err
	}
	if err := tmp.Sync(); err != nil {
		tmp.Close()
		return err
	}
	if err := tmp.Close(); err != nil {
		return err
	}
	return os.Rename(tmpName, path)
}
