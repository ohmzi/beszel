"""netwatch.collect: the always-on Network Watch collector. Ties the readers together and writes their rows to netwatch.db.

    python3 -m homelab_maint.netwatch.collect          (systemd: homelab-maint-netwatch.service, root, see its unit)

Each source has its own cadence, and every source is isolated: one that raises or can't be read records a stale status and the
others still run. Every run writes meta["source:<name>"] = "<status>|<epoch>", where status is "ok", "stale:<why>" or "skipped"
(Cloudflare disabled). Sampling intervals double, up to 4x, while the 1-minute load is at or above the CPU count.

Metadata only. Nothing here reads packet contents or request bodies.
"""
import json
import os
import signal
import sys
import threading
import time
from pathlib import Path

from homelab_maint import core
from homelab_maint.netwatch import entities, logs, plex, sockets, store
from homelab_maint.netwatch.cloudflare import CF, CFUnavailable
from homelab_maint.netwatch.plex import CODEC_WINDOW

TICK_S = 5                 # the loop's wake-up; every source runs on its own interval, checked each tick
TUNNEL_S = 3600
PRUNE_S = 3600
MAX_STRETCH = 4            # the load back-off never multiplies an interval by more than this
PLEX_LOG = "Plex Media Server.log"
PLEX_CURSOR_STEP = 0.001  # the cursor sits just past the newest consumed line, so that line is never read again
PLEX_FIRST_S = 600         # the first Plex read looks back this far, like the journal's first read
_DEFAULTS = {
    "sockets_every_s": 30, "journal_every_s": 60, "plex_every_s": 60, "cloudflare_every_s": 300,
    "cloudflare_window_s": 900, "keep_days": 30,
}


class Collector:
    def __init__(self, db, cfg: dict, cf: CF | None, sh=core.sh, clock=time.time,
                 loadavg=os.getloadavg, cpus=os.cpu_count):
        self._db = db
        self._cfg = {**_DEFAULTS, **(cfg or {})}
        self._cf = cf
        self._sh = sh
        self._clock = clock
        self._loadavg = loadavg
        self._cpus = cpus
        self._due: dict[str, float] = {}
        self._stretch = 1
        if store.meta_get(db, "first_run") is None:
            store.meta_set(db, "first_run", str(int(clock())))

    # ---- scheduling ----------------------------------------------------------------------------------------------------

    def _update_stretch(self) -> None:
        load = self._loadavg()[0]
        if load >= (self._cpus() or 1):
            self._stretch = min(MAX_STRETCH, self._stretch * 2)
        else:
            self._stretch = 1

    @staticmethod
    def _guard(fn, now: float) -> str:
        """Run one source. A failure becomes its status, so it never stops the other sources."""
        try:
            return fn(now)
        except CFUnavailable as exc:
            return f"stale:{exc.reason}"
        except Exception as exc:
            return f"stale:{type(exc).__name__}"

    def _record(self, name: str, now: float, status: str) -> str:
        store.meta_set(self._db, f"source:{name}", f"{status}|{int(now)}")
        return status

    def tick(self, now: float) -> dict[str, str]:
        """Run every source whose interval has passed. Returns the status of each source that ran."""
        self._update_stretch()
        cfg = self._cfg
        plan = (
            ("sockets", cfg["sockets_every_s"], self._sockets, True),
            ("kernel", cfg["journal_every_s"], self._kernel, True),
            ("auth", cfg["journal_every_s"], self._auth, True),
            ("plex", cfg["plex_every_s"], self._plex, True),
            ("cloudflare", cfg["cloudflare_every_s"], self._cloudflare, True),
            ("tunnel", TUNNEL_S, self._tunnel, False),
        )
        ran: dict[str, str] = {}
        for name, every, fn, stretch in plan:
            if now < self._due.get(name, 0.0):
                continue
            self._due[name] = now + every * (self._stretch if stretch else 1)
            ran[name] = self._record(name, now, self._guard(fn, now))
        if now >= self._due.get("prune", 0.0):
            self._due["prune"] = now + PRUNE_S
            self._guard(self._prune, now)
        return ran

    # ---- sources -------------------------------------------------------------------------------------------------------

    def _sockets(self, now: float) -> str:
        topo = entities.load_topology(self._sh)
        flows, status = sockets.sample(topo, self._cfg, now, self._sh)
        store.upsert_flows(self._db, flows)
        return status

    def _kernel(self, now: float) -> str:
        lines, status = logs.read_journal("-k", "cursor:kernel", self._db, self._sh)
        if status != "ok":
            return status
        store.add_events(self._db, logs.parse_ufw(lines))
        return "ok"

    def _auth(self, now: float) -> str:
        lines, status = logs.read_journal("ssh.service", "cursor:auth", self._db, self._sh)
        if status != "ok":
            return status
        store.add_events(self._db, logs.parse_auth(lines))
        return "ok"

    def _plex(self, now: float) -> str:
        """Exploit lines follow the line-time cursor `plex:since`; codec scans follow their own cursor `plex:codec_since`,
        so a codec file that is quiet in the log is reported once, not on every tick."""
        raw = store.meta_get(self._db, "plex:since")
        since = float(raw) if raw else now - PLEX_FIRST_S
        raw = store.meta_get(self._db, "plex:codec_since")
        codec_since = float(raw) if raw else now - PLEX_FIRST_S
        plex_dir = Path(self._cfg.get("plex_dir", plex.PLEX_DIR_DEFAULT))
        if not (plex_dir / "Logs" / PLEX_LOG).is_file():
            return "stale:missing"            # nothing to read: not "ok", and neither cursor moves
        exploit_lines = plex.plex_log_lines(since, self._cfg, margin=0.0)
        codec_lines = plex.plex_log_lines(codec_since, self._cfg, margin=CODEC_WINDOW)
        events = plex.exploit_attempts(exploit_lines)
        events += plex.unexplained_codecs(plex_dir / "Codecs", codec_lines, codec_since)
        store.add_events(self._db, events)
        store.meta_set(self._db, "plex:codec_since", repr(now))
        # The exploit cursor moves to just past the newest line consumed, so the next read never repeats one.
        stamps = [ts for ts in map(plex.line_ts, exploit_lines) if ts is not None and ts >= since]
        if stamps:
            store.meta_set(self._db, "plex:since", repr(max(stamps) + PLEX_CURSOR_STEP))
        return "ok"

    def _cloudflare(self, now: float) -> str:
        if self._cf is None:
            return "skipped"
        window = int(self._cfg["cloudflare_window_s"])
        path_classes = self._cfg.get("path_classes") or {}
        zones = list(self._cfg.get("cf_zones") or self._cf.zones().values())
        for zone in zones:
            store.upsert_edge(self._db, self._cf.edge_requests(zone, now - window, now, path_classes))
            # Security events are not keyed, so only those after this zone's last read are stored: windows overlap.
            mark = store.meta_get(self._db, f"cf:events:{zone}")
            since = float(mark) if mark else now - window
            fresh = [e for e in self._cf.security_events(zone, now - window, now) if e.ts > since]
            store.add_events(self._db, fresh)
            store.meta_set(self._db, f"cf:events:{zone}", str(now))
        return "ok"

    def _tunnel(self, now: float) -> str:
        if self._cf is None:
            return "skipped"
        hosts = sorted(self._cf.tunnel_hostnames())
        store.meta_set(self._db, "tunnel:hosts", json.dumps(hosts))
        return "ok"

    def _prune(self, now: float) -> str:
        store.prune(self._db, now, int(self._cfg["keep_days"]))
        return "ok"


def main(argv: list[str] | None = None) -> int:
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda _signum, _frame: stop.set())
    cfg = core.load_config().get("netwatch", {})
    db = store.open_db()
    collector = Collector(db, cfg, CF(account_id=cfg.get("cf_account_id") or None))
    try:
        while not stop.is_set():
            collector.tick(time.time())
            stop.wait(TICK_S)
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
