package agent

import (
	"bytes"
	"crypto/hmac"
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"time"

	"github.com/henrygd/beszel/agent/utils"
)

// This file is part of the Ohmz fork of Beszel (see docs/OHMZ-REDESIGN.md, phase 3).
//
// It lets the dashboard acknowledge an issue: the UI asks the hub, the hub asks the AGENT, and the
// agent writes a signed request into the maintenance engine's inbox -- exactly the envelope the web
// container writes today. The runner validates it as it always does (HMAC with ack/web.key, schema,
// freshness, rate limit) and applies it on its next tick. The agent builds and signs the request
// itself, so the hub never supplies raw bytes and cannot forge a request.

const defaultMaintenanceAckDir = "/var/lib/homelab-maint/ack"

// enqueueAckRequest validates the fields, builds the signed request and drops it in the inbox.
// All strings are reduced to printable ASCII so the bytes we sign are byte-identical to the
// canonical form the Python runner recomputes (json.dumps with ensure_ascii=True, sort_keys).
func enqueueAckRequest(kind, fp, severity, note string, days int) error {
	kind = strings.TrimSpace(kind)
	if kind != "ack" && kind != "unack" {
		return fmt.Errorf("invalid kind")
	}
	fp = strings.ToLower(strings.TrimSpace(fp))
	if len(fp) != 16 || !isHex16(fp) {
		return fmt.Errorf("invalid fingerprint")
	}
	sev := strings.TrimSpace(severity)
	if sev != "" && sev != "warn" && sev != "crit" {
		return fmt.Errorf("invalid severity")
	}
	note = asciiNote(note)
	if len(note) > 200 {
		note = string([]rune(note)[:200])
	}
	if kind == "ack" {
		if sev == "" {
			return fmt.Errorf("ack needs a severity")
		}
		if days < 0 || days > 365 {
			return fmt.Errorf("days out of range")
		}
	}

	dir := defaultMaintenanceAckDir
	if v, ok := utils.GetEnv("MAINTENANCE_ACK_DIR"); ok && v != "" {
		dir = v
	}
	key, err := readAckKey(filepath.Join(dir, "web.key"))
	if err != nil {
		return err
	}

	// Integers only, and ASCII strings only: then Go's json.Marshal (compact, sorted map keys,
	// HTML escaping off) produces exactly Python's json.dumps(..., sort_keys=True,
	// separators=(",", ":"), ensure_ascii=True) for the same object.
	req := map[string]any{"v": 1, "kind": kind, "source": "web", "fp": fp, "ts": time.Now().Unix()}
	if kind == "ack" {
		req["severity"] = sev
		if days > 0 {
			req["days"] = days
		}
		if note != "" {
			req["note"] = note
		}
	}
	sig, err := signRequest(req, key)
	if err != nil {
		return err
	}
	req["sig"] = sig
	body, err := marshalNoHTML(req)
	if err != nil {
		return err
	}
	return writeInbox(filepath.Join(dir, "inbox"), body)
}

// canonical serialises the request without "sig", map keys sorted, no spaces, no HTML escaping.
func canonical(req map[string]any) ([]byte, error) {
	sigless := make(map[string]any, len(req))
	for k, v := range req {
		if k != "sig" {
			sigless[k] = v
		}
	}
	return marshalNoHTML(sigless)
}

func marshalNoHTML(v any) ([]byte, error) {
	var buf bytes.Buffer
	enc := json.NewEncoder(&buf)
	enc.SetEscapeHTML(false)
	if err := enc.Encode(v); err != nil {
		return nil, err
	}
	return bytes.TrimRight(buf.Bytes(), "\n"), nil // Encode appends a newline; Python's dumps does not
}

func signRequest(req map[string]any, key []byte) (string, error) {
	body, err := canonical(req)
	if err != nil {
		return "", err
	}
	mac := hmac.New(sha256.New, key)
	mac.Write(body)
	return hex.EncodeToString(mac.Sum(nil)), nil
}

func readAckKey(path string) ([]byte, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, fmt.Errorf("read web.key: %w", err)
	}
	key := []byte(strings.TrimSpace(string(data)))
	if len(key) < 32 {
		return nil, fmt.Errorf("web.key too short")
	}
	return key, nil
}

// writeInbox writes the request atomically under <epoch_ms>-<8hex>.json (the runner's name pattern).
func writeInbox(inbox string, body []byte) error {
	name := fmt.Sprintf("%d-%s.json", time.Now().UnixMilli(), randomHex(4))
	tmp := filepath.Join(inbox, ".w-"+randomHex(3))
	if err := os.WriteFile(tmp, body, 0o600); err != nil {
		return err
	}
	if err := os.Rename(tmp, filepath.Join(inbox, name)); err != nil {
		_ = os.Remove(tmp)
		return err
	}
	return nil
}

func isHex16(s string) bool {
	for _, c := range s {
		if !(c >= '0' && c <= '9' || c >= 'a' && c <= 'f') {
			return false
		}
	}
	return true
}

// asciiNote keeps printable ASCII (space..~) only, so the signed bytes never differ from Python's
// ensure_ascii output. Non-ASCII and control characters become spaces.
func asciiNote(s string) string {
	var b strings.Builder
	for _, c := range s {
		if c >= 0x20 && c <= 0x7e {
			b.WriteRune(c)
		} else {
			b.WriteByte(' ')
		}
	}
	return strings.TrimSpace(strings.Join(strings.Fields(b.String()), " "))
}

func randomHex(n int) string {
	b := make([]byte, n)
	if _, err := rand.Read(b); err != nil {
		for i := range b {
			b[i] = byte(time.Now().UnixNano() >> (uint(i) * 5))
		}
	}
	return hex.EncodeToString(b)[:n]
}
