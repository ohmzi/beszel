# Network Watch Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Record 30 days of who talks to this server and what it talks to, flag the odd and the
hostile, email the owner about real attempts, show it all on an OhmzMaintainer Network page and a
Homarr widget, and apply owner-confirmed blocks on the host firewall or at Cloudflare.

**Architecture:**
- **Collector.** A new always-on service (`homelab-maint-netwatch`, like `homelab-maint-live`)
  samples four sources into one SQLite store under `/var/lib/homelab-maint`:
  - the socket tables of the host and each container
  - the kernel log's UFW lines
  - Cloudflare's edge analytics
  - Plex's log
- **Judge.** A C0 check task, `net_watch`, reads the store every 5 minutes. It applies the
  learned baseline, the threat lists and the attack rules, and pages through the existing
  notify path.
- **Views.** The publisher exports `network.json` for the dashboard. A payload feeds the
  Homarr widget.
- **Blocks.** They ride the existing signed-inbox path (hub → agent → `ack/inbox` → runner)
  as new request kinds.

**Tech Stack:**
- Engine: Python 3.12 stdlib only (sqlite3, urllib, ipaddress, tomllib), pytest.
- Commands: `ss`, `nsenter`, `journalctl`, `nft`.
- Dashboard: Go (Beszel agent and hub). React plus Lingui (`internal/site`).
- Cloudflare GraphQL Analytics and the v4 REST API.

**Spec:** `docs/superpowers/specs/2026-10-19-network-watch-design.md` (approved 2026-10-19,
including section 6).

## Global Constraints

- **Retention:** 30 days, then pruned. Learning period: 7 days from the first collector start,
  persisted, so a reinstall never restarts it.
- **Metadata only:** who, when, service or container, port, connection count and bytes. Never
  packet contents, never request bodies. Request paths are stored only as a `path_class` label,
  never raw.
- **Email** goes only to the owner through `notify.send` and the existing Gmail route, quiet
  level:
  - an attack rule firing
  - a known-bad address doing more than scanning
  - a burst of failed sign-ins
  - a service talking somewhere new
  - scanning alone never emails
  - no digest
- **Blocks are never automatic.** Every block is a signed request from a logged-in dashboard
  user (hub `apiAuth` + `excludeReadOnlyRole`). The email link only opens
  `/network?finding=<id>` with the block pre-filled.
  - Correction to the spec: the dashboard is protected by its own hub login, not Cloudflare
    Access. No Access application covers it, as observed on 2026-10-19.
- **Never block (owner's):**
  - Hard ranges, never overridable: `192.168.40.0/24`, `100.64.0.0/10`, every Docker network
    subnet, loopback, link-local.
  - Addresses that signed in successfully in the last 30 days: refused unless the request
    carries a non-empty `override_reason`, which is audited.
- **Blocks expire after 30 days** unless `permanent`.
  - Host target: nftables set elements with a timeout, in a dedicated `inet netwatch` table.
    ufw's rules are never edited.
  - Cloudflare target: IP access rules (mode `block`) on the zone that owns the hostname.
- **The Cloudflare token** is read only from `/etc/homelab-maint/cloudflare.token` (0600 root).
  It is never logged, never in argv, never in an exception message, never in a public file.
- **Collector budget:** `CPUQuota=10%`, `Nice=10`, `IOSchedulingClass=idle`. When the 1-minute
  load is at or above `nproc`, the sampling intervals double, capped at 4×.
- **Engine contract:** summaries are one ASCII line of at most 140 characters, items at most 12,
  metrics small scalars. Fail closed: a source that can't be read is reported stale, never
  "all fine". `core.sh` gets argv lists only.
- **The repository is public.** No hostnames, IPs or zone IDs in committed code, tests or docs.
  Tests use documentation ranges (`203.0.113.0/24`, `198.51.100.0/24`, `2001:db8::/32`) and
  `example.test` hosts.

## Review Focus

1. **Cloudflare unreachable, rate-limited (429) or token missing.** The collector keeps sampling
   everything else. `net_watch` reports `warn` "edge data stale" after 30 minutes. Nothing
   crashes or loops hot. Tests are in Task 5 and Task 11.
2. **IPv6 everywhere:** Plex has a v6 rule, Cloudflare has v6 clients, and containers can use v6.
   Every parser, classifier, protected-range check and nft set handles both families. Tests are
   in Tasks 2, 3, 5 and 14.
3. **A container restarting between listing and `nsenter`**, so its PID is gone. That container
   is skipped for the tick and the rest still record. Test in Task 3.
4. **A mass scan** (thousands of distinct outside IPs in an hour). The store aggregates per
   bucket, the judge produces no finding, and `network.json` stays under 512 KB by top-N
   capping. Tests are in Task 1, Task 9 and Task 12.
5. **A block request for an address that is already blocked, or an unblock for one that
   expired.** Both are idempotent and succeed without duplicate nft elements or duplicate
   Cloudflare rules. Test in Task 14.

---

## Milestone A: collect

### Task 1: Store and model

**Files:**
- Create: `engine/homelab_maint/netwatch/__init__.py`, `engine/homelab_maint/netwatch/model.py`,
  `engine/homelab_maint/netwatch/store.py`
- Test: `engine/tests/test_netwatch_store.py`

**Interfaces:**
- Produces, in `model.py`, frozen dataclasses:
  - `Flow(bucket: int, direction: str, src: str, dst: str, dport: int, proto: str, conns: int, bytes_in: int, bytes_out: int)`.
    `bucket` is the epoch hour. `direction` is one of `"in"|"out"|"svc"|"lan"`. `src`/`dst`
    are entity names (`container:<name>`, `host`, `lan:<ip>`, `ts:<ip>`) or a bare IP.
  - `EdgeRow(bucket: int, zone: str, host: str, client_ip: str, country: str, asn: int, status: int, path_class: str, count: int, bytes: int)`
  - `NetEvent(ts: float, kind: str, src: str, dst: str, dport: int, detail: str)`.
    `kind` is one of `fw_block|cf_security|plex_exploit|codec_unexplained|auth_fail|auth_ok`.
- Produces, in `store.py`:
  - `open_db(path: Path | None = None) -> sqlite3.Connection`: the default is
    `core.STATE_DIR / "netwatch.db"`, mode 0600, WAL, and it creates the schema.
  - `meta_get(db, key) -> str | None` / `meta_set(db, key, value) -> None`
  - `upsert_flows(db, rows: list[Flow]) -> int`: sums counts on the
    `(bucket,direction,src,dst,dport,proto)` key.
  - `upsert_edge(db, rows: list[EdgeRow]) -> int`: replaces on
    `(bucket,zone,host,client_ip,status,path_class)`. A Cloudflare window re-read is
    idempotent.
  - `add_events(db, rows: list[NetEvent]) -> int`
  - `prune(db, now: float, keep_days: int = 30) -> int`

- [ ] **Step 1: Write the failing tests.**
  - `test_prune_keeps_29_days_drops_31`: one Flow, one EdgeRow and one NetEvent at 29 and at
    31 days old. After `prune`, only the 29-day rows remain.
  - `test_upsert_flows_sums`: two identical-key Flows with `conns` 2 and 3 store `conns == 5`.
  - `test_upsert_edge_idempotent`: the same EdgeRow twice gives one row with `count` unchanged.
  - `test_db_mode_0600`
  - `test_meta_persists_across_reopen`
- [ ] **Step 2:** Run `cd engine && python3 -m pytest tests/test_netwatch_store.py -q`.
  Expected: FAIL (module missing).
- [ ] **Step 3: Implement `model.py` and `store.py`.** One table per row type, plus `meta` and
  `baseline` (used by Task 9), plus `blocks` and `signins` (used by Tasks 8 and 14). Create
  them all here so the schema has one owner.
- [ ] **Step 4:** Run the tests. Expected: PASS.
- [ ] **Step 5: Commit** with `feat(netwatch): store and model`.

### Task 2: Topology and peer classification

**Files:**
- Create: `engine/homelab_maint/netwatch/entities.py`
- Test: `engine/tests/test_netwatch_entities.py`, `engine/tests/fixtures/netwatch/docker_networks.json`

**Interfaces:**
- Produces:
  - `Topology(containers: dict[str, ContainerInfo], subnets: list[ip_network], compose: dict[str, str], listening: set[tuple[str,int]])`
  - `ContainerInfo(name: str, pid: int, ips: list[str], project: str)`
  - `load_topology(sh=core.sh) -> Topology`: from `docker ps -q`,
    `docker inspect <ids>` and `docker network inspect <all>`, plus `ss -tlnH`/`ss -ulnH` on the
    host for `listening`.
  - `classify(ip: str, topo: Topology, cfg: dict) -> Peer`, with
    `Peer(kind: "container"|"host"|"lan"|"tailscale"|"internet", name: str)`.
  - `HARD_RANGES(topo) -> list[ip_network]`: LAN, Tailscale, Docker subnets, loopback,
    link-local, for both families.

- [ ] **Step 1: Write the failing tests** against the fixture:
  - a container IP gives `("container","<name>")`
  - `192.168.40.12` gives `"lan"`
  - `100.101.1.2` gives `"tailscale"`
  - `203.0.113.9` gives `"internet"`
  - `2001:db8::5` gives `"internet"`
  - `fd00::1` on a Docker v6 subnet gives `"container"`
  - `HARD_RANGES` contains `192.168.40.0/24` and `100.64.0.0/10`
  - when `docker` fails, `load_topology` returns an empty Topology flagged `stale=True`, not an
    exception
- [ ] **Step 2:** Run them and confirm they fail.
- [ ] **Step 3: Implement `entities.py`.** The LAN and Tailscale ranges come from `[netwatch]`
  `lan_cidrs` / `tailscale_cidrs` in `maint.toml`, defaulting to the two above. The container's
  Compose project comes from the `com.docker.compose.project` label.
- [ ] **Step 4:** Run the tests. Expected: PASS.
- [ ] **Step 5: Commit** with `feat(netwatch): topology and peer classification`.

### Task 3: Socket sampler

**Files:**
- Create: `engine/homelab_maint/netwatch/sockets.py`
- Test: `engine/tests/test_netwatch_sockets.py`, `engine/tests/fixtures/netwatch/ss_tin.txt`

**Interfaces:**
- Consumes: `Topology`, `classify` (Task 2), `Flow` (Task 1).
- Produces: `sample(topo: Topology, cfg: dict, now: float, sh=core.sh) -> list[Flow]`
  - It runs `ss -tinHn state established` on the host, and
    `nsenter -t <pid> -n ss -tinHn state established` per container.
  - It parses `bytes_sent:` / `bytes_received:` (zero when absent).
  - It sets `direction`:
    - `"in"` when the local `(addr,port)` is in `topo.listening` (or the container's published
      ports) and the peer is internet
    - `"out"` for any other internet peer
    - `"lan"` for lan/tailscale peers
    - `"svc"` for container/host peers
  - It aggregates into the hour bucket.
  - Sockets on loopback that belong to cloudflared (local ports from the tunnel's service list,
    `cfg["tunnel_local_ports"]`) are skipped. Their real peers come from Cloudflare (Task 5).

- [ ] **Step 1: Write the failing tests.**
  - The fixture has an inbound v4 `:32400` peer `203.0.113.7`, an outbound container peer
    `198.51.100.20:443`, a v6 peer, a container→container peer, and a LAN peer. The test
    asserts five Flows with the right `direction`, `src`, `dst`, `dport`, and summed bytes.
  - `test_vanished_container_skipped`: `nsenter` exits 1 for one PID, and the other container's
    flows are still returned.
- [ ] **Step 2:** Run them and confirm they fail.
- [ ] **Step 3: Implement `sockets.py`.**
- [ ] **Step 4:** Run the tests. Expected: PASS.
- [ ] **Step 5: Commit** with `feat(netwatch): socket sampler`.

### Task 4: Kernel-log, auth-log and Plex-log readers

**Files:**
- Create: `engine/homelab_maint/netwatch/logs.py`, `engine/homelab_maint/netwatch/plex.py`
- Test: `engine/tests/test_netwatch_logs.py`, `engine/tests/test_netwatch_plex.py`

**Interfaces:**
- Produces, in `logs.py`:
  - `parse_ufw(lines: Iterable[str]) -> list[NetEvent]`: parses `UFW BLOCK` lines into
    `kind="fw_block"` with SRC/DST/DPT/PROTO.
  - `parse_auth(lines) -> list[NetEvent]`: sshd `Failed password` / `Invalid user` lines into
    `auth_fail`, and `Accepted` lines into `auth_ok`.
  - `read_journal(unit_or_k: str, cursor_key: str, db, sh=core.sh) -> list[str]`: uses
    `journalctl --show-cursor --after-cursor`, storing the cursor in `meta`.
- Produces, in `plex.py`:
  - `exploit_attempts(lines) -> list[NetEvent]`: matches
    `CompanionProxy/Subscribe.*unsupported protocol '.*searchOne` and
    `/player/timeline.*protocol=`, as `kind="plex_exploit"`. `detail` gets the request id, never
    the URL's token.
  - `unexplained_codecs(codec_dir: Path, lines, since: float) -> list[NetEvent]`: a file
    modified after `since` is unexplained unless a `CodecManager: obtaining (decoder|encoder) '<x>'`
    line is logged within 5 seconds before its mtime and the filename is `lib<x>_decoder.so` or
    `lib<x>_encoder.so`.
  - `plex_log_lines(since: float) -> list[str]`: reads `Plex Media Server.log` under the snap
    path from `[netwatch] plex_dir`, defaulting to the snap's data path.

- [ ] **Step 1: Write the failing tests.**
  - The UFW fixture line (v4 and v6) gives the expected fields.
  - An `auth.log` fixture gives 3 `auth_fail` and 1 `auth_ok`.
  - The Plex fixture uses the real line shape with a placeholder identifier and gives one
    `plex_exploit` with the request id.
  - A codec written 0.4 s after its log line is explained.
  - A codec with no log line is reported.
  - A codec whose log line names a different decoder is reported.
- [ ] **Step 2:** Run them and confirm they fail.
- [ ] **Step 3: Implement `logs.py` and `plex.py`.**
- [ ] **Step 4:** Run the tests. Expected: PASS.
- [ ] **Step 5: Commit** with `feat(netwatch): log readers and Plex rules`.

### Task 5: Cloudflare client

**Files:**
- Create: `engine/homelab_maint/netwatch/cloudflare.py`
- Test: `engine/tests/test_netwatch_cloudflare.py`, `engine/tests/fixtures/netwatch/cf_*.json`

**Interfaces:**
- Produces: `class CF(token_path: Path = core.CONF_DIR / "cloudflare.token", http=urllib.request.urlopen, account_id: str | None = None)`
  - `zones() -> dict[str, str]`: name to id.
  - `tunnel_hostnames() -> dict[str, str]`: hostname to its local service URL, from
    `cfd_tunnel/<id>/configurations`.
  - `edge_requests(zone_id: str, start: float, end: float, path_classes: dict[str, str]) -> list[EdgeRow]`
    - Uses `httpRequestsAdaptiveGroups` with dimensions `clientIP clientRequestHTTPHost clientRequestPath edgeResponseStatus clientCountryName clientAsn datetimeHour`,
      `count`, and `sum{edgeResponseBytes}`.
    - `path_class` is the first matching name in `path_classes` (regex to class), otherwise
      `"other"`. The raw path is dropped.
  - `security_events(zone_id, start, end) -> list[NetEvent]`: from `firewallEventsAdaptive`,
    as `kind="cf_security"`, with `detail = "<action>/<source>"`.
  - `create_access_rule(zone_id: str, ip: str, note: str) -> str` (the rule id),
    `delete_access_rule(zone_id: str, rule_id: str) -> None`,
    `list_access_rules(zone_id: str) -> list[dict]`
  - The account id comes from `[netwatch] cf_account_id` in `maint.toml`.
  - Errors are raised as `CFUnavailable(reason: str)`: missing or unreadable token file, HTTP
    429 or 5xx, or a GraphQL `errors` entry. The token never appears in it.

- [ ] **Step 1: Write the failing tests** with a fake `http` serving the fixtures:
  - The Authorization header is `Bearer <token from tmp file>`.
  - `edge_requests` maps a fixture group to an `EdgeRow` with `path_class` resolved and no raw
    path stored.
  - A v6 `clientIP` is preserved.
  - A missing token file raises `CFUnavailable("token")`.
  - HTTP 429 raises `CFUnavailable` with `"429"`, and `str(exc)` doesn't contain the token.
  - `create_access_rule` POSTs `{"mode":"block","configuration":{"target":"ip"|"ip6","value":ip},"notes":note}`.
- [ ] **Step 2:** Run them and confirm they fail.
- [ ] **Step 3: Implement `cloudflare.py`.**
- [ ] **Step 4:** Run the tests. Expected: PASS.
- [ ] **Step 5: Commit** with `feat(netwatch): cloudflare client`.

### Task 6: Collector service

**Files:**
- Create: `engine/homelab_maint/netwatch/collect.py`, `engine/systemd/homelab-maint-netwatch.service`
- Modify: `engine/install.sh` (install, enable and restart the unit the way `$LIVE` is
  handled), `engine/etc/maint.toml` (add a `[netwatch]` table with the defaults named in Tasks
  2 to 5 and 8)
- Test: `engine/tests/test_netwatch_collect.py`

**Interfaces:**
- Consumes: Tasks 1 to 5.
- Produces:
  - `Collector(db, cfg, cf: CF | None, sh=core.sh, clock=time.time)`
  - `Collector.tick(now: float) -> dict[str, str]`, returning a source-to-status map
    (`"ok"|"stale:<why>"|"skipped"`)
  - `main(argv=None) -> int`, run as `python3 -m homelab_maint.netwatch.collect`
- Schedule inside `tick`:
  - sockets every 30 s
  - kernel and auth journal every 60 s
  - Plex log every 60 s
  - Cloudflare every 300 s, reading `[now-900, now]`; `upsert_edge` makes overlap harmless
  - tunnel hostnames hourly
  - prune hourly
- On the first start, set `meta["first_run"]` if absent.
- Write `meta["source:<name>"] = "<status>|<ts>"` per source for Task 11.
- Intervals double while `os.getloadavg()[0] >= os.cpu_count()`, capped at 4×.
- SIGTERM finishes the tick and exits 0.
- The unit is modelled on `homelab-maint-live.service`, with `CPUQuota=10%`, `Nice=10`,
  `IOSchedulingClass=idle` and `ReadWritePaths=/var/lib/homelab-maint`. It needs root for
  `nsenter`, the Docker socket and the journal.

- [ ] **Step 1: Write the failing tests** with fakes for `sh`, `CF` and the clock:
  - one `tick` writes Flow, EdgeRow and event rows
  - `CF` raising `CFUnavailable` marks `source:cloudflare` stale while the other sources
    record
  - a load of 2×nproc doubles the next socket interval
  - `first_run` is set once and never moves on a later start
- [ ] **Step 2:** Run them and confirm they fail.
- [ ] **Step 3: Implement `collect.py` and the unit. Wire `install.sh`.**
- [ ] **Step 4:** Run the tests. Expected: PASS.
- [ ] **Step 5: Verify on the host.**
  - Run `cd engine && sudo ./install.sh --dry-run` and confirm it lists the new unit.
  - Then run `sudo ./install.sh` and wait 10 minutes.
  - Run `sudo sqlite3 /var/lib/homelab-maint/netwatch.db "select count(*) from flows; select count(*) from edge;"`.
    Both counts must be above 0.
  - Run `systemctl show homelab-maint-netwatch -p CPUUsageNSec` and confirm the CPU use is
    under 10% of the elapsed time.
- [ ] **Step 6: Commit** with `feat(netwatch): collector service`.

## Milestone B: judge and alert

### Task 7: Threat lists and indicators

**Files:**
- Create: `engine/homelab_maint/netwatch/threat.py`, `engine/etc/netwatch-ioc.toml`
- Modify: `engine/etc/jobs.toml` (a daily `netwatch-threat-refresh` job running
  `python3 -m homelab_maint.netwatch.threat refresh`)
- Test: `engine/tests/test_netwatch_threat.py`

**Interfaces:**
- Produces:
  - `refresh(fetch=urllib.request.urlopen, now=None) -> dict[str, str]`, returning a
    list-to-status map.
    - It downloads Spamhaus DROP, Spamhaus EDROP, the FireHOL level1 netset and the abuse.ch
      Feodo IP blocklist into `core.STATE_DIR/netwatch/threat/<name>.txt`.
    - On a failure it keeps the old file.
  - `ThreatIndex.load(dir, ioc_path) -> ThreatIndex` and `ThreatIndex.lookup(ip: str) -> str | None`,
    returning the list name or `"ioc"`.
  - `ThreatIndex.stale_lists(now) -> list[str]`: lists older than 3 days.
  - `netwatch-ioc.toml` ships empty `ips = []` / `domains = []` tables with a comment. The
    owner's live copy under `/etc/homelab-maint/` holds the Plex2Shell indicators: added at
    install by the Task 16 step, not committed, because the repository is public.

- [ ] **Step 1: Write the failing tests.**
  - CIDR membership works for v4 and v6.
  - A failed fetch keeps the previous file and reports `"kept:<reason>"`.
  - A 4-day-old file is stale.
  - An IOC entry wins with `"ioc"`.
- [ ] **Step 2:** Run them and confirm they fail.
- [ ] **Step 3: Implement `threat.py`.**
- [ ] **Step 4:** Run the tests. Expected: PASS.
- [ ] **Step 5: Commit** with `feat(netwatch): threat lists and indicators`.

### Task 8: Never-block set

**Files:**
- Create: `engine/homelab_maint/netwatch/protect.py`
- Test: `engine/tests/test_netwatch_protect.py`

**Interfaces:**
- Consumes: `HARD_RANGES` (Task 2), the `edge` rows (Task 1), `auth_ok` events (Task 4).
- Produces:
  - `record_signins(db, now: float, cfg: dict, plex_sessions=None) -> int`: writes
    `signins(ip, source, last_seen)`. The sources:
    - Edge rows whose `(host, path_class)` is listed in `[netwatch.signin]` and whose status
      is 2xx count as a signed-in session. The defaults are written as `path_classes` entries
      named `auth_ok_<app>` for Immich, Nextcloud, T'Day and Plex-over-tunnel; Task 16 checks
      them against real data.
    - `auth_ok` sshd events.
    - Plex `/status/sessions` player addresses that are internet peers. This reads the local
      API with the server's own `PlexOnlineToken` from `Preferences.xml`, root only, and never
      logs it.
  - `protection(ip: str, db, topo, now) -> tuple[str, str] | None`: returns `("hard", reason)`,
    `("signin", "<source> <date>")` or `None`. A sign-in is protected for 30 days from its
    `last_seen`.

- [ ] **Step 1: Write the failing tests.**
  - The LAN, a Tailscale address, a Docker subnet address and `::1` are all `hard`.
  - An edge 200 on `auth_ok_immich` gives `signin` for 30 days, and `None` on day 31.
  - An edge 401 on the same path gives no sign-in.
  - A fake Plex session with internet player `203.0.113.4` gives `signin`.
- [ ] **Step 2:** Run them and confirm they fail.
- [ ] **Step 3: Implement `protect.py`.**
- [ ] **Step 4:** Run the tests. Expected: PASS.
- [ ] **Step 5: Commit** with `feat(netwatch): never-block set`.

### Task 9: Judge

**Files:**
- Create: `engine/homelab_maint/netwatch/judge.py`
- Test: `engine/tests/test_netwatch_judge.py`

**Interfaces:**
- Consumes: Tasks 1, 2, 7 and 8.
- Produces:
  - `Finding(id: str, rule: str, severity: "warn"|"crit", src: str, target: str, why: str, first: float, last: float, count: int, block: "host"|"cloudflare"|None, zone: str | None, stolen_account: str | None)`.
    `id` is the 16-hex `sha1(rule|src|target)[:16]`.
  - `judge(db, topo, threat: ThreatIndex, now: float, cfg: dict) -> list[Finding]`: looks back
    over `[now - 1800, now]`, so a finding lives across the runner's two-run debounce. It also
    updates `baseline`.
- Rules, as `rule` names:
  - **`plex_exploit`** (crit, block=host): any `plex_exploit` event. The source IP comes from
    the nearest `in` flow or UFW line on 32400 within 5 seconds, otherwise `"unknown"`, with
    block=None.
  - **`codec_unexplained`** (crit, block=None)
  - **`threat_active`** (warn): an internet IP that `ThreatIndex` matches and that made either
    more than 20 edge requests in the window, any 2xx to a non-`other` path class, or any `in`
    flow. Its block is `"cloudflare"` with the host's zone for edge traffic, otherwise
    `"host"`. Background scanning that doesn't meet this bar is counted, not a finding.
  - **`auth_burst`** (warn): 5 or more sshd `auth_fail` from one IP in 10 minutes, or 10 or
    more edge 401/403 on an `auth_*` path class from one IP in 10 minutes.
  - **`line_not_meant`** (warn, block=None): a `fw_block` event whose SRC is a container
    address hitting a host port. These are pinhole violations.
  - **`new_peer`** (warn, block=None): only after `meta["first_run"] + 7 days`.
    - A container with a new `out` destination (by /24 or /48 network, plus port).
    - A new `svc` pair across different Compose projects. The same project is always expected.
    - A LAN or Tailscale device using a host or container port it never used.
    - Before day 7 it only records the baseline.
- Every finding's src goes through `protection()`. A `signin` match sets `stolen_account` and
  keeps the finding. A `hard` match drops `block` to None.

- [ ] **Step 1: Write the failing tests**, one per rule, plus:
  - `test_scan_only_no_findings`: 5,000 distinct threat-listed IPs with one edge request each
    give `[]`.
  - `test_new_peer_silent_during_learning`
  - `test_same_project_svc_never_flags`
  - `test_signin_source_marks_stolen_account`
  - `test_ids_stable`
- [ ] **Step 2:** Run them and confirm they fail.
- [ ] **Step 3: Implement `judge.py`.**
- [ ] **Step 4:** Run the tests. Expected: PASS.
- [ ] **Step 5: Commit** with `feat(netwatch): judge`.

### Task 10: Review link in alert emails

**Files:**
- Modify: `engine/homelab_maint/core.py` (`Result`: add `link: str = ""`), and the
  Result-to-Event mapping in `engine/homelab_maint/cli.py`, which copies a non-empty
  `Result.link` into `Event.facts["link"]`, a reserved key.
- Test: `engine/tests/test_result_link.py`

**Interfaces:**
- Produces: `Result.link`, which `notify`'s email renders as the alert's link through the
  existing `facts["link"]` handling.

- [ ] **Step 1: Write the failing test.** A task returning `Result("warn", "x", link="https://example.test/network?finding=0123456789abcdef")`,
  sent through a fake transport, gives an email HTML containing that URL. The SMS text doesn't
  contain it, because texts carry no link.
- [ ] **Step 2:** Run it and confirm it fails.
- [ ] **Step 3: Implement the field and the mapping.** If `facts["link"]` isn't already
  rendered, render it next to the existing dashboard link in `notify_templates.py`.
- [ ] **Step 4:** Run the test plus `python3 -m pytest tests -q -k notify`. Expected: PASS.
- [ ] **Step 5: Commit** with `feat(notify): optional per-result review link`.

### Task 11: `net_watch` check

**Files:**
- Create: `engine/homelab_maint/tasks/net_watch.py`
- Modify: `engine/etc/maint.toml` (`[tasks.net_watch] schedule = "*/5 * * * *"`),
  `engine/etc/notify.toml` (`[task_routes] net_watch = "email"` and
  `[todo] net_watch = [...]`), `engine/etc/playbooks.toml` (`[playbook.net_watch]`)
- Test: `engine/tests/test_net_watch.py`

**Interfaces:**
- Consumes: `judge` (Task 9), `ThreatIndex` (Task 7), `load_topology` (Task 2), the store's
  `meta` (Tasks 1 and 6), `Result.link` (Task 10).
- Produces: `@task("net_watch", klass="C0", tier="check", title="Network watch", timeout=60, needs_root=True)`.
  - **Status:** `crit` if any crit finding, `warn` if any warn finding or any source stale for
    30 minutes or more, otherwise `ok`.
  - **Summary example:** `"Network: 1 crit (plex_exploit 203.0.113.7), 2 warn"`.
  - **Items:** the top 12 findings by severity, then recency.
  - **Metrics:** `in24`, `out24`, `svc24`, `lan24` (flow counts), `edge24` (requests),
    `flags`, `blocked`, `stale_sources`, and `top_talkers`.
    - `top_talkers` is one string of at most 120 characters, for example
      `"203.0.113.7 CA 1200; 198.51.100.2 US 300"`. It's a scalar, so the widget payload can
      stay a pure function of `status.json`.
  - **issue_key:** `core.ikey(rules=sorted(rule:src))`.
  - **link:** `<site.url>/network?finding=<top finding id>`.
  - It also writes `core.STATE_DIR/netwatch/findings.json` (0600) for the publisher.

- [ ] **Step 1: Write the failing tests**, with the scaffold's contract checks:
  - registered as C0/check
  - summary is ASCII and at most 140 characters
  - a seeded `plex_exploit` gives `crit` and a link containing that finding's id
  - scan-only data gives `ok`
  - a Cloudflare source stale for 31 minutes gives `warn` "edge data stale"
- [ ] **Step 2:** Run them and confirm they fail.
- [ ] **Step 3: Implement the task and its config entries.** The playbook says what each rule
  means and that blocking happens on the Network page.
- [ ] **Step 4:** Run the tests, then run the task once with the dry-run snippet from
  `docs/EXTENDING.md` section 3. Expected: PASS, and a one-line status.
- [ ] **Step 5: Commit** with `feat(netwatch): net_watch check and alert route`.

## Milestone C: show

### Task 12: Public export and Homarr widget

**Files:**
- Modify: `engine/homelab_maint/publish.py` (add `("network.json", build_network)` to
  `BUILDERS`), `engine/homelab_maint/payloads.py` (`ROUTES["net"] = net`),
  `engine/widgets/build_v2.py` (an `ops-net` entry)
- Modify: `internal/hub/api.go` (add `"network.json"` to `maintenanceFiles`)
- Create: `engine/homelab_maint/netwatch/export.py`, `engine/widgets/ops-net.jsx`
- Test: `engine/tests/test_netwatch_export.py`, `engine/tests/test_payload_net.py`

**Interfaces:**
- Produces:
  - `build_network(status, now) -> dict`. It's 0644 and capped at 512 KB, and it has these
    keys:
    - `generated`, `learning_until`
    - `sources` (status per source)
    - `directions` (24h and 30d counts per direction)
    - `talkers` (the top 50 internet IPs over 24h, each with country, ASN, hosts, requests,
      threat list, and protection kind)
    - `services` (per container or host: peers in and out, 24h)
    - `findings` (open, from `findings.json`)
    - `blocks` (id, ip, target, zone, until, permanent, reason)
    - `protected` (ip and reason, signins only; the hard ranges are listed as CIDRs)
  - Every string passes `publish`'s existing redaction. No token or path from the token file
    ever appears.
  - `net(status, now) -> dict`: the widget payload, under 4 KB, built from `net_watch`'s
    entry in `status.json`.
    - Counts per direction come from its metrics.
    - Top talkers come from `metrics["top_talkers"]`.
    - Open flags come from its `items`.

- [ ] **Step 1: Write the failing tests.**
  - `build_network` on a seeded store of 10,000 talkers stays at or under 512 KB, with 50
    talkers.
  - The output never contains the fixture token string.
  - `net()` payload is under 4 KB, with colour words precomputed.
  - `python3 widgets/build_v2.py --check` passes with `ops-net`.
- [ ] **Step 2:** Run them and confirm they fail.
- [ ] **Step 3: Implement `export.py`, the builder registration, the payload and the widget.**
- [ ] **Step 4:** Run the tests, plus `cd .. && go test ./internal/hub/ -run Maintenance`.
  Expected: PASS.
- [ ] **Step 5: Commit** with `feat(netwatch): network.json export and Homarr widget`.

### Task 13: Network page

**Files:**
- Create: `internal/site/src/components/routes/network.tsx`
- Modify: `internal/site/src/components/router.tsx` (`network: "/network"`), the navbar file
  (a "Network" entry next to Monitors), and the route switch in `main.tsx`, following the
  `incidents` route
- Test: build and type-check (`cd internal/site && npm run build`)

**Interfaces:**
- Consumes: `network.json` through `lib/maintenance.ts`'s file fetcher. The block call comes
  from Task 15 (`POST /api/beszel/maintenance/netblock`).
- The page has five tabs: Flags, Talkers, Services, Blocks, Never-block.
  - **Flags:** each finding shows its rule, why, first and last seen, and the stolen-account
    warning when set.
  - **Talkers:** shows the threat list and the protection badge.
  - **`?finding=<id>`:** selects that finding and opens a confirm dialog pre-filled with the
    target and 30 days.
    - The dialog offers a "permanent" switch.
    - It shows a required reason field only when the IP is sign-in protected.
    - It shows no confirm button at all for a hard-protected IP; the reason is shown instead.
  - **Blocks:** lists expiry and an Unblock button.
- All strings go through Lingui `t`, like the other routes.

- [ ] **Step 1: Implement the route** against the fixture `network.json` (copy one from the
  Task 12 test into `internal/site/src/components/routes/__fixtures__/network.json` for local
  dev).
- [ ] **Step 2:** Run `cd internal/site && npm run build`. Expected: success, with no type
  errors.
- [ ] **Step 3: Commit** with `feat(site): Network page`.

## Milestone D: block

### Task 14: Block application in the engine

**Files:**
- Create: `engine/homelab_maint/netwatch/block.py`
- Modify: the inbox entry point in `engine/homelab_maint/acks.py`'s `process_inbox` path,
  which registers `block.register(acks_mod)` next to `acks_auth.register`, and
  `collect.py` (call `block.restore()` at start and `block.sweep()` hourly)
- Test: `engine/tests/test_netwatch_block.py`

**Interfaces:**
- Consumes: `CF` (Task 5), `protection` (Task 8), store `blocks` (Task 1).
- Produces:
  - Inbox kinds:
    - `net_block` (keys `finding`, `ip`, `target` (`host`|`cloudflare`), `zone`, `days`,
      `permanent`, `override_reason`)
    - `net_unblock` (key `block_id`)
  - Both are registered with `acks.register_inbox_handler(kind, fn, allowed_keys=...)`.
  - `apply_block(req: dict, now: float, *, cf=None, sh=core.sh) -> tuple[bool, str]`
    - It refuses `hard` always, and `signin` without a non-empty `override_reason`.
    - Host: `nft add element inet netwatch block4|block6 { <ip> timeout <days>d }`, with no
      timeout when permanent.
    - Cloudflare: `cf.create_access_rule(zone, ip, "netwatch:<block_id> until <YYYY-MM-DD>")`.
    - It writes `blocks` and appends to `audit.jsonl` through the existing audit helper.
  - `apply_unblock(...)` is the reverse.
  - `ensure_table(sh)` creates `table inet netwatch` with `block4`/`block6` sets
    (`flags timeout`) and an input-hook drop chain at priority -10.
  - `restore(db, sh)` re-adds unexpired host blocks after a reboot.
  - `sweep(db, now, cf)` deletes expired Cloudflare rules found by the `netwatch:` note prefix.
  - `register(acks_mod) -> None` registers both kinds. It's idempotent, the same shape as
    `acks_auth.register`.

- [ ] **Step 1: Write the failing tests**, with fake `sh` and `CF`:
  - A LAN IP is refused with `"hard"`.
  - A sign-in IP with no reason is refused, and with a reason it's applied and audited.
  - A v4 host block runs the exact `nft` argv.
  - A v6 address goes to `block6`.
  - A Cloudflare block uses the right zone and note.
  - A repeat block gives `ok` with no second nft element and no second Cloudflare rule
    (Review Focus 5).
  - An unblock of an expired block gives `ok`.
  - `sweep` deletes only expired `netwatch:` rules.
- [ ] **Step 2:** Run them and confirm they fail.
- [ ] **Step 3: Implement `block.py` and the registrations.**
- [ ] **Step 4:** Run the tests plus `python3 -m pytest tests -q -k ack`. Expected: PASS, and
  the existing ack tests are unchanged.
- [ ] **Step 5: Commit** with `feat(netwatch): owner-confirmed blocks`.

### Task 15: Agent and hub endpoint

**Files:**
- Create: `agent/netwatch_block.go`, `agent/netwatch_block_test.go`
- Modify: `agent/handlers.go` (register a `MaintenanceNetBlock` action),
  `internal/common/common-ws.go` (the action plus `MaintenanceNetBlockRequest{Kind, Finding, IP, Target, Zone, Days, Permanent, OverrideReason, BlockID}`),
  `internal/hub/systems/system.go` (`MaintenanceNetBlockFromAgent`), `internal/hub/api.go`
  (`apiAuth.POST("/maintenance/netblock", h.maintenanceNetBlock).BindFunc(excludeReadOnlyRole)`)
- Test: `agent/netwatch_block_test.go`

**Interfaces:**
- Produces: `enqueueNetBlockRequest(r common.MaintenanceNetBlockRequest) error`, mirroring
  `enqueueAckRequest`:
  - `kind` is `net_block|net_unblock`
  - `ip` parses with `net/netip`
  - `target` is `host|cloudflare`
  - `days` is 1 to 365
  - the ASCII reason is at most 200 characters
  - `finding` and `block_id` are 16 hex
  - it's signed with `web.key` and written to `ack/inbox` through the existing
    `signRequest`/`writeInbox`
- The hub handler checks the user owns the system, as `maintenanceAck` does.

- [ ] **Step 1: Write the failing tests.**
  - Validation rejects each bad field.
  - The canonical bytes for a fixed request equal the bytes Python's
    `acks_auth.canonical` produces. Use the cross-language fixture approach already in
    `agent/maintenance_test.go`.
- [ ] **Step 2:** Run `go test ./agent/ -run NetBlock`. Expected: FAIL.
- [ ] **Step 3: Implement the agent, common, system and hub pieces.**
- [ ] **Step 4:** Run `go test ./agent/ ./internal/hub/...`. Expected: PASS.
- [ ] **Step 5: Commit** with `feat(hub): signed network block requests`.

### Task 16: Install, verify and document on the host

**Files:**
- Modify: `engine/README.md` (a `netwatch` row in the pieces table, plus a "Network watch"
  section: sources, retention, rules, how blocking works, how to undo), and
  `engine/docs/EXTENDING.md` (adding a rule or a path class)

- [ ] **Step 1: Install.**
  - Run `cd engine && sudo ./install.sh`.
  - Add the Plex2Shell indicators (the four attacker IPs, the miner IP and the proxystream
    domains from the 2026-10-19 check) to `/etc/homelab-maint/netwatch-ioc.toml` on the host
    only.
  - Set `[netwatch] cf_account_id` in `/etc/homelab-maint/maint.toml`.
  - Run `sudo homelab-maint run --task net_watch --dry-run`. Expected: an `ok` or `warn` line,
    and no `error`.
- [ ] **Step 2: Check the sign-in path classes against real data.**
  - For each `auth_ok_<app>` class, run
    `sudo sqlite3 /var/lib/homelab-maint/netwatch.db "select host,path_class,status,count(*) from edge where path_class like 'auth_ok_%' group by 1,2,3"`.
  - Each app the owner uses must show 2xx rows. Adjust the regexes in `[netwatch.signin]` until
    it does.
  - This is the spec's launch gate: every tunnelled app yields real client addresses, which
    the edge analytics now give for all of them. Sign-ins must be recognised for the
    account apps.
- [ ] **Step 3: Exercise the Plex rule.**
  - Copy a fixture line into a scratch log and run `plex.exploit_attempts` on it.
  - Then run `python3 -m homelab_maint.notify render --out /tmp/previews` and confirm the
    `net_watch` crit email shows the review link.
- [ ] **Step 4: Exercise blocking end to end.**
  - Block `198.51.100.1` (documentation range) for 1 day from the Network page.
  - Confirm `sudo nft list set inet netwatch block4` shows it.
  - Unblock it, and confirm it's gone.
  - Try `192.168.40.1` and confirm the dialog refuses it.
- [ ] **Step 5: Deploy the hub and agent** the way the fork deploys today (`engine/DEPLOY.md`).
  Open `/network` and confirm the five tabs render live data.
- [ ] **Step 6: Commit** with `docs(netwatch): install and operate`.
