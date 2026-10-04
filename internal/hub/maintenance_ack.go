package hub

import (
	"crypto/hmac"
	"crypto/rand"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"time"

	"github.com/henrygd/beszel/internal/common"
	"github.com/pocketbase/pocketbase/core"
)

// Ohmz fork: acknowledgement tokens for the maintenance engine's e-mail links are now MINTED BY THE
// HUB (the new site), not the engine. The engine asks the hub for a token when it sends an alert
// e-mail (POST /maintenance/ack/mint, authenticated with an HMAC over the body under the engine's
// existing ack/web.key); the e-mail links to <maintainer>/ack?id=..&t=<token>&d=..&s=..; the hub
// validates the token (only its SHA-256 is stored) and applies the ack through the same agent path
// the dashboard button uses. Nothing here trusts a token without a matching stored hash.

const (
	ackTokenTTL   = 30 * 24 * time.Hour
	ackMaxTokens  = 4096
	ackStoreLimit = 8 << 20
)

type ackToken struct {
	Hash   string `json:"hash"` // sha256(token), hex
	Fp     string `json:"fp"`
	Sev    string `json:"sev"`
	Days   int    `json:"days"`
	Exp    int64  `json:"exp"`
	Used   bool   `json:"used"`
	System string `json:"system"`
	Title  string `json:"title,omitempty"`
}

type ackStore struct {
	mu     sync.Mutex
	path   string
	tokens map[string]ackToken
}

var (
	ackStoreOnce sync.Once
	ackStoreInst *ackStore
)

func ackTokenStore() *ackStore {
	ackStoreOnce.Do(func() {
		path := os.Getenv("MAINTENANCE_ACK_STORE")
		if path == "" {
			path = "/var/lib/beszel/maintenance-acks.json"
		}
		s := &ackStore{path: path, tokens: map[string]ackToken{}}
		if data, err := os.ReadFile(path); err == nil && len(data) < ackStoreLimit {
			_ = json.Unmarshal(data, &s.tokens)
		}
		ackStoreInst = s
	})
	return ackStoreInst
}

// save writes the store atomically. Callers hold the mutex.
func (s *ackStore) save() {
	now := time.Now().Unix()
	if len(s.tokens) > ackMaxTokens { // drop the oldest/expired first so the file cannot grow without bound
		for k, t := range s.tokens {
			if t.Used || t.Exp < now {
				delete(s.tokens, k)
			}
		}
	}
	data, err := json.Marshal(s.tokens)
	if err != nil {
		return
	}
	tmp := s.path + ".tmp"
	if err := os.MkdirAll(filepath.Dir(s.path), 0o700); err != nil {
		return
	}
	if err := os.WriteFile(tmp, data, 0o600); err != nil {
		return
	}
	_ = os.Rename(tmp, s.path)
}

func ackHash(token string) string {
	sum := sha256.Sum256([]byte(token))
	return hex.EncodeToString(sum[:])
}

// ackEngineKey reads the engine's ack/web.key (the shared secret the engine signs mint requests with).
func ackEngineKey() ([]byte, error) {
	path := os.Getenv("MAINTENANCE_ACK_KEY_FILE")
	if path == "" {
		path = "/var/lib/homelab-maint/ack/web.key"
	}
	key, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	return key, nil
}

func ackVerifySig(body []byte, sigHex string) bool {
	if sigHex == "" {
		return false
	}
	key, err := ackEngineKey()
	if err != nil || len(key) == 0 {
		return false
	}
	want := hmac.New(sha256.New, key)
	want.Write(body)
	got, err := hex.DecodeString(sigHex)
	if err != nil {
		return false
	}
	return hmac.Equal(got, want.Sum(nil))
}

// resolveAckSystem finds the system record to apply acknowledgements to. The engine's host name is
// matched against the system's name when present; otherwise the single system is used.
func (h *Hub) resolveAckSystem(host string) (string, error) {
	recs, err := h.FindAllRecords("systems")
	if err != nil {
		return "", err
	}
	if len(recs) == 0 {
		return "", os.ErrNotExist
	}
	pick := recs[0].Id
	if host != "" {
		for _, r := range recs {
			if name := r.GetString("name"); name != "" && strings.EqualFold(name, host) {
				pick = r.Id
				break
			}
		}
	}
	return pick, nil
}

// maintenanceAckMint handles POST /api/beszel/maintenance/ack/mint (Ohmz fork). Called by the engine
// (localhost) when it renders an alert e-mail; authenticated with an HMAC over the exact body under
// ack/web.key. Returns a fresh one-time token; only its hash is stored.
func (h *Hub) maintenanceAckMint(e *core.RequestEvent) error {
	body, err := io.ReadAll(io.LimitReader(e.Request.Body, 64<<10))
	if err != nil {
		return e.JSON(http.StatusBadRequest, map[string]any{"error": "unreadable body"})
	}
	if !ackVerifySig(body, e.Request.Header.Get("X-HM-Signature")) {
		return e.JSON(http.StatusUnauthorized, map[string]any{"error": "bad signature"})
	}
	var req struct {
		Fp    string `json:"fp"`
		Sev   string `json:"sev"`
		Days  int    `json:"days"`
		Host  string `json:"host"`
		Title string `json:"title"`
	}
	if err := json.Unmarshal(body, &req); err != nil || req.Fp == "" {
		return e.JSON(http.StatusBadRequest, map[string]any{"error": "invalid request"})
	}
	if req.Days < 1 || req.Days > 365 {
		req.Days = 90
	}
	if req.Sev != "crit" {
		req.Sev = "warn"
	}
	systemID, err := h.resolveAckSystem(req.Host)
	if err != nil {
		return e.JSON(http.StatusServiceUnavailable, map[string]any{"error": "no system"})
	}
	raw := make([]byte, 32)
	if _, err := rand.Read(raw); err != nil {
		return e.JSON(http.StatusInternalServerError, map[string]any{"error": "token"})
	}
	token := base64.RawURLEncoding.EncodeToString(raw)
	s := ackTokenStore()
	s.mu.Lock()
	s.tokens[ackHash(token)] = ackToken{
		Hash: ackHash(token), Fp: req.Fp, Sev: req.Sev, Days: req.Days,
		Exp: time.Now().Add(ackTokenTTL).Unix(), System: systemID, Title: req.Title,
	}
	s.save()
	s.mu.Unlock()
	return e.JSON(http.StatusOK, map[string]any{"token": token})
}

// maintenanceAckPeek handles GET /api/beszel/maintenance/ack/peek?token=.. (Ohmz fork). Public and
// read-only: it tells the confirm page what the token would acknowledge. It reveals no secret.
func (h *Hub) maintenanceAckPeek(e *core.RequestEvent) error {
	token := e.Request.URL.Query().Get("token")
	now := time.Now().Unix()
	resp := map[string]any{"valid": false}
	if token == "" {
		return e.JSON(http.StatusOK, resp)
	}
	s := ackTokenStore()
	s.mu.Lock()
	t, ok := s.tokens[ackHash(token)]
	s.mu.Unlock()
	if !ok || t.Exp < now || t.Used {
		return e.JSON(http.StatusOK, resp)
	}
	return e.JSON(http.StatusOK, map[string]any{
		"valid": true, "fp": t.Fp, "sev": t.Sev, "days": t.Days, "exp": t.Exp, "title": t.Title,
	})
}

// maintenanceAckCommit handles POST /api/beszel/maintenance/ack/commit (Ohmz fork). Public, but it
// needs a valid unguessable token: it applies the acknowledgement through the agent and burns the
// token (single use).
func (h *Hub) maintenanceAckCommit(e *core.RequestEvent) error {
	var req struct {
		Token string `json:"token"`
		Note  string `json:"note"`
	}
	if err := e.BindBody(&req); err != nil || req.Token == "" {
		return e.JSON(http.StatusBadRequest, map[string]any{"error": "invalid request"})
	}
	now := time.Now().Unix()
	s := ackTokenStore()
	s.mu.Lock()
	t, ok := s.tokens[ackHash(req.Token)]
	if !ok || t.Exp < now || t.Used {
		s.mu.Unlock()
		return e.JSON(http.StatusBadRequest, map[string]any{"ok": false, "reason": "invalid"})
	}
	sys, err := h.sm.GetSystem(t.System)
	if err != nil {
		s.mu.Unlock()
		return e.JSON(http.StatusServiceUnavailable, map[string]any{"ok": false, "reason": "system"})
	}
	res, err := sys.MaintenanceAckFromAgent(common.MaintenanceAckRequest{
		Kind: "ack", Fp: t.Fp, Severity: t.Sev, Note: req.Note, Days: t.Days,
	})
	if err != nil || !res.OK {
		s.mu.Unlock()
		return e.JSON(http.StatusBadGateway, map[string]any{"ok": false, "reason": "agent"})
	}
	t.Used = true
	s.tokens[t.Hash] = t
	s.save()
	s.mu.Unlock()
	return e.JSON(http.StatusOK, map[string]any{"ok": true, "fp": t.Fp, "days": t.Days})
}
