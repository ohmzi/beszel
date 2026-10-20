package agent

import (
	"bytes"
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/henrygd/beszel/internal/common"
)

// netBlockInbox points the agent at a temp ack directory with a web.key and an empty inbox.
func netBlockInbox(t *testing.T) string {
	t.Helper()
	dir := t.TempDir()
	if err := os.WriteFile(filepath.Join(dir, "web.key"), []byte(strings.Repeat("k", 40)), 0o600); err != nil {
		t.Fatal(err)
	}
	inbox := filepath.Join(dir, "inbox")
	if err := os.Mkdir(inbox, 0o700); err != nil {
		t.Fatal(err)
	}
	t.Setenv("MAINTENANCE_ACK_DIR", dir)
	return inbox
}

func validNetBlock() common.MaintenanceNetBlockRequest {
	return common.MaintenanceNetBlockRequest{
		Kind:           "net_block",
		Finding:        "0123456789abcdef",
		IP:             "203.0.113.7",
		Target:         "cloudflare",
		Zone:           "example.test",
		Days:           30,
		OverrideReason: "owner says ok / 2nd host",
	}
}

// The golden strings are Python's acks.canonical(...) on the same dicts (engine/homelab_maint/acks.py),
// generated with ts=1787000000. The signed bytes must be identical, byte for byte.
func TestNetBlockCanonicalMatchesPython(t *testing.T) {
	const ts = 1787000000
	host := validNetBlock()
	host.Target, host.Zone, host.IP, host.Days, host.Permanent, host.OverrideReason = "host", "", "2001:db8::1", 365, true, ""

	unblock := common.MaintenanceNetBlockRequest{Kind: "net_unblock", BlockID: "fedcba9876543210"}

	cases := []struct {
		name   string
		req    common.MaintenanceNetBlockRequest
		golden string
	}{
		{
			name:   "cloudflare net_block",
			req:    validNetBlock(),
			golden: `{"days":30,"finding":"0123456789abcdef","ip":"203.0.113.7","kind":"net_block","override_reason":"owner says ok / 2nd host","permanent":false,"source":"web","target":"cloudflare","ts":1787000000,"v":1,"zone":"example.test"}`,
		},
		{
			name:   "host net_block, permanent, v6",
			req:    host,
			golden: `{"days":365,"finding":"0123456789abcdef","ip":"2001:db8::1","kind":"net_block","override_reason":"","permanent":true,"source":"web","target":"host","ts":1787000000,"v":1,"zone":""}`,
		},
		{
			name:   "net_unblock",
			req:    unblock,
			golden: `{"block_id":"fedcba9876543210","kind":"net_unblock","source":"web","ts":1787000000,"v":1}`,
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			m, err := buildNetBlockRequest(tc.req, ts)
			if err != nil {
				t.Fatalf("build: %v", err)
			}
			got, err := canonical(m)
			if err != nil {
				t.Fatalf("canonical: %v", err)
			}
			if string(got) != tc.golden {
				t.Fatalf("canonical bytes differ from Python\n got: %s\nwant: %s", got, tc.golden)
			}
		})
	}
}

func TestNetBlockRejectsBadFields(t *testing.T) {
	cases := map[string]func(r *common.MaintenanceNetBlockRequest){
		"unknown kind":              func(r *common.MaintenanceNetBlockRequest) { r.Kind = "ack" },
		"empty kind":                func(r *common.MaintenanceNetBlockRequest) { r.Kind = "" },
		"finding uppercase":         func(r *common.MaintenanceNetBlockRequest) { r.Finding = "0123456789ABCDEF" },
		"finding short":             func(r *common.MaintenanceNetBlockRequest) { r.Finding = "0123456789abcde" },
		"finding not hex":           func(r *common.MaintenanceNetBlockRequest) { r.Finding = "0123456789abcdeg" },
		"finding empty":             func(r *common.MaintenanceNetBlockRequest) { r.Finding = "" },
		"ip empty":                  func(r *common.MaintenanceNetBlockRequest) { r.IP = "" },
		"ip not an address":         func(r *common.MaintenanceNetBlockRequest) { r.IP = "example.test" },
		"ip with zone":              func(r *common.MaintenanceNetBlockRequest) { r.IP = "fe80::1%eth0" },
		"target unknown":            func(r *common.MaintenanceNetBlockRequest) { r.Target = "both" },
		"target empty":              func(r *common.MaintenanceNetBlockRequest) { r.Target = "" },
		"cloudflare without zone":   func(r *common.MaintenanceNetBlockRequest) { r.Zone = "" },
		"cloudflare zone bad chars": func(r *common.MaintenanceNetBlockRequest) { r.Zone = "bad zone!" },
		"cloudflare zone too long":  func(r *common.MaintenanceNetBlockRequest) { r.Zone = strings.Repeat("a", 65) },
		"host with zone": func(r *common.MaintenanceNetBlockRequest) {
			r.Target, r.Zone = "host", "example.test"
		},
		"days zero":     func(r *common.MaintenanceNetBlockRequest) { r.Days = 0 },
		"days negative": func(r *common.MaintenanceNetBlockRequest) { r.Days = -1 },
		"days too big":  func(r *common.MaintenanceNetBlockRequest) { r.Days = 366 },
		"reason too long": func(r *common.MaintenanceNetBlockRequest) {
			r.OverrideReason = strings.Repeat("x", 201)
		},
	}
	for name, mutate := range cases {
		t.Run(name, func(t *testing.T) {
			inbox := netBlockInbox(t)
			r := validNetBlock()
			mutate(&r)
			if err := enqueueNetBlockRequest(r); err == nil {
				t.Fatal("want an error, got nil")
			}
			entries, err := os.ReadDir(inbox)
			if err != nil {
				t.Fatal(err)
			}
			if len(entries) != 0 {
				t.Fatalf("a refused request wrote %d file(s) to the inbox", len(entries))
			}
		})
	}
}

func TestNetBlockRejectsBadUnblockID(t *testing.T) {
	for _, id := range []string{"", "fedcba987654321", "FEDCBA9876543210", "fedcba98765432100", "fedcba987654321g"} {
		inbox := netBlockInbox(t)
		if err := enqueueNetBlockRequest(common.MaintenanceNetBlockRequest{Kind: "net_unblock", BlockID: id}); err == nil {
			t.Fatalf("block id %q: want an error", id)
		}
		if entries, _ := os.ReadDir(inbox); len(entries) != 0 {
			t.Fatalf("block id %q: wrote %d file(s)", id, len(entries))
		}
	}
}

// A v4-mapped IPv6 address is stored as its IPv4 form, as block.py does.
func TestNetBlockUnmapsV4Mapped(t *testing.T) {
	r := validNetBlock()
	r.Target, r.Zone, r.IP = "host", "", "::ffff:203.0.113.7"
	m, err := buildNetBlockRequest(r, 1)
	if err != nil {
		t.Fatal(err)
	}
	if m["ip"] != "203.0.113.7" {
		t.Fatalf("ip = %v, want 203.0.113.7", m["ip"])
	}
}

func TestNetBlockWritesSignedRequest(t *testing.T) {
	inbox := netBlockInbox(t)
	if err := enqueueNetBlockRequest(validNetBlock()); err != nil {
		t.Fatalf("net_block: %v", err)
	}
	if err := enqueueNetBlockRequest(common.MaintenanceNetBlockRequest{Kind: "net_unblock", BlockID: "fedcba9876543210"}); err != nil {
		t.Fatalf("net_unblock: %v", err)
	}
	entries, err := os.ReadDir(inbox)
	if err != nil {
		t.Fatal(err)
	}
	if len(entries) != 2 {
		t.Fatalf("inbox has %d files, want 2", len(entries))
	}
	key := []byte(strings.Repeat("k", 40))
	for _, e := range entries {
		data, err := os.ReadFile(filepath.Join(inbox, e.Name()))
		if err != nil {
			t.Fatal(err)
		}
		dec := json.NewDecoder(bytes.NewReader(data))
		dec.UseNumber()
		var m map[string]any
		if err := dec.Decode(&m); err != nil {
			t.Fatal(err)
		}
		sig, _ := m["sig"].(string)
		delete(m, "sig")
		want, err := signRequest(m, key)
		if err != nil {
			t.Fatal(err)
		}
		if sig != want {
			t.Fatalf("%s: sig does not verify", e.Name())
		}
		if m["source"] != "web" || m["v"] != json.Number("1") {
			t.Fatalf("%s: envelope = %v", e.Name(), m)
		}
	}
}

// The override reason is checked as typed and never rewritten: control characters, DEL, non-ASCII and
// anything over 200 bytes are refused. Only surrounding spaces are trimmed.
func TestNetBlockOverrideReason(t *testing.T) {
	refused := []struct{ name, reason string }{
		{"tab", "owner\tok"},
		{"newline", "owner\nok"},
		{"carriage return", "owner\rok"},
		{"DEL", "owner\x7fok"},
		{"non-ASCII rune", "owner é"},
		{"raw 201 characters", strings.Repeat("x", 201)},
	}
	for _, tc := range refused {
		t.Run(tc.name, func(t *testing.T) {
			r := validNetBlock()
			r.OverrideReason = tc.reason
			if _, err := buildNetBlockRequest(r, 1); err == nil {
				t.Fatalf("reason %q: want an error", tc.reason)
			}
		})
	}

	// 200 bytes with internal space runs: accepted unchanged, because there is nothing to trim.
	long := "x" + strings.Repeat(" ", 99) + "y" + strings.Repeat(" ", 98) + "z"
	if len(long) != 200 {
		t.Fatalf("test string is %d bytes, want 200", len(long))
	}
	accepted := []struct{ name, reason, want string }{
		{"200 bytes with space runs", long, long},
		{"surrounding spaces trimmed", "  a  b  ", "a  b"},
	}
	for _, tc := range accepted {
		t.Run(tc.name, func(t *testing.T) {
			r := validNetBlock()
			r.OverrideReason = tc.reason
			m, err := buildNetBlockRequest(r, 1)
			if err != nil {
				t.Fatalf("reason %q: %v", tc.reason, err)
			}
			if m["override_reason"] != tc.want {
				t.Fatalf("override_reason = %q, want %q", m["override_reason"], tc.want)
			}
		})
	}
}
