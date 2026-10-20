package agent

import (
	"fmt"
	"net/netip"
	"path/filepath"
	"regexp"
	"strings"
	"time"

	"github.com/henrygd/beszel/agent/utils"
	"github.com/henrygd/beszel/internal/common"
)

// This file is part of the Ohmz fork of Beszel (Network Watch).
//
// The dashboard asks to block or unblock an address. The hub asks the AGENT, and the agent writes a
// signed net_block / net_unblock request into the maintenance engine's inbox, exactly as
// maintenance_ack.go does for acks. The engine validates it again (netwatch/block.py _validate) and
// applies it. Nothing is blocked automatically: every request comes from a logged-in dashboard user.

const (
	netBlockMaxDays   = 365
	netBlockReasonMax = 200
)

var netBlockZoneRe = regexp.MustCompile(`^[A-Za-z0-9._-]{1,64}$`)

// enqueueNetBlockRequest validates the request, builds it, signs it with ack/web.key and drops it in
// the inbox. A refused request writes nothing.
func enqueueNetBlockRequest(r common.MaintenanceNetBlockRequest) error {
	req, err := buildNetBlockRequest(r, time.Now().Unix())
	if err != nil {
		return err
	}
	dir := defaultMaintenanceAckDir
	if v, ok := utils.GetEnv("MAINTENANCE_ACK_DIR"); ok && v != "" {
		dir = v
	}
	key, err := readAckKey(filepath.Join(dir, "web.key"))
	if err != nil {
		return err
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

// buildNetBlockRequest checks r exactly as block.py's _validate does and returns the unsigned request.
// Only integers, ASCII strings and booleans go in the map, so marshalNoHTML gives Python's canonical bytes.
func buildNetBlockRequest(r common.MaintenanceNetBlockRequest, ts int64) (map[string]any, error) {
	switch r.Kind {
	case "net_block":
		return buildNetBlock(r, ts)
	case "net_unblock":
		if !isHex16Exact(r.BlockID) {
			return nil, fmt.Errorf("invalid block id")
		}
		return map[string]any{"v": 1, "kind": r.Kind, "source": "web", "ts": ts, "block_id": r.BlockID}, nil
	}
	return nil, fmt.Errorf("invalid kind")
}

func buildNetBlock(r common.MaintenanceNetBlockRequest, ts int64) (map[string]any, error) {
	if !isHex16Exact(r.Finding) {
		return nil, fmt.Errorf("invalid finding")
	}
	addr, err := netip.ParseAddr(strings.TrimSpace(r.IP))
	if err != nil || addr.Zone() != "" {
		return nil, fmt.Errorf("invalid ip")
	}
	addr = addr.Unmap()
	switch r.Target {
	case "host":
		if r.Zone != "" {
			return nil, fmt.Errorf("a host block has no zone")
		}
	case "cloudflare":
		if !netBlockZoneRe.MatchString(r.Zone) {
			return nil, fmt.Errorf("invalid zone")
		}
	default:
		return nil, fmt.Errorf("invalid target")
	}
	if r.Days < 1 || r.Days > netBlockMaxDays {
		return nil, fmt.Errorf("days out of range")
	}
	reason, err := checkOverrideReason(r.OverrideReason)
	if err != nil {
		return nil, err
	}
	return map[string]any{
		"v":               1,
		"kind":            r.Kind,
		"source":          "web",
		"ts":              ts,
		"finding":         r.Finding,
		"ip":              addr.String(),
		"target":          r.Target,
		"zone":            r.Zone,
		"days":            r.Days,
		"permanent":       r.Permanent,
		"override_reason": reason,
	}, nil
}

// checkOverrideReason validates the reason as typed and never rewrites it: control characters, DEL and
// any non-ASCII byte are refused, as is a raw string longer than 200 bytes. Only surrounding spaces are
// trimmed. Go's canonical bytes match Python's only for ASCII, so there is no sanitising step.
func checkOverrideReason(raw string) (string, error) {
	if len(raw) > netBlockReasonMax {
		return "", fmt.Errorf("reason too long")
	}
	for i := 0; i < len(raw); i++ {
		c := raw[i]
		if c < 0x20 || c == 0x7f {
			return "", fmt.Errorf("reason has a control character")
		}
		if c >= 0x80 {
			return "", fmt.Errorf("reason is not ASCII")
		}
	}
	return strings.TrimSpace(raw), nil
}

// isHex16Exact is 16 lowercase hex digits and nothing else, the same check as block.py's _HEX16.
func isHex16Exact(s string) bool {
	return len(s) == 16 && isHex16(s)
}
