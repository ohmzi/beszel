package agent

import (
	"os"
	"path/filepath"
	"testing"

	"github.com/henrygd/beszel/internal/entities/system"
)

// TestMaintenanceRead covers the composite verdict: the worst level wins, acknowledged incidents
// do not hold it at crit, and the counts/updated come through.
func TestMaintenanceRead(t *testing.T) {
	dir := t.TempDir()
	write := func(name, body string) {
		if err := os.WriteFile(filepath.Join(dir, name), []byte(body), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	write("self.json", `{"level":"ok","headline":"Monitoring pipeline: healthy","generated_at":100}`)
	write("checks.json", `{"generated_at":110,"checks":[{"status":"ok"},{"status":"warn"},{"status":"crit"}]}`)
	write("incidents.json", `{"generated_at":120,"open":[{"status":"open","severity":"sev2"},{"status":"acknowledged","severity":"sev3"}]}`)
	write("acks.json", `{"generated_at":130,"stats":{"active":2}}`)

	m := &maintenanceManager{dir: dir, result: system.Maintenance{Status: "unknown"}}
	got := m.read()
	if got.Status != "crit" {
		t.Fatalf("status = %q, want crit (a crit check must win)", got.Status)
	}
	if got.Failing != 2 {
		t.Fatalf("failing = %d, want 2 (warn + crit)", got.Failing)
	}
	if got.Incidents != 2 {
		t.Fatalf("incidents = %d, want 2 (open + acknowledged)", got.Incidents)
	}
	if got.Acked != 2 {
		t.Fatalf("acked = %d, want 2", got.Acked)
	}
	if got.Updated != 130 {
		t.Fatalf("updated = %d, want 130", got.Updated)
	}
	if got.Summary != "Monitoring pipeline: healthy" {
		t.Fatalf("summary = %q", got.Summary)
	}
}

// A missing directory is not fatal: the verdict is simply unknown.
func TestMaintenanceMissing(t *testing.T) {
	m := &maintenanceManager{dir: filepath.Join(t.TempDir(), "nope"), result: system.Maintenance{Status: "unknown"}}
	if got := m.read(); got.Status != "unknown" {
		t.Fatalf("status = %q, want unknown", got.Status)
	}
}

// An acknowledged crit incident counts as open but must not force the verdict to crit.
func TestMaintenanceAckedCritDoesNotEscalate(t *testing.T) {
	dir := t.TempDir()
	if err := os.WriteFile(filepath.Join(dir, "incidents.json"),
		[]byte(`{"open":[{"status":"acknowledged","severity":"sev1"}]}`), 0o644); err != nil {
		t.Fatal(err)
	}
	m := &maintenanceManager{dir: dir, result: system.Maintenance{Status: "unknown"}}
	got := m.read()
	if got.Incidents != 1 || got.Status != "ok" {
		t.Fatalf("incidents=%d status=%q, want 1/ok", got.Incidents, got.Status)
	}
}
