package agent

import (
	"encoding/json"
	"log/slog"
	"os"
	"path/filepath"
	"sync"
	"time"

	"github.com/henrygd/beszel/agent/utils"
	"github.com/henrygd/beszel/internal/entities/system"
)

// This file is part of the Ohmz fork of Beszel (see docs/OHMZ-REDESIGN.md). It surfaces the
// homelab-maint maintenance engine as a native Beszel metric.
//
// The Python engine does the real maintenance (checks, cleaners, routine, incidents,
// acknowledgements) and publishes its state to STATE_DIR/public/*.json. This manager READS those
// files on a bounded interval and caches a compact verdict. It never runs the scripts: there is no
// new command surface here, and nothing the agent does can change what the engine reports.

const (
	defaultMaintenanceInterval = time.Minute
	defaultMaintenanceDir      = "/var/lib/homelab-maint/public"
	// Cap what we will parse from a single published file. The real files are tens of KB; a
	// saboteur-sized file is refused rather than loaded.
	maintenanceMaxFile = 8 << 20
	// How many recent delivery-log entries to carry to the hub (newest first).
	maintenanceRecentMax = 15
)

// maintenanceManager periodically reads the published maintenance state in the background and
// caches the verdict, so the timed metrics loop never blocks on disk.
type maintenanceManager struct {
	sync.Mutex
	dir       string
	interval  time.Duration
	result    system.Maintenance
	checkedAt time.Time
	running   bool
}

// newMaintenanceManager returns nil only when explicitly disabled (MAINTENANCE_INTERVAL=0). A
// missing or unreadable directory is not fatal: the verdict is reported as "unknown".
func newMaintenanceManager() *maintenanceManager {
	dir := defaultMaintenanceDir
	if v, ok := utils.GetEnv("MAINTENANCE_PUBLIC_DIR"); ok && v != "" {
		dir = v
	}
	interval := defaultMaintenanceInterval
	if v, ok := utils.GetEnv("MAINTENANCE_INTERVAL"); ok {
		if d, err := time.ParseDuration(v); err == nil {
			if d == 0 {
				slog.Info("MAINTENANCE_INTERVAL", "duration", "disabled")
				return nil
			}
			if d > 0 {
				interval = d
			}
		}
	}
	slog.Debug("Maintenance", "dir", dir, "interval", interval)
	return &maintenanceManager{dir: dir, interval: interval, result: system.Maintenance{Status: "unknown"}}
}

// get returns the cached verdict and starts a background refresh when it is stale. It is cheap and
// never blocks on I/O (the read happens in the goroutine).
func (m *maintenanceManager) get(now time.Time) system.Maintenance {
	m.Lock()
	defer m.Unlock()
	if !m.running && (m.checkedAt.IsZero() || now.Sub(m.checkedAt) >= m.interval) {
		m.running = true
		go m.refresh()
	}
	return m.result
}

func (m *maintenanceManager) refresh() {
	res := m.read()
	m.Lock()
	m.result = res
	m.checkedAt = time.Now()
	m.running = false
	m.Unlock()
}

// levelRank orders the verdict for "worst wins". unknown sorts below ok so a real signal always
// beats it; an all-unknown result stays unknown (handled by statusOf's empty start).
func levelRank(level string) int {
	switch level {
	case "crit":
		return 3
	case "warn":
		return 2
	case "ok":
		return 1
	default:
		return 0
	}
}

func worseStatus(a, b string) string {
	if levelRank(b) > levelRank(a) {
		return b
	}
	return a
}

// read parses the published files and returns the composite verdict. Every file is optional: a
// missing one simply contributes nothing, and the worst level seen across the present files wins.
func (m *maintenanceManager) read() system.Maintenance {
	out := system.Maintenance{}
	status := ""
	readAny := false
	var newest int64

	if d := m.readJSON("self.json"); d != nil {
		readAny = true
		if s, ok := d["level"].(string); ok {
			status = worseStatus(status, s) // the pipeline's own health (ok|warn|crit)
		}
		if h, ok := d["headline"].(string); ok && out.Summary == "" {
			out.Summary = h
		}
		newest = max(newest, generatedAt(d))
	}

	if d := m.readJSON("checks.json"); d != nil {
		readAny = true
		var failing, crit uint16
		for _, c := range listOf(d["checks"]) {
			switch str(c["status"]) {
			case "crit", "error":
				failing++
				crit++
			case "warn":
				failing++
			}
		}
		out.Failing = failing
		if crit > 0 {
			status = worseStatus(status, "crit")
		} else if failing > 0 {
			status = worseStatus(status, "warn")
		}
		newest = max(newest, generatedAt(d))
	}

	if d := m.readJSON("incidents.json"); d != nil {
		readAny = true
		var open, crit uint16
		for _, i := range listOf(d["open"]) {
			// "acknowledged" incidents are still open in the ledger but the owner knows: do not
			// let them hold the verdict at crit.
			switch str(i["status"]) {
			case "open":
				open++
				if s := str(i["severity"]); s == "sev1" || s == "sev2" {
					crit++
				}
			case "acknowledged":
				open++
			}
		}
		out.Incidents = open
		if crit > 0 {
			status = worseStatus(status, "crit")
		}
		newest = max(newest, generatedAt(d))
	}

	if d := m.readJSON("acks.json"); d != nil {
		readAny = true
		if st, ok := d["stats"].(map[string]any); ok {
			out.Acked = uint16(num(st["active"]))
		}
		newest = max(newest, generatedAt(d))
	}

	// The delivery log: everything the engine alerted about (its own alerts and the external ones
	// bridged through it, e.g. Hermes). Surfaced as the central alerts feed. It is not a health
	// signal, so it does not set readAny.
	if d := m.readJSON("notifications.json"); d != nil {
		out.Recent = recentAlerts(d["recent"], maintenanceRecentMax)
		newest = max(newest, generatedAt(d))
	}

	if status == "" {
		// Data was read and nothing was wrong: healthy. No file at all: unknown.
		status = "ok"
		if !readAny {
			status = "unknown"
		}
	}
	out.Status = status
	if out.Updated = uint64(max(newest, 0)); out.Summary == "" && status != "unknown" {
		out.Summary = "Maintenance: " + status
	}
	return out
}

// readJSON loads and decodes one published file, returning nil on any error (missing, unreadable,
// too large, or not an object). It never panics and never returns partial data.
func (m *maintenanceManager) readJSON(name string) map[string]any {
	path := filepath.Join(m.dir, name)
	info, err := os.Stat(path)
	if err != nil || info.Size() > maintenanceMaxFile {
		return nil
	}
	data, err := os.ReadFile(path)
	if err != nil {
		slog.Debug("Maintenance read failed", "file", name, "err", err)
		return nil
	}
	var d map[string]any
	if err := json.Unmarshal(data, &d); err != nil {
		slog.Debug("Maintenance parse failed", "file", name, "err", err)
		return nil
	}
	return d
}

// maintenanceLevel maps the verdict to the numeric stat used for history/charts/alerts.
func maintenanceLevel(status string) float64 {
	switch status {
	case "crit":
		return 2
	case "warn":
		return 1
	default: // ok, unknown, ""
		return 0
	}
}

func listOf(v any) []map[string]any {
	raw, ok := v.([]any)
	if !ok {
		return nil
	}
	out := make([]map[string]any, 0, len(raw))
	for _, e := range raw {
		if m, ok := e.(map[string]any); ok {
			out = append(out, m)
		}
	}
	return out
}

// recentAlerts maps the delivery log's recent[] into the bounded wire list (newest first as
// published). Every field is clamped so one hostile title cannot bloat the payload.
func recentAlerts(v any, n int) []system.MaintenanceAlert {
	raw := listOf(v)
	if len(raw) > n {
		raw = raw[:n]
	}
	out := make([]system.MaintenanceAlert, 0, len(raw))
	for _, r := range raw {
		out = append(out, system.MaintenanceAlert{
			TS:       uint64(max(int64(num(r["ts"])), 0)),
			Kind:     clampStr(str(r["kind"]), 24),
			Severity: clampStr(str(r["severity"]), 8),
			Title:    clampStr(str(r["title"]), 120),
			OK:       truthy(r["ok"]),
			Note:     clampStr(str(r["note"]), 160),
			Skipped:  clampStr(str(r["skipped"]), 24),
		})
	}
	return out
}

func truthy(v any) bool {
	b, _ := v.(bool)
	return b
}

func clampStr(s string, n int) string {
	if len(s) <= n {
		return s
	}
	r := []rune(s)
	if len(r) > n {
		r = r[:n]
	}
	return string(r)
}

func str(v any) string {
	s, _ := v.(string)
	return s
}

func num(v any) float64 {
	f, _ := v.(float64)
	return f
}

func generatedAt(d map[string]any) int64 {
	return int64(num(d["generated_at"]))
}
